"""
TEAMworks360 (formerly Job Profitability Manager) - Flask backend.

Run with:  python app.py
Then open: http://127.0.0.1:5000
"""
import base64
import csv
import io
import os
import sys
import re
import secrets
import sqlite3
import json
import time
from datetime import datetime, timedelta, date
import calendar
from functools import wraps

from flask import (Flask, g, jsonify, request, session, send_from_directory,
                    Response, render_template, has_request_context)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from quote_pdf import build_quote_pdf
from po_pdf import build_po_pdf
from despatch_pdf import build_despatch_pdf
import db_location
import migrations
import version as app_version
import totp
import qr_png

# import turso_serverless
import libsql
# import turso
# from http.server import BaseHTTPRequestHandler

# The Vercel Marketplace integration automatically injects these keys
# DATABASE_URL = os.environ.get("TURSO_DATABASE_URL")
# AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

# class handler(BaseHTTPRequestHandler):
 #    def do_GET(self):
  #       if not DATABASE_URL or not AUTH_TOKEN:
   #          self.send_response(500)
   #          self.end_headers()
    #         self.wfile.write(b"Missing Turso Environment Variables from Integration")
   #          return

        # Connect directly using the auto-injected credentials
#         with turso.connect(DATABASE_URL, auth_token=AUTH_TOKEN) as db:
#             cursor = db.cursor()
#             cursor.execute("SELECT * FROM users LIMIT 5;")
#             rows = cursor.fetchall()
            
#         self.send_response(200)
 #        self.send_header('Content-type', 'application/json')
 #        self.end_headers()
 #        self.wfile.write(str(rows).encode('utf-8'))
 #        return

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Must run before anything below touches the data directory (the secret
# key is loaded at import time, further down this file, before the
# __main__ block's own migrate_legacy_files() call ever gets a chance to
# run) - otherwise a genuinely fresh environment with no data directory
# yet crashes before Flask even starts.
db_location.ensure_data_dir()
# Also must run before load_or_create_secret_key() below, not just
# alongside run_startup_tasks()'s other migrations later - otherwise, on
# every existing install's very next update, load_or_create_secret_key()
# would see no key yet at the new (renamed) location and generate a brand
# new one before this ever got a chance to carry the old one across,
# silently logging everyone out instead of the seamless rename intended.
db_location.migrate_from_old_product_name()
DB_PATH = db_location.DB_PATH
SECRET_KEY_PATH = db_location.SECRET_KEY_PATH


def load_or_create_secret_key():
    """Never hardcode a secret key in source that ships to users - anyone who
    read it could forge a session cookie and log in as any role, including
    Administrator, with no password at all. Prefer an env var (for real
    deployments); otherwise generate a random key once and persist it
    locally so it's stable across restarts but unique to this install."""
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    if os.path.exists(SECRET_KEY_PATH):
        with open(SECRET_KEY_PATH, "r") as f:
            key = f.read().strip()
            if key:
                return key
    key = secrets.token_hex(32)
    with open(SECRET_KEY_PATH, "w") as f:
        f.write(key)
    try:
        os.chmod(SECRET_KEY_PATH, 0o600)  # owner read/write only
    except OSError:
        pass
    return key


app = Flask(__name__)
app.secret_key = load_or_create_secret_key()

# Sessions expire automatically after a period of inactivity rather than
# staying valid forever - reduces the window a stolen/left-open browser
# session stays usable.
app.permanent_session_lifetime = timedelta(hours=8)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,   # JS can't read the session cookie (mitigates XSS token theft)
    SESSION_COOKIE_SAMESITE="Lax",  # cookie isn't sent on cross-site requests
    # Set FLASK_HTTPS=1 once this is served over HTTPS (e.g. behind a reverse
    # proxy) so the browser refuses to send the session cookie over plain HTTP.
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_HTTPS") == "1",
    # Without this, Flask compiles index.html once on its first render and
    # keeps serving that compiled copy from memory for as long as the process
    # stays running - completely separate from the browser's own cache, so a
    # hard refresh (or even an incognito window) does nothing. On a long-lived
    # install (the usual shared-host pattern here, often left running for
    # weeks), that means a template-only update - a sidebar tweak, a label
    # change - silently fails to appear until the actual Python process
    # behind it is stopped and started again, which looks exactly like the
    # update never applied at all. This makes every request re-check the
    # template file's timestamp and reload it if it changed, regardless of
    # debug mode, so "I don't see the change" from a stale template is never
    # a possibility again - only an app that wasn't restarted after replacing
    # its files still needs that restart for a real code change.
    TEMPLATES_AUTO_RELOAD=True,
    # A circuit breaker against an accidental (or malicious) huge upload -
    # generous enough for a multi-page scanned delivery note or a handful
    # of photos, well short of anything that could meaningfully strain a
    # small office server. Applies to every request, but Goods In document
    # uploads are the only endpoint that accepts files at all.
    MAX_CONTENT_LENGTH=25 * 1024 * 1024,
)

DEFAULT_MARGIN_TARGET = 15.0  # percent - only used as a last-resort fallback if
                               # the settings table is ever empty/missing

# Goods In document uploads: what a "goods received document" is allowed to
# be. Covers a scanned/photographed delivery note or packing slip (the
# overwhelmingly common case), a PDF emailed over by the supplier, or a
# Word document for the rare case where someone types one up. Deliberately
# not open to any file type - this is audit evidence, not general file
# storage.
GOODS_IN_ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".doc", ".docx"}
GOODS_IN_MAX_FILES_PER_UPLOAD = 5

# The exact phrase an Administrator must type to confirm a go-live reset -
# see reset_job_data() below. Deliberately not a simple yes/no click,
# given how irreversible this is.
RESET_JOB_DATA_CONFIRMATION = "RESET JOB DATA"


def get_margin_settings(db):
    """Default margin target + per-contract-code overrides, managed from the
    Settings screen (Administrator only) and stored in the database. Cached
    on flask.g for the life of one request so compute_job_metrics() - called
    once per Works Order, sometimes 1000+ times in a single list/report
    request - doesn't re-query settings every single time."""
    if not hasattr(g, "_margin_settings"):
        row = db.execute("SELECT value FROM settings WHERE key='default_margin_target'").fetchone()
        default_target = safe_float(row["value"], DEFAULT_MARGIN_TARGET) if row else DEFAULT_MARGIN_TARGET
        code_rows = db.execute("SELECT contract_code, target_pct FROM contract_code_targets").fetchall()
        code_targets = {r["contract_code"].strip().upper(): r["target_pct"] for r in code_rows}
        g._margin_settings = (default_target, code_targets)
    return g._margin_settings


def get_margin_target(db, contract_code):
    default_target, code_targets = get_margin_settings(db)
    if not contract_code:
        return default_target
    return code_targets.get(contract_code.strip().upper(), default_target)

ROLE_PERMISSIONS = {
    "Administrator": {"all"},
    # Despatch Notes sit with whoever manages the job itself (Project
    # Manager), the same as Works Orders and Quotes - not thrown open to
    # every role the way Goods In is, since raising one records goods
    # actually leaving the business for a customer, not just acknowledging
    # a delivery that's already arrived.
    "Project Manager": {"works_orders:write", "labour_entries:write", "quotes:write", "goods_in:write",
                         "despatch_notes:write", "read"},
    "Procurement": {"purchase_orders:write", "inventory:write", "goods_in:write", "read"},
    "Finance": {"supplier_invoices:write", "customer_invoices:write", "goods_in:write", "read"},
    "Executive": {"goods_in:write", "read"},
    # Service Engineers work the shop-floor side only: Works Orders, Purchase
    # Orders, Goods In, Despatch Notes, Inventory and Hours Quoted & Booked.
    # No deleting (Administrator only, as for everyone) and nothing else is
    # reachable at all - see SERVICE_ENGINEER_API_PREFIXES below.
    "Service Engineer": {"works_orders:write", "purchase_orders:write", "goods_in:write",
                          "despatch_notes:write", "inventory:write", "labour_entries:write", "read"},
}

# What a Service Engineer's login may reach at all, reads included. Anything
# not listed (Quotes, Invoices, Customers, Suppliers, Reporting, Dashboard,
# Alerts, Users, Settings) is refused by the server - hiding the menu items in
# the browser is only for tidiness, this is the real lock.
SERVICE_ENGINEER_API_PREFIXES = (
    "/api/auth/", "/api/reference", "/api/version", "/api/works-orders", "/api/purchase-orders",
    "/api/goods-in", "/api/despatch-notes", "/api/stock-items", "/api/stock-allocations",
    "/api/stock-movements", "/api/labour-entries", "/api/labour-comparison", "/api/screen-summaries",
)
SERVICE_ENGINEER_AUDIT_TABLES = {"works_orders", "purchase_orders", "goods_in", "despatch_notes",
                                  "stock_items", "labour_entries"}


@app.before_request
def restrict_service_engineer():
    if session.get("role") != "Service Engineer" or not request.path.startswith("/api/"):
        return
    path = request.path
    if path.startswith("/api/audit-log/"):
        if path.split("/")[3:4] and path.split("/")[3] in SERVICE_ENGINEER_AUDIT_TABLES:
            return
    elif path.startswith(SERVICE_ENGINEER_API_PREFIXES):
        return
    return jsonify({"error": "Your role (Service Engineer) doesn't have access to this part of TEAMworks360."}), 403

# ---- Basic login throttling (in-memory; resets on restart) ----
# Slows down credential-guessing against the login endpoint. Keyed by
# (username, client IP) so one bad actor can't lock out a real user.
_LOGIN_ATTEMPTS = {}
LOGIN_MAX_ATTEMPTS = 8
LOGIN_LOCKOUT_SECONDS = 5 * 60
_DUMMY_HASH = generate_password_hash(secrets.token_hex(16))  # for timing-safe "user not found"


def _login_throttle_key():
    username = (request.get_json(silent=True) or {}).get("username", "")
    return f"{username.strip().lower()}|{request.remote_addr}"


def is_login_locked_out(key):
    entry = _LOGIN_ATTEMPTS.get(key)
    if not entry:
        return False
    count, first_attempt = entry
    if count < LOGIN_MAX_ATTEMPTS:
        return False
    if time.time() - first_attempt > LOGIN_LOCKOUT_SECONDS:
        _LOGIN_ATTEMPTS.pop(key, None)
        return False
    return True


def record_failed_login(key):
    count, first_attempt = _LOGIN_ATTEMPTS.get(key, (0, time.time()))
    _LOGIN_ATTEMPTS[key] = (count + 1, first_attempt)


def clear_login_attempts(key):
    _LOGIN_ATTEMPTS.pop(key, None)


def log_login_event(db, username, event_type):
    """Durable record of every login attempt - success, failure (bad
    credentials), or lockout - independent of _LOGIN_ATTEMPTS above, which
    is purely in-memory for throttling and resets on every restart. This
    is what actually lets an Administrator see, days later, who tried to
    sign in, from what IP, and whether they got in - e.g. to investigate a
    run of failed attempts against a particular account. Never pruned
    automatically, same as audit_log."""
    db.execute(
        "INSERT INTO login_events (username, ip_address, event_type) VALUES (?,?,?)",
        (username or "(blank)", request.remote_addr, event_type),
    )
    db.commit()


def generate_temporary_password():
    """A random one-time password for a newly created (or reset) account -
    used instead of a fixed default like "changeme123", which anyone could
    guess or reuse against a real account before its owner ever signs in.
    URL-safe base64, ~12 characters - plenty of entropy for something that
    must be changed on first use anyway."""
    return secrets.token_urlsafe(9)


# Endpoints a user with must_change_password set is still allowed to call -
# otherwise they'd be locked out of the one screen that lets them clear it.
_PASSWORD_GATE_EXEMPT_ENDPOINTS = {"change_password", "logout", "me"}

# Same idea for the two-factor-authentication gate below: the endpoints
# that get someone through 2FA setup or verification must stay reachable
# while must_setup_2fa/must_verify_2fa is set, or there'd be no way to ever
# clear it.
_2FA_GATE_EXEMPT_ENDPOINTS = {"logout", "me", "get_2fa_setup", "get_2fa_setup_qr", "confirm_2fa_setup", "verify_2fa"}

# ---- 2FA verification throttling (in-memory; resets on restart) ----
# Same shape as _LOGIN_ATTEMPTS above, kept as its own separate dict/key
# space rather than reusing that one - a wrong password and a wrong TOTP
# code are different failure modes and shouldn't share one lockout clock.
# Keyed by (user_id, client IP) since this only ever runs against an
# already-password-authenticated session.
_TOTP_ATTEMPTS = {}
TOTP_MAX_ATTEMPTS = 8
TOTP_LOCKOUT_SECONDS = 5 * 60


def _totp_throttle_key():
    return f"{session.get('user_id')}|{request.remote_addr}"


def is_totp_locked_out(key):
    entry = _TOTP_ATTEMPTS.get(key)
    if not entry:
        return False
    count, first_attempt = entry
    if count < TOTP_MAX_ATTEMPTS:
        return False
    if time.time() - first_attempt > TOTP_LOCKOUT_SECONDS:
        _TOTP_ATTEMPTS.pop(key, None)
        return False
    return True


def record_failed_totp(key):
    count, first_attempt = _TOTP_ATTEMPTS.get(key, (0, time.time()))
    _TOTP_ATTEMPTS[key] = (count + 1, first_attempt)


def clear_totp_attempts(key):
    _TOTP_ATTEMPTS.pop(key, None)


def generate_backup_codes(db, user_id, count=10):
    """Wipes any backup codes already on file for this user and issues a
    fresh set of `count`, returned here in plaintext exactly once - from
    this point on only each code's hash is stored (generate_password_hash,
    the same as password_hash), so even a full copy of the database can't
    be used to sign in with them. Called once, right when 2FA setup is
    confirmed."""
    db.execute("DELETE FROM totp_backup_codes WHERE user_id=?", (user_id,))
    codes = []
    for _ in range(count):
        code = totp.generate_backup_code()
        codes.append(code)
        db.execute(
            "INSERT INTO totp_backup_codes (user_id, code_hash) VALUES (?,?)",
            (user_id, generate_password_hash(code)),
        )
    db.commit()
    return codes


def verify_backup_code(db, user_id, code):
    """Checks `code` against every unused backup code on file for this
    user, consuming (marking used_date on) the one that matches so it
    can never be reused - the same one-time-use guarantee as the
    `used_date IS NULL` filter below enforces at the query level."""
    code = (code or "").strip().upper()
    if not code:
        return False
    rows = db.execute(
        "SELECT backup_code_id, code_hash FROM totp_backup_codes WHERE user_id=? AND used_date IS NULL",
        (user_id,),
    ).fetchall()
    for row in rows:
        if check_password_hash(row["code_hash"], code):
            db.execute(
                "UPDATE totp_backup_codes SET used_date=CURRENT_TIMESTAMP WHERE backup_code_id=?",
                (row["backup_code_id"],),
            )
            db.commit()
            return True
    return False


def log_document_access(db, document_type, reference):
    """Durable record of every PDF download and CSV export - who,
    which document, when - independent of audit_log (field-value edits)
    and login_events (sign-ins). Read via the admin-only
    /api/document-access-log endpoint. Never pruned automatically, same
    as those two."""
    db.execute(
        "INSERT INTO document_access_log (username, document_type, reference, ip_address) VALUES (?,?,?,?)",
        (current_user_name(), document_type, reference, request.remote_addr),
    )
    db.commit()


# ---- Security headers on every response (defense in depth) ----
# Content-Security-Policy: the HTML/JS here never builds inline <script>
# tags or inline event-handler attributes (every click/submit handler is
# wired with addEventListener in app.js) and the only cross-origin
# resource ever loaded is Chart.js from its CDN - so script-src can be
# strict. Inline style="..." attributes ARE used throughout app.js's
# dynamically-built HTML (a large, deliberate existing pattern, not
# something worth a risky rewrite just to drop 'unsafe-inline' from
# style-src) - that's a far smaller concession than allowing inline
# *scripts*, which is what actually matters for stopping an XSS payload
# from executing even if one ever slipped past the HTML-escaping in esc().
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' https://cdnjs.cloudflare.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    # Mirrors the CSRF token into a readable cookie on every response for
    # as long as a session exists, so app.js can always read it (via
    # document.cookie) and echo it back as the X-CSRF-Token header on
    # state-changing requests - see enforce_csrf() below. Re-setting the
    # same value every response is harmless and means a browser that
    # dropped the cookie (or never got it, e.g. an older session from
    # before this feature existed) picks it back up on its very next
    # request rather than staying broken until the next login.
    if session.get("csrf_token"):
        response.set_cookie(
            "csrf_token", session["csrf_token"], httponly=False, samesite="Lax",
            secure=os.environ.get("FLASK_HTTPS") == "1", path="/",
        )
    return response


# ---- CSRF protection on state-changing requests (defense in depth) ----
# SameSite=Lax on the session cookie already blocks the classic
# cross-site-form-POST CSRF attack in any reasonably modern browser, but
# it's one setting away from being silently weakened (someone changing it
# while debugging a cross-site integration, say) and doesn't cover every
# edge case across browser versions. This is a second, independent layer:
# a double-submit token, readable only by this app's own JavaScript (see
# the cookie-mirroring in set_security_headers above), which an attacker's
# page has no way to read or forge due to the browser's same-origin policy
# - so a forged cross-site request simply has no valid header to send.
# login/logout are exempt: login has no session yet to carry a token, and
# forging a login (making a victim's browser log in as someone else) has
# no real impact here since nothing sensitive is attached to that action.
_CSRF_EXEMPT_ENDPOINTS = {"login", "logout"}


@app.before_request
def ensure_csrf_token():
    """A session created before this feature existed - i.e. anyone who was
    already logged in when a server picked up this code - has no
    csrf_token at all yet, since the only place one is minted is inside
    login(). Without this, such a session would be stuck permanently
    rejected by enforce_csrf() below: no token server-side, so no cookie
    ever gets delivered to the browser either, and nothing short of an
    explicit logout/login would ever fix it - "refresh and try again"
    would be a lie. Mint one here instead, the first time such a session
    makes any request (a plain page-load GET is enough) - registered
    ahead of enforce_csrf so it always runs first and this request's own
    response already carries the new cookie via set_security_headers."""
    if "user_id" in session and not session.get("csrf_token"):
        session["csrf_token"] = secrets.token_urlsafe(32)


@app.before_request
def enforce_csrf():
    if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
        return
    if request.endpoint in _CSRF_EXEMPT_ENDPOINTS:
        return
    if "user_id" not in session:
        # No session at all - login_required/permission_required on the
        # actual route will reject this with a clean 401; don't mask that
        # behind an unrelated CSRF error.
        return
    token = session.get("csrf_token")
    header = request.headers.get("X-CSRF-Token")
    if not token or not header or not secrets.compare_digest(token, header):
        return jsonify({"error": "Your session's security token is missing or stale. "
                                  "Please refresh the page and try again."}), 403


@app.errorhandler(413)
def handle_too_large(e):
    # Flask's default 413 is an HTML error page - the frontend's api()
    # helper always expects JSON back, so without this a file that's too
    # big for MAX_CONTENT_LENGTH (see app.config, above) would surface as
    # a generic "Request failed" instead of a message that actually says
    # what happened.
    return jsonify({"error": "That upload is too large. Each Goods In document "
                              "attachment is limited to 25MB in total."}), 413


def safe_float(value, default=0.0):
    """Coerce request input to float without ever raising - malformed input
    (or someone probing the API) gets a clean fallback instead of an
    unhandled exception that could crash the request or (with debug mode
    misconfigured) leak a stack trace."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default=None):
    """Same idea as safe_float, for the Goods In multipart form fields
    (purchase_order_id, ship_to_id) - a plain JSON body hands these over
    already as ints, but multipart/form-data (needed to carry the uploaded
    file alongside them) only ever has strings."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# Quote numbering
# --------------------------------------------------------------------------
# Quote numbers are assigned automatically and sequentially rather than
# typed in, mirroring "the next folder in SharePoint" - QUOTE_NUMBER_START
# is the first number issued once real deployment begins (set to match
# where the team's own numbering had actually reached). In full deployment,
# this is the one function that would be swapped out to call the SharePoint
# API to create the next numbered folder instead of just incrementing a
# number locally - the same approach is intended to extend to Works Order
# and Purchase Order numbering at that point.
QUOTE_NUMBER_PREFIX = "TMQ-"
QUOTE_NUMBER_START = 3000


def get_next_quote_number(db):
    rows = db.execute("SELECT quote_number FROM quotes").fetchall()
    max_n = QUOTE_NUMBER_START - 1
    for r in rows:
        m = re.search(r"(\d+)\s*$", r["quote_number"] or "")
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"{QUOTE_NUMBER_PREFIX}{max_n + 1}"


# Despatch Note numbering follows the same auto-assigned, sequential
# pattern as quotes (see above) - a separate prefix/range so the two series
# never collide or get confused for one another.
DESPATCH_NUMBER_PREFIX = "TMD-"
DESPATCH_NUMBER_START = 4000


def get_next_despatch_number(db):
    rows = db.execute("SELECT despatch_number FROM despatch_notes").fetchall()
    max_n = DESPATCH_NUMBER_START - 1
    for r in rows:
        m = re.search(r"(\d+)\s*$", r["despatch_number"] or "")
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"{DESPATCH_NUMBER_PREFIX}{max_n + 1}"


# Works Order (job) and Purchase Order numbers are server-assigned too, so two
# people can never raise the same number and the sequence has no gaps or typos.
# Only numbers in the plain pattern count (TM-9595, PO-3301): odd historic
# references such as "PO-1234 PO-1235" or "PO-TM-8670" are ignored, as are the
# HIST-PO-* placeholder jobs.
WORKS_ORDER_PREFIX = "TM-"
WORKS_ORDER_START = 9000
PO_NUMBER_PREFIX = "PO-"
PO_NUMBER_START = 3000


def _next_plain_number(db, table, column, prefix, start):
    pattern = re.compile(r"^" + re.escape(prefix) + r"(\d+)$")
    max_n = start - 1
    for r in db.execute(f"SELECT {column} FROM {table}").fetchall():
        m = pattern.match((r[column] or "").strip())
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"{prefix}{max_n + 1}"


def number_sort_key(value):
    """Sort key so the HIGHEST number comes first when reversed: TM-10000 above TM-9595.
    Only plain numbers (PREFIX-digits) count; odd historic references such as
    "PO-TM-8670" or "PO-1 PO-2" sort to the bottom rather than jumping to the top."""
    m = re.match(r"^[A-Za-z]+-(\d+)$", (value or "").strip())
    return int(m.group(1)) if m else -1


def get_next_works_order_number(db):
    return _next_plain_number(db, "works_orders", "job_number", WORKS_ORDER_PREFIX, WORKS_ORDER_START)


def get_next_po_number(db):
    return _next_plain_number(db, "purchase_orders", "po_number", PO_NUMBER_PREFIX, PO_NUMBER_START)


# --------------------------------------------------------------------------
# DB helpers
# --------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        # timeout: how long a connection waits for a lock held by another
        # connection before giving up - matters now that more than one
        # person can genuinely be writing at once under shared hosting;
        # the SQLite default is 0 (fail immediately). WAL mode lets reads
        # continue while a write is in progress, the standard setting for
        # any SQLite database with concurrent access - a no-op to set
        # again on a database that already has it (the mode is stored in
        # the file itself), so it's safe to just always ask for it here.

        g.db = libsql.connect(
        database="libsql://jpm-vercel-icfg-1esii5ughllw7rk73nvltcfc.aws-us-east-1.turso.io", #os.environ["TURSO_DATABASE_URL"],
        auth_token="eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3OTI3NTYyNjQsImlkIjoiMDFhMTY4OTktYWMwMS03NjU5LTg4ZjUtOGIwYjc1NzQ2MWZmIiwia2lkIjoiRDVhakpBbDVwQlU1bjVzcV9vQWFGbU9meVQ3eVV3VV9Ib1VkclUtcHgtTSIsInJpZCI6IjRjNDM0OGJjLWI0ZGMtNDcxMi04YzI0LWQyMWZiZTFiM2M4YyJ9.bcDS1sjRyvzxg3Mn68MLjW0sCY9TqJPTOckwa06_LcCwRNjsBcMhysmJfVxjEYuxjDk70fCwo1guUFW6RxXAAg", #os.environ["TURSO_AUTH_TOKEN"],
        )
        # g.db = turso_serverless.connect(
        #    os.environ["TURSO_DATABASE_URL"],
        #    auth_token=os.environ["TURSO_AUTH_TOKEN"],
        # )
        # g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA journal_mode = WAL")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def rows_to_list(rows):
    return [dict(r) for r in rows]


def log_field_changes(db, table, record_id, old_row, new_values, fields, changed_by):
    """Writes one audit_log row for each field in `fields` whose value is
    actually changing, comparing `old_row` (fetched before the UPDATE) to
    `new_values` (the dict about to be written). Values are compared and
    stored as plain strings - simple, and good enough for the numbers,
    dates and statuses this covers. A record that doesn't exist yet
    (old_row is None, i.e. this is a create, not an update) has nothing to
    diff against, so nothing is logged - there's no "before" for a brand
    new record."""
    if old_row is None:
        return
    for field in fields:
        old_val = old_row[field] if field in old_row.keys() else None
        new_val = new_values.get(field)
        old_norm = "" if old_val is None else str(old_val)
        new_norm = "" if new_val is None else str(new_val)
        if old_norm != new_norm:
            db.execute(
                """INSERT INTO audit_log (table_name, record_id, field_name, old_value, new_value, changed_by)
                   VALUES (?,?,?,?,?,?)""",
                (table, record_id, field, old_val, new_val, changed_by),
            )


def get_audit_history(db, table, record_id):
    rows = db.execute(
        "SELECT * FROM audit_log WHERE table_name=? AND record_id=? ORDER BY audit_id DESC",
        (table, record_id),
    ).fetchall()
    return rows_to_list(rows)


# --------------------------------------------------------------------------
# Auth helpers
# --------------------------------------------------------------------------
def _revalidate_session_user(db):
    """Re-checks the session's user_id and role against the database on
    every request, instead of trusting the values cached in the session
    cookie at login time. Without this, deleting someone's account or
    changing their role has no effect on a browser tab they already have
    open until that session naturally expires (up to
    app.permanent_session_lifetime - currently 8 hours): a departing
    staff member whose account was just deleted, or someone just demoted
    out of Administrator, could keep using the old access for the rest
    of that window. Returns True if the session is still good to use
    (refreshing session["role"] in place if it's changed); returns False
    - after clearing the session outright - if the account is gone."""
    row = db.execute("SELECT role FROM users WHERE user_id=?", (session.get("user_id"),)).fetchone()
    if not row:
        session.clear()
        return False
    if row["role"] != session.get("role"):
        session["role"] = row["role"]
    return True


def _check_auth_gates():
    """Shared by login_required and permission_required below, so the
    session-revalidation and must_change_password/2FA gates are written
    once and can never drift between the two. Returns a Flask response
    to send back if the request must be rejected, or None if it's fine
    to let the wrapped route run."""
    if not _revalidate_session_user(get_db()):
        return jsonify({"error": "Your session is no longer valid - your account may have been removed "
                                  "or changed. Please sign in again."}), 401
    if session.get("must_change_password") and request.endpoint not in _PASSWORD_GATE_EXEMPT_ENDPOINTS:
        return jsonify({"error": "Password change required", "must_change_password": True}), 403
    if session.get("must_setup_2fa") and request.endpoint not in _2FA_GATE_EXEMPT_ENDPOINTS:
        return jsonify({"error": "Two-factor authentication setup required", "must_setup_2fa": True}), 403
    if session.get("must_verify_2fa") and request.endpoint not in _2FA_GATE_EXEMPT_ENDPOINTS:
        return jsonify({"error": "Two-factor authentication code required", "must_verify_2fa": True}), 403
    return None


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Authentication required"}), 401
        gate_response = _check_auth_gates()
        if gate_response is not None:
            return gate_response
        return fn(*args, **kwargs)
    return wrapper


def permission_required(perm):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if "user_id" not in session:
                return jsonify({"error": "Authentication required"}), 401
            gate_response = _check_auth_gates()
            if gate_response is not None:
                return gate_response
            role = session.get("role")
            perms = ROLE_PERMISSIONS.get(role, set())
            if "all" in perms or perm in perms:
                return fn(*args, **kwargs)
            return jsonify({"error": f"Role '{role}' is not permitted to perform this action"}), 403
        return wrapper
    return decorator


def current_user_name():
    """The logged-in user's display name, for stamping created_by/updated_by
    on records. Every route that calls this is already gated by
    login_required or permission_required, which both refuse the request
    before this could ever run without a valid session - so this is never
    guessing at who's making the request, only reading what the login
    already established."""
    return session.get("full_name") or session.get("username") or "Unknown"


# --------------------------------------------------------------------------
# Profitability engine
# --------------------------------------------------------------------------
def compute_job_metrics(db, wo):
    wo_id = wo["works_order_id"]

    revenue = db.execute(
        "SELECT COALESCE(SUM(invoice_amount),0) FROM customer_invoices "
        "WHERE works_order_id=? AND status != 'Cancelled'", (wo_id,)
    ).fetchone()[0]

    supplier_invoice_total = db.execute(
        "SELECT COALESCE(SUM(invoice_amount),0) FROM supplier_invoices "
        "WHERE works_order_id=? AND status != 'Cancelled'", (wo_id,)
    ).fetchone()[0]

    labour_total = db.execute(
        "SELECT COALESCE(SUM(labour_cost),0) FROM labour_entries WHERE works_order_id=?", (wo_id,)
    ).fetchone()[0]

    po_total = db.execute(
        "SELECT COALESCE(SUM(po_value),0) FROM purchase_orders "
        "WHERE works_order_id=? AND status != 'Cancelled' AND is_historic=0", (wo_id,)
    ).fetchone()[0]

    stock_cost = db.execute(
        """SELECT COALESCE(SUM(sa.quantity * si.purchase_cost), 0)
           FROM stock_allocations sa JOIN stock_items si ON si.stock_item_id = sa.stock_item_id
           WHERE sa.works_order_id=?""", (wo_id,)
    ).fetchone()[0]

    actual_cost = supplier_invoice_total + labour_total + stock_cost
    outstanding_commitment = max(0.0, po_total - supplier_invoice_total)
    forecast_cost = actual_cost + outstanding_commitment
    profit = revenue - actual_cost
    forecast_profit = revenue - forecast_cost
    margin_pct = (profit / revenue * 100) if revenue else 0.0
    forecast_margin_pct = (forecast_profit / revenue * 100) if revenue else 0.0

    margin_target = get_margin_target(db, wo["contract_code"] if "contract_code" in wo.keys() else None)

    alerts = []
    if actual_cost > wo["budget_cost"] and wo["budget_cost"] > 0:
        is_estimated = bool(wo["budget_is_estimated"]) if "budget_is_estimated" in wo.keys() else False
        if is_estimated:
            alerts.append({"level": "amber", "type": "Cost Above Estimated Target",
                            "message": f"Actual cost (£{actual_cost:,.2f}) is above the estimated target of "
                                       f"£{wo['budget_cost']:,.2f} (35% of invoice value - no confirmed budget was set for this job)"})
        else:
            alerts.append({"level": "red", "type": "Budget Exceeded",
                            "message": f"Actual cost (£{actual_cost:,.2f}) exceeds budget (£{wo['budget_cost']:,.2f})"})
    if revenue and forecast_margin_pct < margin_target:
        alerts.append({"level": "amber", "type": "Margin Risk",
                        "message": f"Forecast margin {forecast_margin_pct:.1f}% is below the {margin_target:.0f}% target"})

    # Over-invoiced PO check (per PO within this job). Excludes a Cancelled
    # PO itself, same as po_total/supplier_invoice_total above - a
    # cancelled PO is no longer a live commitment, so it shouldn't trigger
    # a "£X exceeds PO value - approval required" alert just because a
    # non-cancelled invoice happens to still reference it.
    over_invoiced_pos = db.execute(
        """SELECT po.po_number, po.po_value, COALESCE(SUM(si.invoice_amount),0) AS invoiced
           FROM purchase_orders po
           LEFT JOIN supplier_invoices si ON si.purchase_order_id = po.purchase_order_id AND si.status != 'Cancelled'
           WHERE po.works_order_id = ? AND po.status != 'Cancelled' AND po.is_historic = 0
           GROUP BY po.purchase_order_id
           HAVING invoiced > po.po_value""",
        (wo_id,),
    ).fetchall()
    for po in over_invoiced_pos:
        alerts.append({
            "level": "red", "type": "Over-Invoiced PO",
            "message": f"{po['po_number']}: invoiced £{po['invoiced']:,.2f} exceeds PO value £{po['po_value']:,.2f} - approval required",
        })

    if profit < 0:
        status_light = "red"
    elif margin_pct < margin_target:
        status_light = "amber"
    else:
        status_light = "green"

    return {
        "revenue": round(revenue, 2),
        "actual_cost": round(actual_cost, 2),
        "committed_cost": round(outstanding_commitment, 2),
        "forecast_cost": round(forecast_cost, 2),
        "profit": round(profit, 2),
        "forecast_profit": round(forecast_profit, 2),
        "margin_pct": round(margin_pct, 2),
        "forecast_margin_pct": round(forecast_margin_pct, 2),
        "margin_target": margin_target,
        "alerts": alerts,
        "status_light": status_light,
        "po_total": round(po_total, 2),
        "supplier_invoice_total": round(supplier_invoice_total, 2),
        "labour_total": round(labour_total, 2),
        "stock_cost": round(stock_cost, 2),
    }


# Everything about a job's money: hidden from Service Engineers entirely (not
# sent to their browser at all, so there is nothing to find in the page source).
JOB_FINANCIAL_FIELDS = (
    "contract_value", "budget_cost", "budget_is_estimated", "revenue", "actual_cost",
    "committed_cost", "forecast_cost", "profit", "forecast_profit", "margin_pct",
    "forecast_margin_pct", "margin_target", "alerts", "status_light", "po_total",
    "supplier_invoice_total", "labour_total", "stock_cost",
)


def is_service_engineer():
    # Also called from background/report code with no request behind it - then it's simply "no".
    return has_request_context() and session.get("role") == "Service Engineer"


def works_order_with_metrics(db, wo_row):
    wo = dict(wo_row)
    if is_service_engineer():
        # No profitability for Service Engineers - skip the calculation too.
        for k in JOB_FINANCIAL_FIELDS:
            wo.pop(k, None)
        return wo
    metrics = compute_job_metrics(db, wo_row)
    wo.update(metrics)
    return wo


def job_profit_margin_for_year(db, year):
    """Per-job Revenue/Cost/Profit/Margin scoped to ONE calendar year, by
    invoice/work date - a different cut from compute_job_metrics(), which is
    a job's whole-life total regardless of when that activity happened. Used
    only by the Dashboard's chart area (Top/Bottom by Profit, Profit vs
    Margin), which is deliberately "this year's performance," not a
    cumulative total that could span several years of a long-running job -
    see job_number TM-xxxx jobs that carry activity from more than one year.

    Stock cost is left out of this year-scoped total: stock_allocations
    records where a unit of stock currently sits, not a dated cost event, so
    there's no sound way to attribute it to one calendar year. A job's
    lifetime total (Works Orders, Reporting) still includes it in full -
    this is a Dashboard-only simplification, not a change to the real
    numbers used anywhere else.

    Only returns jobs with some revenue or cost actually dated in that year
    - a job with no activity this year has nothing to plot on a "this year"
    chart, however large its all-time total is."""
    yr = str(year)
    rows = db.execute(
        """SELECT wo.works_order_id, wo.job_number, wo.job_name, wo.contract_code,
                  COALESCE(rev.total, 0) AS revenue,
                  COALESCE(sup.total, 0) + COALESCE(lab.total, 0) AS actual_cost
           FROM works_orders wo
           LEFT JOIN (
               SELECT works_order_id, SUM(invoice_amount) AS total FROM customer_invoices
               WHERE strftime('%Y', invoice_date) = ? AND status != 'Cancelled'
               GROUP BY works_order_id
           ) rev ON rev.works_order_id = wo.works_order_id
           LEFT JOIN (
               SELECT works_order_id, SUM(invoice_amount) AS total FROM supplier_invoices
               WHERE strftime('%Y', invoice_date) = ? AND status != 'Cancelled'
               GROUP BY works_order_id
           ) sup ON sup.works_order_id = wo.works_order_id
           LEFT JOIN (
               SELECT works_order_id, SUM(labour_cost) AS total FROM labour_entries
               WHERE strftime('%Y', work_date) = ?
               GROUP BY works_order_id
           ) lab ON lab.works_order_id = wo.works_order_id
           WHERE COALESCE(rev.total, 0) != 0 OR COALESCE(sup.total, 0) != 0 OR COALESCE(lab.total, 0) != 0""",
        (yr, yr, yr),
    ).fetchall()

    result = []
    for r in rows:
        revenue = r["revenue"]
        actual_cost = r["actual_cost"]
        profit = revenue - actual_cost
        margin_pct = (profit / revenue * 100) if revenue else 0.0
        margin_target = get_margin_target(db, r["contract_code"])
        if profit < 0:
            status_light = "red"
        elif margin_pct < margin_target:
            status_light = "amber"
        else:
            status_light = "green"
        result.append({
            "job_number": r["job_number"], "job_name": r["job_name"],
            "revenue": round(revenue, 2), "actual_cost": round(actual_cost, 2),
            "profit": round(profit, 2), "margin_pct": round(margin_pct, 2),
            "margin_target": margin_target, "status_light": status_light,
        })
    return result


# --------------------------------------------------------------------------
# Static / page routes
# --------------------------------------------------------------------------
# A version string based on server start time, passed to every static asset
# URL as a query parameter. Browsers cache JS/CSS aggressively by default -
# without this, updating the app and restarting it could leave a browser
# tab silently running old JavaScript against the new backend. Since this
# changes every time the server restarts (which happens on every app
# update), it forces a fresh fetch instead of a stale cache hit.
STATIC_VERSION = str(int(time.time()))


@app.route("/")
def index():
    # True only when this process was started against a separate data
    # directory (see db_location.py's TEAMWORKS360_DATA_DIR override) -
    # i.e. the shared test copy, never a normal single-user or production
    # shared-hosting install. Drives the "TEST COPY" banner in the page
    # itself, so nobody using it can mistake it for the live system.
    is_test_copy = bool(os.environ.get("TEAMWORKS360_DATA_DIR"))
    return render_template("index.html", static_version=STATIC_VERSION, is_test_copy=is_test_copy,
                           app_owner=app_version.APP_OWNER, app_licence=app_version.APP_LICENCE)


# --------------------------------------------------------------------------
# Auth routes
# --------------------------------------------------------------------------
@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json(force=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    db = get_db()

    throttle_key = _login_throttle_key()
    if is_login_locked_out(throttle_key):
        log_login_event(db, username, "lockout")
        return jsonify({"error": "Too many failed attempts. Try again in a few minutes."}), 429

    user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    # Always run a hash check, even for an unknown username, against a dummy
    # hash - otherwise a nonexistent-user response returns faster than a
    # wrong-password one, letting an attacker enumerate valid usernames by
    # timing alone.
    password_ok = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, password)
    if not user or not password_ok:
        record_failed_login(throttle_key)
        log_login_event(db, username, "failure")
        return jsonify({"error": "Invalid username or password"}), 401

    clear_login_attempts(throttle_key)
    log_login_event(db, username, "success")
    session.clear()
    session.permanent = True
    session["user_id"] = user["user_id"]
    session["role"] = user["role"]
    session["full_name"] = user["full_name"]
    session["username"] = user["username"]
    session["must_change_password"] = bool(user["must_change_password"]) if "must_change_password" in user.keys() else False
    # Two-factor authentication is mandatory for every account. An account
    # that's never been through setup gets the setup screen; one that
    # already has it gets asked to verify a code on this new session -
    # every login needs a fresh code, a session never "remembers" having
    # passed 2FA before.
    totp_enabled = bool(user["totp_enabled"]) if "totp_enabled" in user.keys() else False
    session["must_setup_2fa"] = not totp_enabled
    session["must_verify_2fa"] = totp_enabled
    # A fresh CSRF token per login (never reused across sessions) - mirrored
    # into a readable cookie by set_security_headers() on this very
    # response, so the frontend can start sending it on the next
    # state-changing request immediately.
    session["csrf_token"] = secrets.token_urlsafe(32)
    return jsonify({"user_id": user["user_id"], "username": user["username"],
                     "full_name": user["full_name"], "role": user["role"],
                     "must_change_password": session["must_change_password"],
                     "must_setup_2fa": session["must_setup_2fa"],
                     "must_verify_2fa": session["must_verify_2fa"]})


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/auth/me")
def me():
    if "user_id" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    if not _revalidate_session_user(get_db()):
        return jsonify({"error": "Not authenticated"}), 401
    return jsonify({"user_id": session["user_id"], "username": session["username"],
                     "full_name": session["full_name"], "role": session["role"],
                     "must_change_password": bool(session.get("must_change_password")),
                     "must_setup_2fa": bool(session.get("must_setup_2fa")),
                     "must_verify_2fa": bool(session.get("must_verify_2fa"))})


@app.route("/api/auth/change-password", methods=["POST"])
@login_required
def change_password():
    data = request.get_json(force=True) or {}
    current_password = data.get("current_password", "")
    new_password = data.get("new_password", "")
    if len(new_password) < 8:
        return jsonify({"error": "New password must be at least 8 characters."}), 400
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE user_id=?", (session["user_id"],)).fetchone()
    if not user or not check_password_hash(user["password_hash"], current_password):
        return jsonify({"error": "Current password is incorrect."}), 400
    db.execute(
        "UPDATE users SET password_hash=?, must_change_password=0 WHERE user_id=?",
        (generate_password_hash(new_password), session["user_id"]),
    )
    db.commit()
    session["must_change_password"] = False
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Two-factor authentication (TOTP) - mandatory for every account (see
# totp.py for the algorithm itself). login() above sets must_setup_2fa for
# an account that's never been through setup, or must_verify_2fa for one
# that has but hasn't confirmed a code on this particular session yet;
# login_required/permission_required block every other endpoint while
# either is set (see _check_auth_gates()).
# --------------------------------------------------------------------------
def _pending_totp_secret(db, user):
    """The secret currently pending setup for this user - generating one
    the first time it's needed, and reusing it on every later call until
    setup is actually confirmed, so reloading the setup screen (or its QR
    code image) never invalidates a key already typed or scanned into an
    authenticator app. Shared by get_2fa_setup and get_2fa_setup_qr below
    so both always agree on the same secret."""
    secret = user["totp_secret"]
    if not secret:
        secret = totp.generate_secret()
        db.execute("UPDATE users SET totp_secret=? WHERE user_id=?", (secret, user["user_id"]))
        db.commit()
    return secret


@app.route("/api/auth/2fa/setup", methods=["GET"])
@login_required
def get_2fa_setup():
    """Returns the manual setup key (and otpauth:// link) for whichever
    secret is currently pending for this user."""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE user_id=?", (session["user_id"],)).fetchone()
    if user["totp_enabled"]:
        return jsonify({"error": "Two-factor authentication is already set up for this account."}), 400
    secret = _pending_totp_secret(db, user)
    return jsonify({
        "manual_entry_key": totp.format_secret_for_display(secret),
        "otpauth_uri": totp.provisioning_uri(secret, user["username"]),
        "issuer": totp.ISSUER,
    })


@app.route("/api/auth/2fa/setup/qr.png", methods=["GET"])
@login_required
def get_2fa_setup_qr():
    """A QR code image of the same otpauth:// link get_2fa_setup() returns
    as text, for the authenticator apps that can add an account by
    scanning instead of typing the setup key - a convenience alongside
    the manual key, not a replacement for it (see totp.py's module
    docstring for why manual entry is the primary path)."""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE user_id=?", (session["user_id"],)).fetchone()
    if user["totp_enabled"]:
        return Response("Two-factor authentication is already set up for this account.", status=400)
    secret = _pending_totp_secret(db, user)
    uri = totp.provisioning_uri(secret, user["username"])
    png_bytes = qr_png.render_png(uri)
    return Response(png_bytes, mimetype="image/png",
                     headers={"Cache-Control": "no-store"})


@app.route("/api/auth/2fa/setup", methods=["POST"])
@login_required
def confirm_2fa_setup():
    """Confirms the pending secret actually works - the user's
    authenticator app produces a code that matches it - before switching
    totp_enabled on. This is what stops a mistyped setup key from locking
    someone out the moment 2FA becomes mandatory on their very next
    request: until this succeeds, get_2fa_setup() above just keeps
    returning the same pending key to try again."""
    data = request.get_json(force=True) or {}
    code = data.get("code", "")
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE user_id=?", (session["user_id"],)).fetchone()
    if user["totp_enabled"]:
        return jsonify({"error": "Two-factor authentication is already set up for this account."}), 400
    if not user["totp_secret"]:
        return jsonify({"error": "Start setup first to get a setup key."}), 400
    if not totp.verify(user["totp_secret"], code):
        return jsonify({"error": "That code didn't match. Check the time on your phone and try again."}), 400
    db.execute("UPDATE users SET totp_enabled=1 WHERE user_id=?", (session["user_id"],))
    db.commit()
    backup_codes = generate_backup_codes(db, session["user_id"])
    session["must_setup_2fa"] = False
    session["must_verify_2fa"] = False
    return jsonify({"ok": True, "backup_codes": backup_codes})


@app.route("/api/auth/2fa/verify", methods=["POST"])
@login_required
def verify_2fa():
    """The regular-login path for someone who already has 2FA set up:
    checks a 6-digit authenticator code, or (is_backup_code: true) a
    one-time recovery code for when they can't get to their
    authenticator app at all. Throttled the same shape as the password
    login itself, keyed by session rather than username/IP since this
    only ever runs against an already-password-authenticated session."""
    data = request.get_json(force=True) or {}
    code = data.get("code", "")
    is_backup = bool(data.get("is_backup_code"))
    key = _totp_throttle_key()
    if is_totp_locked_out(key):
        return jsonify({"error": "Too many failed attempts. Try again in a few minutes."}), 429
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE user_id=?", (session["user_id"],)).fetchone()
    if not user["totp_enabled"] or not user["totp_secret"]:
        return jsonify({"error": "Two-factor authentication isn't set up for this account."}), 400
    ok = verify_backup_code(db, session["user_id"], code) if is_backup else totp.verify(user["totp_secret"], code)
    if not ok:
        record_failed_totp(key)
        return jsonify({"error": "That code wasn't recognised."}), 400
    clear_totp_attempts(key)
    session["must_verify_2fa"] = False
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Audit log - who changed a financially significant field, from what, to
# what, and when. Read-only: rows are only ever written by
# log_field_changes(), called from each covered record's update endpoint.
# --------------------------------------------------------------------------
AUDIT_LOG_TABLES = {
    "works_orders", "quotes", "purchase_orders",
    "customer_invoices", "supplier_invoices", "labour_entries",
}


@app.route("/api/audit-log/<table>/<int:record_id>")
@login_required
def get_audit_log_route(table, record_id):
    if table not in AUDIT_LOG_TABLES:
        return jsonify({"error": "Unknown table"}), 400
    db = get_db()
    history = get_audit_history(db, table, record_id)
    if is_service_engineer():
        # Changes to a job's value/budget stay out of an engineer's view of the history.
        hidden = {"contract_value", "budget_cost"}
        history = [h for h in history if h.get("field_name") not in hidden]
    return jsonify(history)


# --------------------------------------------------------------------------
# Login activity (Administrator only) - the durable audit trail written by
# log_login_event(), as opposed to _LOGIN_ATTEMPTS, which only ever drives
# the in-memory throttling and has nothing to show an admin after the fact.
# --------------------------------------------------------------------------
@app.route("/api/login-events")
@permission_required("all")
def list_login_events():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM login_events ORDER BY event_id DESC LIMIT 200"
    ).fetchall()
    return jsonify(rows_to_list(rows))


# --------------------------------------------------------------------------
# Document access log (Administrator only) - the durable audit trail
# written by log_document_access(), called from every PDF-download and
# CSV-export route below. Distinct from login_events (who got in) and
# audit_log (what field values changed) - this is who READ or EXPORTED
# which document, and when.
# --------------------------------------------------------------------------
@app.route("/api/document-access-log")
@permission_required("all")
def list_document_access_log():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM document_access_log ORDER BY access_id DESC LIMIT 200"
    ).fetchall()
    return jsonify(rows_to_list(rows))


# --------------------------------------------------------------------------
# Quotes
# --------------------------------------------------------------------------
# Every quote PDF states "valid for 30 days from the date of issue" (see
# quote_pdf.py's standard terms) - this is what backs that up: a quote left
# without an explicit Valid Until date gets one 30 days after its quote
# date automatically, so the printed terms and the stored record never
# drift apart, and still-open quotes that have run past it get flagged
# rather than silently sitting there looking current.
QUOTE_VALIDITY_DAYS = 30
QUOTE_OPEN_STATUSES = ("Draft", "Sent")

# The Purchase Order Register import files POs for jobs that aren't in the app
# on one placeholder job per customer, called HIST-PO-<CUSTOMER>. They exist only
# so those historic POs have somewhere to sit, so they're kept out of the Works
# Orders list, the Dashboard and the Reporting totals.
PLACEHOLDER_JOB_SQL = "wo.job_number NOT LIKE 'HIST-PO-%'"


def default_valid_until(quote_date_str):
    try:
        base = date.fromisoformat((quote_date_str or "")[:10])
    except ValueError:
        base = date.today()
    return (base + timedelta(days=QUOTE_VALIDITY_DAYS)).isoformat()


def is_past_validity(valid_until, status):
    if status not in QUOTE_OPEN_STATUSES or not valid_until:
        return False
    try:
        return date.fromisoformat(valid_until[:10]) < date.today()
    except ValueError:
        return False


@app.route("/api/quotes", methods=["GET"])
@login_required
def list_quotes():
    db = get_db()
    rows = db.execute(
        """SELECT q.*, c.customer_name, wo.job_number AS linked_job_number FROM quotes q
           LEFT JOIN customers c ON c.customer_id = q.customer_id
           LEFT JOIN works_orders wo ON wo.works_order_id = q.works_order_id
           ORDER BY q.quote_id DESC"""
    ).fetchall()
    # Highest quote number at the top (TMQ-3001 above TMQ-3000).
    rows = sorted(rows, key=lambda r: number_sort_key(r["quote_number"]), reverse=True)
    quotes = rows_to_list(rows)
    for q in quotes:
        q["is_past_validity"] = is_past_validity(q.get("valid_until"), q.get("status"))
    return jsonify(quotes)


@app.route("/api/quotes/next-number")
@login_required
def next_quote_number():
    db = get_db()
    return jsonify({"quote_number": get_next_quote_number(db)})


@app.route("/api/quotes", methods=["POST"])
@permission_required("quotes:write")
def create_quote():
    data = request.get_json(force=True) or {}
    db = get_db()
    # The quote number is always server-assigned - never taken from the
    # request - so it stays a reliable, gapless sequence regardless of what
    # a client sends.
    quote_number = get_next_quote_number(db)
    valid_until = data.get("valid_until") or default_valid_until(data.get("quote_date"))
    try:
        cur = db.execute(
            """INSERT INTO quotes
               (quote_number, customer_id, description, owner, status, estimated_value, quote_date, valid_until, notes, sharepoint_url,
                contact_name, your_ref, works_order_ref, works_order_id, scope_items, delivery_terms, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (quote_number, data.get("customer_id") or None, data.get("description"), data.get("owner"),
             data.get("status", "Draft"), safe_float(data.get("estimated_value")),
             data.get("quote_date"), valid_until, data.get("notes"), data.get("sharepoint_url"),
             data.get("contact_name"), data.get("your_ref"), data.get("works_order_ref"), data.get("works_order_id") or None,
             data.get("scope_items"), data.get("delivery_terms"), current_user_name()),
        )
        quote_id = cur.lastrowid

        # Optional cost buildup from the New Quote wizard - a plain quote
        # created without these fields behaves exactly as before.
        for line in data.get("labour_lines") or []:
            std_hours = safe_float(line.get("standard_hours"))
            ot_hours = safe_float(line.get("ot_hours"))
            if std_hours <= 0 and ot_hours <= 0:
                continue  # skip rate-card rows the user left blank
            db.execute(
                """INSERT INTO quote_labour_lines
                   (quote_id, category, activity, standard_rate, ot_rate, standard_hours, ot_hours)
                   VALUES (?,?,?,?,?,?,?)""",
                (quote_id, line.get("category"), line.get("activity"),
                 safe_float(line.get("standard_rate")), safe_float(line.get("ot_rate")), std_hours, ot_hours),
            )
        for line in data.get("purchase_lines") or []:
            if not (line.get("description") or safe_float(line.get("purchase_amount")) > 0):
                continue  # skip fully-blank rows
            db.execute(
                """INSERT INTO quote_purchase_lines
                   (quote_id, invoice_no, line_date, supplier, description, quantity, purchase_amount, sales_value)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (quote_id, line.get("invoice_no"), line.get("line_date"), line.get("supplier"),
                 line.get("description"), safe_float(line.get("quantity")),
                 safe_float(line.get("purchase_amount")), safe_float(line.get("sales_value"))),
            )
        db.commit()
    except sqlite3.IntegrityError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"quote_id": cur.lastrowid, "quote_number": quote_number}), 201


@app.route("/api/quotes/<int:qid>/cost-breakdown")
@login_required
def get_quote_cost_breakdown(qid):
    db = get_db()
    labour = db.execute(
        "SELECT * FROM quote_labour_lines WHERE quote_id=? ORDER BY line_id", (qid,)
    ).fetchall()
    purchases = db.execute(
        "SELECT * FROM quote_purchase_lines WHERE quote_id=? ORDER BY line_id", (qid,)
    ).fetchall()
    return jsonify({
        "labour_lines": rows_to_list(labour),
        "purchase_lines": rows_to_list(purchases),
    })


@app.route("/api/quotes/<int:qid>/pdf")
@login_required
def get_quote_pdf(qid):
    db = get_db()
    quote = db.execute(
        """SELECT q.*, c.customer_name, c.address AS customer_address, c.contact_name AS customer_contact_name
           FROM quotes q LEFT JOIN customers c ON c.customer_id = q.customer_id
           WHERE q.quote_id=?""",
        (qid,),
    ).fetchone()
    if not quote:
        return jsonify({"error": "Quote not found"}), 404
    pdf_bytes = build_quote_pdf(quote)
    log_document_access(db, "Quote PDF", quote["quote_number"])
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={quote['quote_number']}.pdf"},
    )


@app.route("/api/rate-card")
@login_required
def get_rate_card():
    db = get_db()
    rows = db.execute("SELECT * FROM rate_card ORDER BY sort_order").fetchall()
    return jsonify(rows_to_list(rows))


@app.route("/api/quotes/<int:qid>", methods=["PUT"])
@permission_required("quotes:write")
def update_quote(qid):
    data = request.get_json(force=True) or {}
    db = get_db()
    old = db.execute("SELECT * FROM quotes WHERE quote_id=?", (qid,)).fetchone()
    valid_until = data.get("valid_until") or default_valid_until(data.get("quote_date"))
    log_field_changes(db, "quotes", qid, old,
                       {"status": data.get("status"), "estimated_value": safe_float(data.get("estimated_value"))},
                       ["status", "estimated_value"], current_user_name())
    # quote_number is intentionally not accepted here - it's fixed once
    # assigned, the same way a SharePoint folder name wouldn't be renamed
    # just because the record was edited.
    db.execute(
        """UPDATE quotes SET customer_id=?, description=?, owner=?, status=?,
           estimated_value=?, quote_date=?, valid_until=?, notes=?, sharepoint_url=?,
           contact_name=?, your_ref=?, works_order_ref=?, works_order_id=?, scope_items=?, delivery_terms=?,
           updated_by=?, updated_date=datetime('now') WHERE quote_id=?""",
        (data.get("customer_id") or None, data.get("description"), data.get("owner"),
         data.get("status"), safe_float(data.get("estimated_value")),
         data.get("quote_date"), valid_until, data.get("notes"), data.get("sharepoint_url"),
         data.get("contact_name"), data.get("your_ref"), data.get("works_order_ref"), data.get("works_order_id") or None,
         data.get("scope_items"), data.get("delivery_terms"), current_user_name(), qid),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/quotes/<int:qid>", methods=["DELETE"])
@permission_required("all")
def delete_quote(qid):
    db = get_db()
    db.execute("DELETE FROM quotes WHERE quote_id=?", (qid,))
    db.commit()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Customers
# --------------------------------------------------------------------------
@app.route("/api/customers", methods=["GET"])
@login_required
def list_customers():
    db = get_db()
    return jsonify(rows_to_list(db.execute("SELECT * FROM customers ORDER BY customer_name").fetchall()))


@app.route("/api/customers", methods=["POST"])
@permission_required("all")
def create_customer():
    data = request.get_json(force=True) or {}
    db = get_db()
    cur = db.execute(
        "INSERT INTO customers (customer_name, address, contact_name, email, phone, created_by, created_date) VALUES (?,?,?,?,?,?,datetime('now'))",
        (data.get("customer_name"), data.get("address"), data.get("contact_name"), data.get("email"), data.get("phone"), current_user_name()),
    )
    db.commit()
    return jsonify({"customer_id": cur.lastrowid}), 201


@app.route("/api/customers/<int:cid>", methods=["PUT"])
@permission_required("all")
def update_customer(cid):
    data = request.get_json(force=True) or {}
    db = get_db()
    db.execute(
        "UPDATE customers SET customer_name=?, address=?, contact_name=?, email=?, phone=?, updated_by=?, updated_date=datetime('now') WHERE customer_id=?",
        (data.get("customer_name"), data.get("address"), data.get("contact_name"), data.get("email"), data.get("phone"), current_user_name(), cid),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/customers/<int:cid>", methods=["DELETE"])
@permission_required("all")
def delete_customer(cid):
    db = get_db()
    try:
        db.execute("DELETE FROM customers WHERE customer_id=?", (cid,))
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "This customer has Works Orders linked to it and cannot be deleted."}), 400
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Suppliers
# --------------------------------------------------------------------------
@app.route("/api/suppliers", methods=["GET"])
@login_required
def list_suppliers():
    db = get_db()
    return jsonify(rows_to_list(db.execute("SELECT * FROM suppliers ORDER BY supplier_name").fetchall()))


@app.route("/api/suppliers", methods=["POST"])
@permission_required("all")
def create_supplier():
    data = request.get_json(force=True) or {}
    db = get_db()
    cur = db.execute(
        """INSERT INTO suppliers (supplier_name, supply_of, reason_for_use, authorised_by, address, contact_name, email, vat_number, created_by, created_date)
           VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))""",
        (data.get("supplier_name"), data.get("supply_of"), data.get("reason_for_use"), data.get("authorised_by"),
         data.get("address"), data.get("contact_name"), data.get("email"), data.get("vat_number"), current_user_name()),
    )
    db.commit()
    return jsonify({"supplier_id": cur.lastrowid}), 201


@app.route("/api/suppliers/<int:sid>", methods=["PUT"])
@permission_required("all")
def update_supplier(sid):
    data = request.get_json(force=True) or {}
    db = get_db()
    db.execute(
        """UPDATE suppliers SET supplier_name=?, supply_of=?, reason_for_use=?, authorised_by=?,
           address=?, contact_name=?, email=?, vat_number=?, updated_by=?, updated_date=datetime('now') WHERE supplier_id=?""",
        (data.get("supplier_name"), data.get("supply_of"), data.get("reason_for_use"), data.get("authorised_by"),
         data.get("address"), data.get("contact_name"), data.get("email"), data.get("vat_number"), current_user_name(), sid),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/suppliers/<int:sid>", methods=["DELETE"])
@permission_required("all")
def delete_supplier(sid):
    db = get_db()
    try:
        db.execute("DELETE FROM suppliers WHERE supplier_id=?", (sid,))
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "This supplier has Purchase Orders linked to it and cannot be deleted."}), 400
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Works Orders
# --------------------------------------------------------------------------
@app.route("/api/works-orders", methods=["GET"])
@login_required
def list_works_orders():
    db = get_db()
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    due_month = request.args.get("due_month", "").strip()  # YYYY-MM, matches the Dashboard's due_month_keys
    location = request.args.get("location", "").strip()
    contract_code = request.args.get("contract_code", "").strip()
    late_only = request.args.get("late", "").strip() == "1"
    end_year = request.args.get("end_year", "").strip()   # YYYY, used by the Completed year to date panel
    sql = f"""SELECT wo.*, c.customer_name FROM works_orders wo
              JOIN customers c ON c.customer_id = wo.customer_id WHERE {PLACEHOLDER_JOB_SQL}"""
    params = []
    if q:
        sql += " AND (wo.job_number LIKE ? OR wo.job_name LIKE ? OR c.customer_name LIKE ? OR wo.project_manager LIKE ?)"
        params += [f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"]
    if location:
        sql += " AND wo.location = ?"
        params.append(location)
    if contract_code:
        sql += " AND wo.contract_code = ?"
        params.append(contract_code)
    if end_year:
        sql += " AND substr(wo.end_date, 1, 4) = ?"
        params.append(end_year)
    if late_only:
        # Same definition as the Dashboard's Late Works Orders panel: live
        # (Open / In Progress) with an End Date before today.
        sql += (" AND wo.status IN ('Open', 'In Progress') AND wo.end_date IS NOT NULL"
                " AND wo.end_date != '' AND substr(wo.end_date, 1, 10) < ?")
        params.append(date.today().isoformat())
    if due_month:
        # Same "year and month of End Date" match the Dashboard's own Due
        # cards use - substr rather than a date function, since end_date
        # is plain TEXT and this needs to match however it's actually
        # stored, not assume a fully-valid SQLite date string.
        sql += " AND substr(wo.end_date, 1, 7) = ?"
        params.append(due_month)
    if status == "Active":
        # Not a real status value in its own right - a shorthand for the
        # same "Open or In Progress" grouping the Dashboard's Active
        # Jobs/Live Works Orders figures already use (see active_statuses
        # in get_dashboard), so clicking through from there lands on
        # exactly the same set of jobs the number represented.
        sql += " AND wo.status IN ('Open', 'In Progress')"
    elif status:
        sql += " AND wo.status = ?"
        params.append(status)
    sql += " ORDER BY wo.created_date DESC"
    rows = db.execute(sql, params).fetchall()
    # Highest job number at the top (TM-9596 above TM-9595).
    rows = sorted(rows, key=lambda r: number_sort_key(r["job_number"]), reverse=True)
    result = [works_order_with_metrics(db, r) for r in rows]
    return jsonify(result)


@app.route("/api/works-orders/<int:wid>", methods=["GET"])
@login_required
def get_works_order(wid):
    db = get_db()
    row = db.execute(
        """SELECT wo.*, c.customer_name FROM works_orders wo
           JOIN customers c ON c.customer_id = wo.customer_id WHERE wo.works_order_id=?""",
        (wid,),
    ).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(works_order_with_metrics(db, row))


@app.route("/api/works-orders/next-number")
@login_required
def next_works_order_number():
    return jsonify({"job_number": get_next_works_order_number(get_db())})


@app.route("/api/works-orders", methods=["POST"])
@permission_required("works_orders:write")
def create_works_order():
    data = request.get_json(force=True) or {}
    if is_service_engineer():
        data["contract_value"] = 0
        data["budget_cost"] = 0
    db = get_db()
    # The job number is always server-assigned - never taken from the request.
    # (Retries cover the rare case of two people saving at the same instant.)
    for attempt in range(5):
        job_number = get_next_works_order_number(db)
        try:
            cur = db.execute(
                """INSERT INTO works_orders
                   (job_number, job_name, customer_id, project_manager, status, start_date, end_date, contract_value, budget_cost, budget_is_estimated, sharepoint_url, contract_code, location, created_by)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_number, data.get("job_name"), data.get("customer_id"), data.get("project_manager"),
                 data.get("status", "Draft"), data.get("start_date"), data.get("end_date"),
                 safe_float(data.get("contract_value")), safe_float(data.get("budget_cost")), 0, data.get("sharepoint_url"),
                 data.get("contract_code") or None, data.get("location") or None, current_user_name()),
            )
            db.commit()
            break
        except sqlite3.IntegrityError as e:
            db.rollback()
            if "job_number" in str(e) and attempt < 4:
                continue
            return jsonify({"error": str(e)}), 400
    return jsonify({"works_order_id": cur.lastrowid, "job_number": job_number}), 201


@app.route("/api/works-orders/<int:wid>", methods=["PUT"])
@permission_required("works_orders:write")
def update_works_order(wid):
    data = request.get_json(force=True) or {}
    db = get_db()
    old = db.execute("SELECT * FROM works_orders WHERE works_order_id=?", (wid,)).fetchone()
    if not old:
        return jsonify({"error": "Not found"}), 404
    if is_service_engineer():
        # They never see the money fields, so whatever the job already has stays exactly as it is.
        data["contract_value"] = old["contract_value"]
        data["budget_cost"] = old["budget_cost"]
        keep_estimated = old["budget_is_estimated"]
    else:
        keep_estimated = 0
    log_field_changes(db, "works_orders", wid, old,
                       {"status": data.get("status"), "contract_value": safe_float(data.get("contract_value")),
                        "budget_cost": safe_float(data.get("budget_cost"))},
                       ["status", "contract_value", "budget_cost"], current_user_name())
    db.execute(
        """UPDATE works_orders SET job_number=?, job_name=?, customer_id=?, project_manager=?, status=?,
           start_date=?, end_date=?, contract_value=?, budget_cost=?, budget_is_estimated=?, sharepoint_url=?, contract_code=?,
           location=?, updated_by=?, updated_date=datetime('now') WHERE works_order_id=?""",
        (old["job_number"], data.get("job_name"), data.get("customer_id"), data.get("project_manager"),
         data.get("status"), data.get("start_date"), data.get("end_date"),
         safe_float(data.get("contract_value")), safe_float(data.get("budget_cost")), keep_estimated, data.get("sharepoint_url"),
         data.get("contract_code") or None, data.get("location") or None, current_user_name(), wid),
    )
    db.commit()
    return jsonify({"ok": True})


def delete_works_order_records(db, wid):
    """Deletes a Works Order and everything that hangs off it: its purchase
    orders (with their goods-in records and uploaded documents), supplier and
    customer invoices, labour entries and despatch notes (with signatures).
    Quotes that were converted to this job are kept - they just lose the link.
    Stock movement history is kept too, minus the link to the deleted job.
    Does NOT commit. Returns (goods_in_files, signature_files) for the caller
    to remove from disk once the commit has happened. Raises ValueError (with a
    plain-English message) if stock is still allocated to the job: stock is
    never silently destroyed - it has to be moved back via Inventory first."""
    held = db.execute(
        """SELECT s.part_number, a.quantity FROM stock_allocations a
           JOIN stock_items s ON s.stock_item_id = a.stock_item_id
           WHERE a.works_order_id=?""", (wid,)).fetchall()
    if held:
        items = ", ".join(f"{r['part_number']} x {r['quantity']:g}" for r in held)
        raise ValueError(f"Stock is still allocated to this works order ({items}). "
                         "On the Inventory page, move it back to a site (or remove the allocation), then delete the works order.")
    files_goods, files_sigs = [], []
    po_ids = [r["purchase_order_id"] for r in db.execute(
        "SELECT purchase_order_id FROM purchase_orders WHERE works_order_id=?", (wid,))]
    for pid in po_ids:
        gids = [r["goods_in_id"] for r in db.execute(
            "SELECT goods_in_id FROM goods_in WHERE purchase_order_id=?", (pid,))]
        for gid in gids:
            files_goods += [d["stored_filename"] for d in db.execute(
                "SELECT stored_filename FROM goods_in_documents WHERE goods_in_id=?", (gid,))]
            db.execute("DELETE FROM goods_in WHERE goods_in_id=?", (gid,))
    files_sigs = [d["signature_filename"] for d in db.execute(
        "SELECT signature_filename FROM despatch_notes WHERE works_order_id=? AND signature_filename IS NOT NULL", (wid,))]
    db.execute("DELETE FROM despatch_notes WHERE works_order_id=?", (wid,))
    db.execute("DELETE FROM supplier_invoices WHERE works_order_id=?", (wid,))
    db.execute("DELETE FROM supplier_invoices WHERE purchase_order_id IN "
               "(SELECT purchase_order_id FROM purchase_orders WHERE works_order_id=?)", (wid,))
    db.execute("DELETE FROM purchase_orders WHERE works_order_id=?", (wid,))
    db.execute("DELETE FROM labour_entries WHERE works_order_id=?", (wid,))
    db.execute("DELETE FROM customer_invoices WHERE works_order_id=?", (wid,))
    db.execute("UPDATE quotes SET works_order_id=NULL WHERE works_order_id=?", (wid,))
    db.execute("UPDATE stock_movements SET from_works_order_id=NULL WHERE from_works_order_id=?", (wid,))
    db.execute("UPDATE stock_movements SET to_works_order_id=NULL WHERE to_works_order_id=?", (wid,))
    db.execute("DELETE FROM works_orders WHERE works_order_id=?", (wid,))
    return files_goods, files_sigs


@app.route("/api/works-orders/<int:wid>", methods=["DELETE"])
@permission_required("all")
def delete_works_order(wid):
    db = get_db()
    if not db.execute("SELECT 1 FROM works_orders WHERE works_order_id=?", (wid,)).fetchone():
        return jsonify({"error": "Works order not found"}), 404
    try:
        files_goods, files_sigs = delete_works_order_records(db, wid)
        db.commit()
    except ValueError as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400
    except sqlite3.IntegrityError as e:
        db.rollback()
        return jsonify({"error": f"This works order is still linked to other records and could not be deleted ({e})."}), 400
    for fn in files_goods:
        try:
            os.remove(_goods_in_doc_path(fn))
        except OSError:
            pass
    for fn in files_sigs:
        try:
            os.remove(_despatch_signature_path(fn))
        except OSError:
            pass
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Purchase Orders
# --------------------------------------------------------------------------
def purchase_order_lines(db, pid):
    rows = db.execute(
        "SELECT * FROM purchase_order_lines WHERE purchase_order_id=? ORDER BY line_id", (pid,)
    ).fetchall()
    return rows_to_list(rows)


def save_purchase_order_lines(db, pid, lines):
    """Replace-all: deletes any existing lines for this PO and inserts the
    new set. Returns the computed subtotal (sum of qty * unit_price) so the
    caller can keep po_value in sync with what the lines actually add up
    to, rather than it being a second, independently-editable figure that
    could drift out of sync."""
    db.execute("DELETE FROM purchase_order_lines WHERE purchase_order_id=?", (pid,))
    subtotal = 0.0
    for ln in lines or []:
        qty = safe_float(ln.get("qty"), 1) or 1
        unit_price = safe_float(ln.get("unit_price"))
        subtotal += qty * unit_price
        db.execute(
            """INSERT INTO purchase_order_lines (purchase_order_id, qty, part_number, item_number, description, unit_price)
               VALUES (?,?,?,?,?,?)""",
            (pid, qty, ln.get("part_number"), ln.get("item_number"), ln.get("description"), unit_price),
        )
    return subtotal


@app.route("/api/purchase-orders", methods=["GET"])
@login_required
def list_purchase_orders():
    db = get_db()
    rows = db.execute(
        """SELECT po.*, s.supplier_name, wo.job_number, wo.job_name, sta.label AS ship_to_label,
                  COALESCE((SELECT SUM(invoice_amount) FROM supplier_invoices si
                            WHERE si.purchase_order_id = po.purchase_order_id AND si.status != 'Cancelled'), 0) AS invoiced_total
           FROM purchase_orders po
           JOIN suppliers s ON s.supplier_id = po.supplier_id
           LEFT JOIN works_orders wo ON wo.works_order_id = po.works_order_id
           LEFT JOIN ship_to_addresses sta ON sta.ship_to_id = po.ship_to_id
           ORDER BY po.order_date DESC"""
    ).fetchall()
    # Highest PO number at the top (PO-3302 above PO-3301).
    rows = sorted(rows, key=lambda r: (number_sort_key(r["po_number"]), r["purchase_order_id"]), reverse=True)
    result = rows_to_list(rows)
    for r in result:
        r["lines"] = purchase_order_lines(db, r["purchase_order_id"])
    return jsonify(result)


@app.route("/api/purchase-orders/next-number")
@login_required
def next_po_number():
    return jsonify({"po_number": get_next_po_number(get_db())})


@app.route("/api/purchase-orders", methods=["POST"])
@permission_required("purchase_orders:write")
def create_purchase_order():
    data = request.get_json(force=True) or {}
    db = get_db()
    lines = data.get("lines") or []
    po_value = safe_float(data.get("po_value"))
    # A new PO must belong to a job that has actually been raised - no free-typed
    # or future TM numbers (only imported history is allowed to have no job).
    if not db.execute("SELECT 1 FROM works_orders WHERE works_order_id=? AND job_number NOT LIKE 'HIST-PO-%'",
                      (data.get("works_order_id"),)).fetchone():
        return jsonify({"error": "Choose a works order that has been raised - a purchase order can't be placed against a job number that doesn't exist yet."}), 400
    # The PO number is always server-assigned - never taken from the request.
    for attempt in range(5):
        po_number = get_next_po_number(db)
        try:
            cur = db.execute(
                """INSERT INTO purchase_orders (po_number, works_order_id, supplier_id, description, order_date, po_value, status,
                      sharepoint_url, ship_to_id, date_required, account_number, raised_by, vat_rate)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (po_number, data.get("works_order_id"), data.get("supplier_id"), data.get("description"),
                 data.get("order_date"), po_value, data.get("status", "Draft"),
                 data.get("sharepoint_url"), data.get("ship_to_id") or None, data.get("date_required") or None,
                 data.get("account_number"), data.get("raised_by"), safe_float(data.get("vat_rate"), 20)),
            )
            pid = cur.lastrowid
            if lines:
                subtotal = save_purchase_order_lines(db, pid, lines)
                db.execute("UPDATE purchase_orders SET po_value=? WHERE purchase_order_id=?", (round(subtotal, 2), pid))
            db.commit()
            break
        except sqlite3.IntegrityError as e:
            db.rollback()
            if "po_number" in str(e) and attempt < 4:
                continue
            return jsonify({"error": str(e)}), 400
    return jsonify({"purchase_order_id": pid, "po_number": po_number}), 201


@app.route("/api/purchase-orders/<int:pid>", methods=["PUT"])
@permission_required("purchase_orders:write")
def update_purchase_order(pid):
    data = request.get_json(force=True) or {}
    db = get_db()
    old = db.execute("SELECT * FROM purchase_orders WHERE purchase_order_id=?", (pid,)).fetchone()
    if not old:
        return jsonify({"error": "Not found"}), 404
    if data.get("works_order_id") in (None, ""):
        if not old["is_historic"]:
            return jsonify({"error": "Choose a works order for this purchase order."}), 400
        data["works_order_id"] = None
    elif not db.execute("SELECT 1 FROM works_orders WHERE works_order_id=?", (data.get("works_order_id"),)).fetchone():
        return jsonify({"error": "That works order doesn't exist."}), 400
    lines = data.get("lines")
    po_value = safe_float(data.get("po_value"))
    try:
        # Lines (when sent) are the authority on po_value - compute the
        # final figure up front so what gets audited below is the value
        # that's actually about to be stored, not the pre-recompute one.
        final_po_value = po_value
        if lines:
            final_po_value = round(sum(safe_float(ln.get("qty"), 1) * safe_float(ln.get("unit_price")) for ln in lines), 2)
        log_field_changes(db, "purchase_orders", pid, old,
                           {"status": data.get("status"), "po_value": final_po_value,
                            "vat_rate": safe_float(data.get("vat_rate"), 20)},
                           ["status", "po_value", "vat_rate"], current_user_name())
        db.execute(
            """UPDATE purchase_orders SET po_number=?, works_order_id=?, supplier_id=?, description=?,
               order_date=?, po_value=?, status=?, sharepoint_url=?, ship_to_id=?,
               date_required=?, account_number=?, raised_by=?, vat_rate=? WHERE purchase_order_id=?""",
            (old["po_number"], data.get("works_order_id"), data.get("supplier_id"), data.get("description"),
             data.get("order_date"), po_value, data.get("status"),
             data.get("sharepoint_url"), data.get("ship_to_id") or None, data.get("date_required") or None,
             data.get("account_number"), data.get("raised_by"), safe_float(data.get("vat_rate"), 20), pid),
        )
        if lines is not None:
            subtotal = save_purchase_order_lines(db, pid, lines)
            if lines:
                db.execute("UPDATE purchase_orders SET po_value=? WHERE purchase_order_id=?", (round(subtotal, 2), pid))
        db.commit()
    except sqlite3.IntegrityError as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/purchase-orders/<int:pid>/pdf")
@login_required
def get_purchase_order_pdf(pid):
    db = get_db()
    po = db.execute(
        """SELECT po.*, s.supplier_name, s.address AS supplier_address, wo.job_number,
                  sta.company_name AS ship_to_company_name, sta.address_line1 AS ship_to_address_line1,
                  sta.address_line2 AS ship_to_address_line2, sta.city AS ship_to_city, sta.postcode AS ship_to_postcode
           FROM purchase_orders po
           JOIN suppliers s ON s.supplier_id = po.supplier_id
           LEFT JOIN works_orders wo ON wo.works_order_id = po.works_order_id
           LEFT JOIN ship_to_addresses sta ON sta.ship_to_id = po.ship_to_id
           WHERE po.purchase_order_id=?""",
        (pid,),
    ).fetchone()
    if not po:
        return jsonify({"error": "Purchase Order not found"}), 404
    lines = purchase_order_lines(db, pid)
    pdf_bytes = build_po_pdf(po, lines)
    log_document_access(db, "Purchase Order PDF", po["po_number"])
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={po['po_number']}.pdf"},
    )


@app.route("/api/purchase-orders/<int:pid>/approve", methods=["POST"])
@permission_required("purchase_orders:write")
def approve_purchase_order(pid):
    db = get_db()
    db.execute("UPDATE purchase_orders SET status='Approved' WHERE purchase_order_id=?", (pid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/purchase-orders/<int:pid>", methods=["DELETE"])
@permission_required("all")
def delete_purchase_order(pid):
    db = get_db()
    try:
        db.execute("DELETE FROM purchase_orders WHERE purchase_order_id=?", (pid,))
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "This purchase order has Supplier Invoices linked to it and cannot be deleted."}), 400
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Goods In - acknowledgement that a delivery against a PO has physically
# arrived. Open to every role (see ROLE_PERMISSIONS) - whoever is actually
# there to sign for a delivery needs to log it, not just Procurement.
#
# Every new Goods In record must carry at least one uploaded document (a
# delivery note, packing slip, or photo of the goods/paperwork) as proof of
# what was actually received, for audit purposes - enforced here, not just
# in the UI, via save_goods_in_documents() below. Records created before
# this feature existed won't have one; an Administrator can attach one
# retroactively via POST /api/goods-in/<gid>/documents, but existing
# records are never forced to gain one just to stay editable.
# --------------------------------------------------------------------------
def _goods_in_doc_path(stored_filename):
    return os.path.join(db_location.GOODS_IN_DOCS_DIR, stored_filename)


def save_goods_in_documents(db, goods_in_id, files):
    """Validates and saves one or more uploaded files as documents against
    an existing (already INSERTed, not yet committed) goods_in row. Returns
    None on success or an (error_message, status_code) tuple on the first
    problem found - file-type/count checks run before anything touches disk
    or the database, and if saving itself fails partway through (e.g. a
    full disk), every file this call wrote is removed again and the
    exception re-raised so the caller's transaction can be rolled back
    rather than left half-applied."""
    files = [f for f in (files or []) if f and f.filename]
    if not files:
        return ("A goods received document (e.g. a delivery note, packing slip, or "
                "photo of the goods) must be attached before this can be saved.", 400)
    if len(files) > GOODS_IN_MAX_FILES_PER_UPLOAD:
        return (f"Attach at most {GOODS_IN_MAX_FILES_PER_UPLOAD} files at a time.", 400)
    for f in files:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in GOODS_IN_ALLOWED_EXTENSIONS:
            return (f"'{f.filename}' isn't an accepted file type. Allowed: "
                     f"{', '.join(sorted(GOODS_IN_ALLOWED_EXTENSIONS))}.", 400)

    saved_paths = []
    try:
        for f in files:
            original_name = secure_filename(f.filename) or "document"
            ext = os.path.splitext(original_name)[1].lower()
            stored_name = f"{secrets.token_hex(16)}{ext}"
            dest = _goods_in_doc_path(stored_name)
            f.save(dest)
            saved_paths.append(dest)
            try:
                size = os.path.getsize(dest)
            except OSError:
                size = None
            db.execute(
                """INSERT INTO goods_in_documents
                       (goods_in_id, original_filename, stored_filename, content_type, file_size, uploaded_by)
                   VALUES (?,?,?,?,?,?)""",
                (goods_in_id, original_name, stored_name, f.mimetype, size, current_user_name()),
            )
    except Exception:
        for p in saved_paths:
            try:
                os.remove(p)
            except OSError:
                pass
        raise
    db_location.harden_file_permissions()
    return None


@app.route("/api/goods-in", methods=["GET"])
@login_required
def list_goods_in():
    db = get_db()
    rows = db.execute(
        """SELECT gi.*, po.po_number, po.description AS po_description, s.supplier_name,
                  wo.job_number, sta.label AS ship_to_label,
                  (SELECT COUNT(*) FROM goods_in_documents d WHERE d.goods_in_id = gi.goods_in_id) AS document_count
           FROM goods_in gi
           JOIN purchase_orders po ON po.purchase_order_id = gi.purchase_order_id
           JOIN suppliers s ON s.supplier_id = po.supplier_id
           LEFT JOIN works_orders wo ON wo.works_order_id = po.works_order_id
           LEFT JOIN ship_to_addresses sta ON sta.ship_to_id = gi.ship_to_id
           ORDER BY gi.received_date DESC, gi.goods_in_id DESC"""
    ).fetchall()
    return jsonify(rows_to_list(rows))


@app.route("/api/goods-in/<int:gid>/documents", methods=["GET"])
@login_required
def list_goods_in_documents(gid):
    db = get_db()
    rows = db.execute(
        """SELECT document_id, original_filename, content_type, file_size, uploaded_by, uploaded_date
           FROM goods_in_documents WHERE goods_in_id=? ORDER BY uploaded_date, document_id""",
        (gid,),
    ).fetchall()
    return jsonify(rows_to_list(rows))


@app.route("/api/goods-in/documents/<int:doc_id>/download")
@login_required
def download_goods_in_document(doc_id):
    db = get_db()
    doc = db.execute(
        """SELECT d.*, gi.purchase_order_id, po.po_number
           FROM goods_in_documents d
           JOIN goods_in gi ON gi.goods_in_id = d.goods_in_id
           JOIN purchase_orders po ON po.purchase_order_id = gi.purchase_order_id
           WHERE d.document_id=?""",
        (doc_id,),
    ).fetchone()
    if not doc:
        return jsonify({"error": "Document not found"}), 404
    if not os.path.exists(_goods_in_doc_path(doc["stored_filename"])):
        return jsonify({"error": "That document's file is missing from storage - it may have been removed "
                                  "outside the app. Contact an Administrator."}), 404
    log_document_access(db, "Goods In document", f"{doc['po_number']} - {doc['original_filename']}")
    return send_from_directory(
        db_location.GOODS_IN_DOCS_DIR, doc["stored_filename"],
        mimetype=doc["content_type"] or None, as_attachment=True, download_name=doc["original_filename"],
    )


@app.route("/api/goods-in/<int:gid>/documents", methods=["POST"])
@permission_required("goods_in:write")
def add_goods_in_documents(gid):
    db = get_db()
    if not db.execute("SELECT 1 FROM goods_in WHERE goods_in_id=?", (gid,)).fetchone():
        return jsonify({"error": "Goods In record not found"}), 404
    err = save_goods_in_documents(db, gid, request.files.getlist("documents"))
    if err:
        db.rollback()
        return jsonify({"error": err[0]}), err[1]
    db.commit()
    return jsonify({"ok": True}), 201


@app.route("/api/goods-in/documents/<int:doc_id>", methods=["DELETE"])
@permission_required("all")
def delete_goods_in_document(doc_id):
    """Administrator-only: lets a wrongly-attached document be removed
    (e.g. the wrong file was picked), but never the last one on a record -
    every Goods In record must keep at least one, so correcting a mistake
    always means uploading the right file before removing the wrong one."""
    db = get_db()
    doc = db.execute("SELECT * FROM goods_in_documents WHERE document_id=?", (doc_id,)).fetchone()
    if not doc:
        return jsonify({"error": "Document not found"}), 404
    remaining = db.execute(
        "SELECT COUNT(*) AS n FROM goods_in_documents WHERE goods_in_id=?", (doc["goods_in_id"],)
    ).fetchone()["n"]
    if remaining <= 1:
        return jsonify({"error": "This is the only document on this Goods In record - upload a replacement "
                                  "before removing it."}), 400
    db.execute("DELETE FROM goods_in_documents WHERE document_id=?", (doc_id,))
    db.commit()
    try:
        os.remove(_goods_in_doc_path(doc["stored_filename"]))
    except OSError:
        pass
    return jsonify({"ok": True})


@app.route("/api/goods-in", methods=["POST"])
@permission_required("goods_in:write")
def create_goods_in():
    data = request.form if request.content_type and "multipart/form-data" in request.content_type else (request.get_json(silent=True) or {})
    if not data.get("purchase_order_id"):
        return jsonify({"error": "Purchase Order is required"}), 400
    if data.get("destination_type") not in ("Works Order", "Stock"):
        return jsonify({"error": "Destination must be 'Works Order' or 'Stock'"}), 400
    if data.get("destination_type") == "Stock" and not data.get("ship_to_id"):
        return jsonify({"error": "Choose a site for stock deliveries"}), 400
    if not data.get("received_by"):
        return jsonify({"error": "Name of person accepting goods is required"}), 400
    db = get_db()
    cur = db.execute(
        """INSERT INTO goods_in (purchase_order_id, destination_type, ship_to_id, received_date,
              quantity_status, quantity_received, received_by, comment)
           VALUES (?,?,?,?,?,?,?,?)""",
        (safe_int(data.get("purchase_order_id")), data.get("destination_type"),
         safe_int(data.get("ship_to_id")) if data.get("destination_type") == "Stock" else None,
         data.get("received_date"), data.get("quantity_status"),
         safe_float(data.get("quantity_received"), None),
         data.get("received_by"), data.get("comment")),
    )
    gid = cur.lastrowid
    err = save_goods_in_documents(db, gid, request.files.getlist("documents"))
    if err:
        db.rollback()
        return jsonify({"error": err[0]}), err[1]
    db.commit()
    return jsonify({"goods_in_id": gid}), 201


@app.route("/api/goods-in/<int:gid>", methods=["PUT"])
@permission_required("goods_in:write")
def update_goods_in(gid):
    data = request.get_json(force=True) or {}
    if data.get("destination_type") not in ("Works Order", "Stock"):
        return jsonify({"error": "Destination must be 'Works Order' or 'Stock'"}), 400
    if data.get("destination_type") == "Stock" and not data.get("ship_to_id"):
        return jsonify({"error": "Choose a site for stock deliveries"}), 400
    db = get_db()
    db.execute(
        """UPDATE goods_in SET purchase_order_id=?, destination_type=?, ship_to_id=?, received_date=?,
              quantity_status=?, quantity_received=?, received_by=?, comment=? WHERE goods_in_id=?""",
        (data.get("purchase_order_id"), data.get("destination_type"),
         data.get("ship_to_id") if data.get("destination_type") == "Stock" else None,
         data.get("received_date"), data.get("quantity_status"),
         safe_float(data.get("quantity_received"), None),
         data.get("received_by"), data.get("comment"), gid),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/goods-in/<int:gid>", methods=["DELETE"])
@permission_required("all")
def delete_goods_in(gid):
    db = get_db()
    docs = db.execute("SELECT stored_filename FROM goods_in_documents WHERE goods_in_id=?", (gid,)).fetchall()
    db.execute("DELETE FROM goods_in WHERE goods_in_id=?", (gid,))
    db.commit()
    for d in docs:
        try:
            os.remove(_goods_in_doc_path(d["stored_filename"]))
        except OSError:
            pass
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Despatch Notes - the outbound counterpart to Goods In: goods physically
# shipped to the customer against a Works Order, with itemised lines and a
# printable PDF built on the same branding as Quotes and Purchase Orders.
# --------------------------------------------------------------------------
def despatch_note_lines(db, did):
    rows = db.execute(
        "SELECT * FROM despatch_note_lines WHERE despatch_note_id=? ORDER BY line_id", (did,)
    ).fetchall()
    return rows_to_list(rows)


def save_despatch_note_lines(db, did, lines):
    """Replace-all, mirroring save_purchase_order_lines - deletes any
    existing lines for this despatch note and inserts the new set."""
    db.execute("DELETE FROM despatch_note_lines WHERE despatch_note_id=?", (did,))
    for ln in lines or []:
        qty = safe_float(ln.get("qty"), 1) or 1
        db.execute(
            "INSERT INTO despatch_note_lines (despatch_note_id, qty, part_number, description) VALUES (?,?,?,?)",
            (did, qty, ln.get("part_number"), ln.get("description")),
        )


def _despatch_signature_path(stored_filename):
    return os.path.join(db_location.SIGNATURES_DIR, stored_filename)


# A signature pad PNG at a sensible on-screen size comes to a few tens of KB;
# this generously caps the decoded file at 2MB so a malformed or hostile
# data: URL can't be used to fill the disk, without being anywhere close to
# a real signature's size.
MAX_SIGNATURE_BYTES = 2 * 1024 * 1024


@app.route("/api/despatch-notes", methods=["GET"])
@login_required
def list_despatch_notes():
    db = get_db()
    rows = db.execute(
        """SELECT dn.*, wo.job_number, wo.job_name, c.customer_name
           FROM despatch_notes dn
           JOIN works_orders wo ON wo.works_order_id = dn.works_order_id
           JOIN customers c ON c.customer_id = wo.customer_id
           ORDER BY dn.despatch_date DESC, dn.despatch_note_id DESC"""
    ).fetchall()
    result = rows_to_list(rows)
    for r in result:
        r["lines"] = despatch_note_lines(db, r["despatch_note_id"])
    return jsonify(result)


@app.route("/api/despatch-notes/next-number")
@login_required
def next_despatch_number():
    db = get_db()
    return jsonify({"despatch_number": get_next_despatch_number(db)})


@app.route("/api/despatch-notes", methods=["POST"])
@permission_required("despatch_notes:write")
def create_despatch_note():
    data = request.get_json(force=True) or {}
    db = get_db()
    # Server-assigned, same reasoning as quote numbers: always generated
    # here, never taken from the request, so it stays a reliable, gapless
    # sequence regardless of what a client sends.
    despatch_number = get_next_despatch_number(db)
    try:
        cur = db.execute(
            """INSERT INTO despatch_notes (despatch_number, works_order_id, despatch_date, delivery_address,
                  carrier, despatched_by, notes, status, created_by, created_date)
               VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))""",
            (despatch_number, data.get("works_order_id"), data.get("despatch_date"), data.get("delivery_address"),
             data.get("carrier"), data.get("despatched_by"), data.get("notes"), data.get("status", "Draft"),
             current_user_name()),
        )
        did = cur.lastrowid
        save_despatch_note_lines(db, did, data.get("lines") or [])
        db.commit()
    except sqlite3.IntegrityError as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400
    return jsonify({"despatch_note_id": did}), 201


@app.route("/api/despatch-notes/<int:did>", methods=["PUT"])
@permission_required("despatch_notes:write")
def update_despatch_note(did):
    data = request.get_json(force=True) or {}
    db = get_db()
    old = db.execute("SELECT * FROM despatch_notes WHERE despatch_note_id=?", (did,)).fetchone()
    log_field_changes(db, "despatch_notes", did, old, {"status": data.get("status")}, ["status"], current_user_name())
    try:
        db.execute(
            """UPDATE despatch_notes SET works_order_id=?, despatch_date=?, delivery_address=?, carrier=?,
               despatched_by=?, notes=?, status=?, updated_by=?, updated_date=datetime('now')
               WHERE despatch_note_id=?""",
            (data.get("works_order_id"), data.get("despatch_date"), data.get("delivery_address"), data.get("carrier"),
             data.get("despatched_by"), data.get("notes"), data.get("status"), current_user_name(), did),
        )
        if data.get("lines") is not None:
            save_despatch_note_lines(db, did, data.get("lines"))
        db.commit()
    except sqlite3.IntegrityError as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/despatch-notes/<int:did>/pdf")
@login_required
def get_despatch_note_pdf(did):
    db = get_db()
    dn = db.execute(
        """SELECT dn.*, wo.job_number, c.customer_name
           FROM despatch_notes dn
           JOIN works_orders wo ON wo.works_order_id = dn.works_order_id
           JOIN customers c ON c.customer_id = wo.customer_id
           WHERE dn.despatch_note_id=?""",
        (did,),
    ).fetchone()
    if not dn:
        return jsonify({"error": "Despatch Note not found"}), 404
    lines = despatch_note_lines(db, did)
    signature_path = None
    if dn["signature_filename"]:
        candidate = _despatch_signature_path(dn["signature_filename"])
        if os.path.exists(candidate):
            signature_path = candidate
    pdf_bytes = build_despatch_pdf(dn, lines, signature_path=signature_path)
    log_document_access(db, "Despatch Note PDF", dn["despatch_number"])
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={dn['despatch_number']}.pdf"},
    )


@app.route("/api/despatch-notes/<int:did>/signature", methods=["POST"])
@permission_required("despatch_notes:write")
def sign_despatch_note(did):
    """Captures an on-screen signature taken on the despatching staff
    member's own device at the point of delivery - see README.md,
    "Despatch Note signatures". Only a Despatched note can be signed (a
    Draft hasn't gone out yet, and a Cancelled one never will), and
    signing again (e.g. a mis-drawn signature) simply replaces whatever
    was captured before rather than keeping both."""
    db = get_db()
    dn = db.execute("SELECT * FROM despatch_notes WHERE despatch_note_id=?", (did,)).fetchone()
    if not dn:
        return jsonify({"error": "Despatch Note not found"}), 404
    if dn["status"] != "Despatched":
        return jsonify({"error": "Only a Despatched note can be signed for - set its status to "
                                   "Despatched first."}), 400

    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    signature = data.get("signature") or ""
    if not name:
        return jsonify({"error": "The name of whoever signed is required."}), 400
    if not signature:
        return jsonify({"error": "A signature must be drawn before this can be saved."}), 400

    match = re.match(r"^data:image/png;base64,(.+)$", signature, re.DOTALL)
    if not match:
        return jsonify({"error": "The signature wasn't in the expected image format."}), 400
    try:
        png_bytes = base64.b64decode(match.group(1), validate=True)
    except ValueError:
        return jsonify({"error": "The signature data was corrupted and couldn't be read - please try signing "
                                   "again."}), 400
    if not png_bytes or len(png_bytes) > MAX_SIGNATURE_BYTES:
        return jsonify({"error": "The signature image was empty or too large."}), 400

    old_filename = dn["signature_filename"]
    stored_name = f"{secrets.token_hex(16)}.png"
    dest = _despatch_signature_path(stored_name)
    with open(dest, "wb") as f:
        f.write(png_bytes)

    db.execute(
        """UPDATE despatch_notes SET signature_filename=?, signature_name=?, signature_date=?,
           signature_captured_by=? WHERE despatch_note_id=?""",
        (stored_name, name, data.get("date") or datetime.now().strftime("%Y-%m-%d"), current_user_name(), did),
    )
    db.commit()
    db_location.harden_file_permissions()

    if old_filename:
        try:
            os.remove(_despatch_signature_path(old_filename))
        except OSError:
            pass

    return jsonify({"ok": True})


@app.route("/api/despatch-notes/<int:did>/signature")
@login_required
def get_despatch_note_signature(did):
    db = get_db()
    dn = db.execute("SELECT * FROM despatch_notes WHERE despatch_note_id=?", (did,)).fetchone()
    if not dn or not dn["signature_filename"]:
        return jsonify({"error": "No signature has been captured for this Despatch Note"}), 404
    if not os.path.exists(_despatch_signature_path(dn["signature_filename"])):
        return jsonify({"error": "That signature's file is missing from storage - it may have been removed "
                                   "outside the app. Contact an Administrator."}), 404
    log_document_access(db, "Despatch Note Signature", dn["despatch_number"])
    return send_from_directory(db_location.SIGNATURES_DIR, dn["signature_filename"], mimetype="image/png")


@app.route("/api/despatch-notes/<int:did>", methods=["DELETE"])
@permission_required("all")
def delete_despatch_note(did):
    db = get_db()
    dn = db.execute("SELECT signature_filename FROM despatch_notes WHERE despatch_note_id=?", (did,)).fetchone()
    db.execute("DELETE FROM despatch_notes WHERE despatch_note_id=?", (did,))
    db.commit()
    if dn and dn["signature_filename"]:
        try:
            os.remove(_despatch_signature_path(dn["signature_filename"]))
        except OSError:
            pass
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Inventory - stock items and their allocations to a Ship To site or a
# Works Order
# --------------------------------------------------------------------------
def move_stock(db, stock_item_id, quantity, from_ship_to_id, from_works_order_id,
               to_ship_to_id, to_works_order_id, notes, moved_by):
    """The one place stock_allocations (current state) and stock_movements
    (audit log) are ever both written, so they can't drift apart. Handles
    a receipt (no source - brand new stock arriving), an issue (Site to a
    Works Order), or a transfer (anything to anything else). Raises
    ValueError with a message safe to show the user on any invalid input -
    callers are expected to catch it and roll back."""
    if quantity is None or quantity <= 0:
        raise ValueError("Quantity must be a positive number")
    if from_ship_to_id and from_works_order_id:
        raise ValueError("Source can't be both a Site and a Works Order")
    if bool(to_ship_to_id) == bool(to_works_order_id):  # both set, or neither
        raise ValueError("Choose exactly one destination - a Site or a Works Order")
    if (from_ship_to_id and from_ship_to_id == to_ship_to_id) or \
       (from_works_order_id and from_works_order_id == to_works_order_id):
        raise ValueError("Source and destination can't be the same")

    has_source = bool(from_ship_to_id) or bool(from_works_order_id)
    if has_source:
        if from_ship_to_id:
            src = db.execute(
                "SELECT * FROM stock_allocations WHERE stock_item_id=? AND ship_to_id=?",
                (stock_item_id, from_ship_to_id),
            ).fetchone()
        else:
            src = db.execute(
                "SELECT * FROM stock_allocations WHERE stock_item_id=? AND works_order_id=?",
                (stock_item_id, from_works_order_id),
            ).fetchone()
        if not src:
            raise ValueError("No stock currently allocated at the selected source")
        if src["quantity"] < quantity:
            raise ValueError(f"Only {src['quantity']:g} available at the selected source")
        remaining = src["quantity"] - quantity
        if remaining <= 0:
            db.execute("DELETE FROM stock_allocations WHERE allocation_id=?", (src["allocation_id"],))
        else:
            db.execute("UPDATE stock_allocations SET quantity=? WHERE allocation_id=?", (remaining, src["allocation_id"]))

    if to_ship_to_id:
        dest = db.execute(
            "SELECT * FROM stock_allocations WHERE stock_item_id=? AND ship_to_id=?",
            (stock_item_id, to_ship_to_id),
        ).fetchone()
    else:
        dest = db.execute(
            "SELECT * FROM stock_allocations WHERE stock_item_id=? AND works_order_id=?",
            (stock_item_id, to_works_order_id),
        ).fetchone()
    if dest:
        db.execute("UPDATE stock_allocations SET quantity=quantity+? WHERE allocation_id=?", (quantity, dest["allocation_id"]))
    else:
        db.execute(
            "INSERT INTO stock_allocations (stock_item_id, quantity, ship_to_id, works_order_id, notes) VALUES (?,?,?,?,?)",
            (stock_item_id, quantity, to_ship_to_id, to_works_order_id, notes),
        )

    db.execute(
        """INSERT INTO stock_movements
           (stock_item_id, quantity, from_ship_to_id, from_works_order_id, to_ship_to_id, to_works_order_id, moved_by, notes)
           VALUES (?,?,?,?,?,?,?,?)""",
        (stock_item_id, quantity, from_ship_to_id, from_works_order_id, to_ship_to_id, to_works_order_id, moved_by, notes),
    )


def stock_item_with_allocations(db, item_row):
    item = dict(item_row)
    allocations = db.execute(
        """SELECT sa.*, sta.label AS ship_to_label, wo.job_number, wo.job_name
           FROM stock_allocations sa
           LEFT JOIN ship_to_addresses sta ON sta.ship_to_id = sa.ship_to_id
           LEFT JOIN works_orders wo ON wo.works_order_id = sa.works_order_id
           WHERE sa.stock_item_id=? ORDER BY sa.allocated_date DESC""",
        (item["stock_item_id"],),
    ).fetchall()
    item["allocations"] = rows_to_list(allocations)
    item["total_quantity"] = sum(a["quantity"] for a in item["allocations"])
    item["total_value"] = round(item["total_quantity"] * item["purchase_cost"], 2)
    item["total_sale_value"] = round(item["total_quantity"] * item["sale_price"], 2)
    return item


@app.route("/api/stock-items", methods=["GET"])
@login_required
def list_stock_items():
    db = get_db()
    rows = db.execute(
        """SELECT si.*, s.supplier_name FROM stock_items si
           LEFT JOIN suppliers s ON s.supplier_id = si.supplier_id
           ORDER BY si.part_number"""
    ).fetchall()
    return jsonify([stock_item_with_allocations(db, r) for r in rows])


@app.route("/api/stock-items/<int:item_id>", methods=["GET"])
@login_required
def get_stock_item(item_id):
    db = get_db()
    row = db.execute(
        """SELECT si.*, s.supplier_name FROM stock_items si
           LEFT JOIN suppliers s ON s.supplier_id = si.supplier_id
           WHERE si.stock_item_id=?""",
        (item_id,),
    ).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(stock_item_with_allocations(db, row))


@app.route("/api/stock-items", methods=["POST"])
@permission_required("inventory:write")
def create_stock_item():
    data = request.get_json(force=True) or {}
    part_number = (data.get("part_number") or "").strip()
    if not part_number:
        return jsonify({"error": "Part number is required"}), 400
    initial_quantity = safe_float(data.get("initial_quantity"))
    initial_ship_to_id = data.get("initial_ship_to_id") or None
    if initial_quantity > 0 and not initial_ship_to_id:
        return jsonify({"error": "Choose a site to hold the initial quantity"}), 400
    db = get_db()
    try:
        cur = db.execute(
            """INSERT INTO stock_items (part_number, description, unit, purchase_cost, sale_price, reorder_level, supplier_id, notes)
               VALUES (?,?,?,?,?,?,?,?)""",
            (part_number, data.get("description"), data.get("unit") or "each",
             safe_float(data.get("purchase_cost")), safe_float(data.get("sale_price")),
             safe_float(data.get("reorder_level")), data.get("supplier_id") or None, data.get("notes")),
        )
        item_id = cur.lastrowid
        if initial_quantity > 0:
            move_stock(db, item_id, initial_quantity, None, None, initial_ship_to_id, None,
                       "Initial stock on creation", session.get("full_name"))
        db.commit()
    except (sqlite3.IntegrityError, ValueError) as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400
    return jsonify({"stock_item_id": cur.lastrowid}), 201


@app.route("/api/stock-items/<int:item_id>", methods=["PUT"])
@permission_required("inventory:write")
def update_stock_item(item_id):
    data = request.get_json(force=True) or {}
    part_number = (data.get("part_number") or "").strip()
    if not part_number:
        return jsonify({"error": "Part number is required"}), 400
    db = get_db()
    try:
        db.execute(
            """UPDATE stock_items SET part_number=?, description=?, unit=?, purchase_cost=?, sale_price=?,
               reorder_level=?, supplier_id=?, notes=? WHERE stock_item_id=?""",
            (part_number, data.get("description"), data.get("unit") or "each",
             safe_float(data.get("purchase_cost")), safe_float(data.get("sale_price")),
             safe_float(data.get("reorder_level")), data.get("supplier_id") or None, data.get("notes"), item_id),
        )
        db.commit()
    except sqlite3.IntegrityError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/stock-items/<int:item_id>", methods=["DELETE"])
@permission_required("all")
def delete_stock_item(item_id):
    db = get_db()
    db.execute("DELETE FROM stock_items WHERE stock_item_id=?", (item_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/stock-items/<int:item_id>/allocations", methods=["POST"])
@permission_required("inventory:write")
def create_stock_allocation(item_id):
    data = request.get_json(force=True) or {}
    ship_to_id = data.get("ship_to_id") or None
    works_order_id = data.get("works_order_id") or None
    if bool(ship_to_id) == bool(works_order_id):  # both set, or neither
        return jsonify({"error": "Choose either a Site or a Works Order for this allocation, not both or neither"}), 400
    quantity = safe_float(data.get("quantity"), None)
    if quantity is None or quantity <= 0:
        return jsonify({"error": "Quantity must be a positive number"}), 400
    db = get_db()
    try:
        cur = db.execute(
            """INSERT INTO stock_allocations (stock_item_id, quantity, ship_to_id, works_order_id, notes)
               VALUES (?,?,?,?,?)""",
            (item_id, quantity, ship_to_id, works_order_id, data.get("notes")),
        )
        db.commit()
    except sqlite3.IntegrityError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"allocation_id": cur.lastrowid}), 201


@app.route("/api/stock-allocations/<int:allocation_id>", methods=["PUT"])
@permission_required("inventory:write")
def update_stock_allocation(allocation_id):
    data = request.get_json(force=True) or {}
    ship_to_id = data.get("ship_to_id") or None
    works_order_id = data.get("works_order_id") or None
    if bool(ship_to_id) == bool(works_order_id):
        return jsonify({"error": "Choose either a Site or a Works Order for this allocation, not both or neither"}), 400
    quantity = safe_float(data.get("quantity"), None)
    if quantity is None or quantity <= 0:
        return jsonify({"error": "Quantity must be a positive number"}), 400
    db = get_db()
    try:
        db.execute(
            """UPDATE stock_allocations SET quantity=?, ship_to_id=?, works_order_id=?, notes=?
               WHERE allocation_id=?""",
            (quantity, ship_to_id, works_order_id, data.get("notes"), allocation_id),
        )
        db.commit()
    except sqlite3.IntegrityError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/stock-allocations/<int:allocation_id>", methods=["DELETE"])
@permission_required("inventory:write")
def delete_stock_allocation(allocation_id):
    db = get_db()
    db.execute("DELETE FROM stock_allocations WHERE allocation_id=?", (allocation_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/stock-items/<int:item_id>/transfer", methods=["POST"])
@permission_required("inventory:write")
def transfer_stock(item_id):
    data = request.get_json(force=True) or {}
    db = get_db()
    try:
        move_stock(
            db, item_id,
            safe_float(data.get("quantity"), None),
            data.get("from_ship_to_id") or None,
            data.get("from_works_order_id") or None,
            data.get("to_ship_to_id") or None,
            data.get("to_works_order_id") or None,
            data.get("notes"),
            session.get("full_name"),
        )
        db.commit()
    except (ValueError, sqlite3.IntegrityError) as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/stock-movements")
@login_required
def list_stock_movements():
    db = get_db()
    item_id = request.args.get("stock_item_id")
    sql = """SELECT sm.*, si.part_number, si.description,
                    f_site.label AS from_ship_to_label, f_wo.job_number AS from_job_number,
                    t_site.label AS to_ship_to_label, t_wo.job_number AS to_job_number
             FROM stock_movements sm
             JOIN stock_items si ON si.stock_item_id = sm.stock_item_id
             LEFT JOIN ship_to_addresses f_site ON f_site.ship_to_id = sm.from_ship_to_id
             LEFT JOIN works_orders f_wo ON f_wo.works_order_id = sm.from_works_order_id
             LEFT JOIN ship_to_addresses t_site ON t_site.ship_to_id = sm.to_ship_to_id
             LEFT JOIN works_orders t_wo ON t_wo.works_order_id = sm.to_works_order_id"""
    params = []
    if item_id:
        sql += " WHERE sm.stock_item_id=?"
        params.append(item_id)
    sql += " ORDER BY sm.movement_date DESC, sm.movement_id DESC"
    rows = db.execute(sql, params).fetchall()
    return jsonify(rows_to_list(rows))


# --------------------------------------------------------------------------
# Supplier Invoices
# --------------------------------------------------------------------------
@app.route("/api/supplier-invoices", methods=["GET"])
@login_required
def list_supplier_invoices():
    db = get_db()
    rows = db.execute(
        """SELECT si.*, s.supplier_name, wo.job_number, po.po_number, po.po_value
           FROM supplier_invoices si
           JOIN suppliers s ON s.supplier_id = si.supplier_id
           JOIN works_orders wo ON wo.works_order_id = si.works_order_id
           JOIN purchase_orders po ON po.purchase_order_id = si.purchase_order_id
           ORDER BY si.invoice_date DESC"""
    ).fetchall()
    return jsonify(rows_to_list(rows))


@app.route("/api/supplier-invoices", methods=["POST"])
@permission_required("supplier_invoices:write")
def create_supplier_invoice():
    data = request.get_json(force=True) or {}
    db = get_db()
    po = db.execute("SELECT * FROM purchase_orders WHERE purchase_order_id=?", (data.get("purchase_order_id"),)).fetchone()
    if not po:
        return jsonify({"error": "Purchase order not found"}), 400
    cur = db.execute(
        """INSERT INTO supplier_invoices (invoice_number, purchase_order_id, works_order_id, supplier_id, invoice_date, invoice_amount, status, created_by, created_date)
           VALUES (?,?,?,?,?,?,?,?,datetime('now'))""",
        (data.get("invoice_number"), po["purchase_order_id"], po["works_order_id"], po["supplier_id"],
         data.get("invoice_date"), safe_float(data.get("invoice_amount")), data.get("status", "Pending"), current_user_name()),
    )
    db.commit()
    # Validate against PO value and surface a warning (record is still saved)
    existing_total = db.execute(
        "SELECT COALESCE(SUM(invoice_amount),0) FROM supplier_invoices WHERE purchase_order_id=? AND status != 'Cancelled'",
        (po["purchase_order_id"],),
    ).fetchone()[0]
    warning = None
    if existing_total > po["po_value"]:
        warning = f"Total invoiced (£{existing_total:,.2f}) now exceeds PO value (£{po['po_value']:,.2f}) - approval required"
    return jsonify({"supplier_invoice_id": cur.lastrowid, "warning": warning}), 201


@app.route("/api/supplier-invoices/<int:iid>", methods=["PUT"])
@permission_required("supplier_invoices:write")
def update_supplier_invoice(iid):
    data = request.get_json(force=True) or {}
    db = get_db()
    old = db.execute("SELECT * FROM supplier_invoices WHERE supplier_invoice_id=?", (iid,)).fetchone()
    log_field_changes(db, "supplier_invoices", iid, old,
                       {"status": data.get("status"), "invoice_amount": safe_float(data.get("invoice_amount"))},
                       ["status", "invoice_amount"], current_user_name())
    db.execute(
        """UPDATE supplier_invoices SET invoice_number=?, invoice_date=?, invoice_amount=?, status=?,
           updated_by=?, updated_date=datetime('now') WHERE supplier_invoice_id=?""",
        (data.get("invoice_number"), data.get("invoice_date"), safe_float(data.get("invoice_amount")), data.get("status"), current_user_name(), iid),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/supplier-invoices/<int:iid>", methods=["DELETE"])
@permission_required("all")
def delete_supplier_invoice(iid):
    db = get_db()
    db.execute("DELETE FROM supplier_invoices WHERE supplier_invoice_id=?", (iid,))
    db.commit()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Labour Entries
# --------------------------------------------------------------------------
@app.route("/api/labour-entries", methods=["GET"])
@login_required
def list_labour_entries():
    db = get_db()
    rows = db.execute(
        """SELECT le.*, wo.job_number FROM labour_entries le
           JOIN works_orders wo ON wo.works_order_id = le.works_order_id
           ORDER BY le.work_date DESC"""
    ).fetchall()
    return jsonify(rows_to_list(rows))


@app.route("/api/labour-comparison")
@login_required
def labour_comparison():
    """Quoted vs Booked hours and cost, grouped by Works Order. Quoted
    comes from quote_labour_lines for every Quote linked to that Works
    Order via quotes.works_order_id (summed across all of them, e.g. an
    original quote plus a later variation) - a quote with no Works Order
    linked contributes nothing here, which is the normal case for a quote
    that hasn't (yet, or ever) become an actual job. Booked comes from
    labour_entries, same as the Entries view. A Works Order appears here
    if it has either side (quoted or booked), even if the other is zero,
    so a job that's been quoted but not yet started still shows up."""
    db = get_db()
    quoted = db.execute(
        """SELECT wo.works_order_id, wo.job_number, wo.job_name,
                  COALESCE(SUM(qll.standard_hours + qll.ot_hours), 0) AS quoted_hours,
                  COALESCE(SUM(qll.standard_hours * qll.standard_rate + qll.ot_hours * qll.ot_rate), 0) AS quoted_cost
           FROM works_orders wo
           JOIN quotes q ON q.works_order_id = wo.works_order_id
           JOIN quote_labour_lines qll ON qll.quote_id = q.quote_id
           GROUP BY wo.works_order_id"""
    ).fetchall()
    booked = db.execute(
        """SELECT wo.works_order_id, wo.job_number, wo.job_name,
                  COALESCE(SUM(le.hours_worked), 0) AS booked_hours,
                  COALESCE(SUM(le.labour_cost), 0) AS booked_cost
           FROM works_orders wo
           JOIN labour_entries le ON le.works_order_id = wo.works_order_id
           GROUP BY wo.works_order_id"""
    ).fetchall()

    combined = {}
    for r in quoted:
        combined[r["works_order_id"]] = {
            "job_number": r["job_number"], "job_name": r["job_name"],
            "quoted_hours": r["quoted_hours"], "quoted_cost": round(r["quoted_cost"], 2),
            "booked_hours": 0, "booked_cost": 0,
        }
    for r in booked:
        entry = combined.setdefault(r["works_order_id"], {
            "job_number": r["job_number"], "job_name": r["job_name"],
            "quoted_hours": 0, "quoted_cost": 0, "booked_hours": 0, "booked_cost": 0,
        })
        entry["booked_hours"] = r["booked_hours"]
        entry["booked_cost"] = round(r["booked_cost"], 2)

    result = list(combined.values())
    for r in result:
        r["variance_hours"] = round(r["quoted_hours"] - r["booked_hours"], 2)
    result.sort(key=lambda r: r["job_number"])
    return jsonify(result)


@app.route("/api/labour-entries", methods=["POST"])
@permission_required("labour_entries:write")
def create_labour_entry():
    data = request.get_json(force=True) or {}
    hours = safe_float(data.get("hours_worked"))
    rate = safe_float(data.get("hourly_rate"))
    cost = round(hours * rate, 2)
    db = get_db()
    cur = db.execute(
        """INSERT INTO labour_entries (works_order_id, employee_name, work_date, hours_worked, hourly_rate, labour_cost, created_by, created_date)
           VALUES (?,?,?,?,?,?,?,datetime('now'))""",
        (data.get("works_order_id"), data.get("employee_name"), data.get("work_date"), hours, rate, cost, current_user_name()),
    )
    db.commit()
    return jsonify({"labour_entry_id": cur.lastrowid, "labour_cost": cost}), 201


@app.route("/api/labour-entries/<int:lid>", methods=["PUT"])
@permission_required("labour_entries:write")
def update_labour_entry(lid):
    data = request.get_json(force=True) or {}
    hours = safe_float(data.get("hours_worked"))
    rate = safe_float(data.get("hourly_rate"))
    cost = round(hours * rate, 2)
    db = get_db()
    old = db.execute("SELECT * FROM labour_entries WHERE labour_entry_id=?", (lid,)).fetchone()
    log_field_changes(db, "labour_entries", lid, old,
                       {"hours_worked": hours, "hourly_rate": rate, "labour_cost": cost},
                       ["hours_worked", "hourly_rate", "labour_cost"], current_user_name())
    db.execute(
        """UPDATE labour_entries SET employee_name=?, work_date=?, hours_worked=?, hourly_rate=?, labour_cost=?,
           updated_by=?, updated_date=datetime('now') WHERE labour_entry_id=?""",
        (data.get("employee_name"), data.get("work_date"), hours, rate, cost, current_user_name(), lid),
    )
    db.commit()
    return jsonify({"ok": True, "labour_cost": cost})


@app.route("/api/labour-entries/<int:lid>", methods=["DELETE"])
@permission_required("all")
def delete_labour_entry(lid):
    db = get_db()
    db.execute("DELETE FROM labour_entries WHERE labour_entry_id=?", (lid,))
    db.commit()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Customer Invoices
# --------------------------------------------------------------------------
@app.route("/api/customer-invoices", methods=["GET"])
@login_required
def list_customer_invoices():
    db = get_db()
    rows = db.execute(
        """SELECT ci.*, wo.job_number FROM customer_invoices ci
           JOIN works_orders wo ON wo.works_order_id = ci.works_order_id
           ORDER BY ci.invoice_date DESC"""
    ).fetchall()
    return jsonify(rows_to_list(rows))


@app.route("/api/customer-invoices", methods=["POST"])
@permission_required("customer_invoices:write")
def create_customer_invoice():
    data = request.get_json(force=True) or {}
    db = get_db()
    cur = db.execute(
        """INSERT INTO customer_invoices (works_order_id, invoice_number, invoice_date, invoice_amount, status, created_by, created_date)
           VALUES (?,?,?,?,?,?,datetime('now'))""",
        (data.get("works_order_id"), data.get("invoice_number"), data.get("invoice_date"),
         safe_float(data.get("invoice_amount")), data.get("status", "Draft"), current_user_name()),
    )
    db.commit()
    return jsonify({"customer_invoice_id": cur.lastrowid}), 201


@app.route("/api/customer-invoices/<int:iid>", methods=["PUT"])
@permission_required("customer_invoices:write")
def update_customer_invoice(iid):
    data = request.get_json(force=True) or {}
    db = get_db()
    old = db.execute("SELECT * FROM customer_invoices WHERE customer_invoice_id=?", (iid,)).fetchone()
    log_field_changes(db, "customer_invoices", iid, old,
                       {"status": data.get("status"), "invoice_amount": safe_float(data.get("invoice_amount"))},
                       ["status", "invoice_amount"], current_user_name())
    db.execute(
        """UPDATE customer_invoices SET invoice_number=?, invoice_date=?, invoice_amount=?, status=?,
           updated_by=?, updated_date=datetime('now') WHERE customer_invoice_id=?""",
        (data.get("invoice_number"), data.get("invoice_date"), safe_float(data.get("invoice_amount")), data.get("status"), current_user_name(), iid),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/customer-invoices/<int:iid>", methods=["DELETE"])
@permission_required("all")
def delete_customer_invoice(iid):
    db = get_db()
    db.execute("DELETE FROM customer_invoices WHERE customer_invoice_id=?", (iid,))
    db.commit()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
FINISHED_JOB_STATUSES = {"Completed", "Closed", "Cancelled"}


def compute_all_alerts(db, jobs=None, include_accepted=False):
    """Every job-level alert (budget/margin/over-invoiced, from
    works_order_with_metrics) plus every open quote past its Valid Until
    date, as one flat list. Shared by the full /api/dashboard payload and
    the lightweight /api/alerts endpoint, which exists so a stale quote or
    a budget overrun shows up as soon as anyone signs in - not only for
    whoever happens to open the Dashboard page that day. Pass `jobs` when
    the caller (dashboard()) has already computed it, so this doesn't
    redo that work. Jobs that are already Completed, Closed or Cancelled are
    left out - there is nothing left to do about them."""
    if jobs is None:
        rows = db.execute(
            f"""SELECT wo.*, c.customer_name FROM works_orders wo
               JOIN customers c ON c.customer_id = wo.customer_id WHERE {PLACEHOLDER_JOB_SQL}"""
        ).fetchall()
        jobs = [works_order_with_metrics(db, r) for r in rows]

    accepted = {r[0] for r in db.execute("SELECT alert_key FROM alert_acceptances").fetchall()}
    all_alerts = []
    for j in jobs:
        # A finished job (Completed / Closed / Cancelled) can't be acted on any
        # more, so it doesn't raise a dashboard / sidebar alert. Its alerts are
        # still shown on the job's own detail view and in its traffic light.
        if j["status"] in FINISHED_JOB_STATUSES:
            continue
        for a in j["alerts"]:
            all_alerts.append({**a, "job_number": j["job_number"], "job_name": j["job_name"],
                               "works_order_id": j["works_order_id"],
                               "alert_key": alert_key(j["job_number"], a["type"], a["message"])})

    # Quotes still open (Draft/Sent) whose Valid Until date has passed -
    # same "needs attention" treatment as a job alert, so a stale quote
    # doesn't sit there looking current until someone happens to open it.
    open_quotes = db.execute(
        f"""SELECT q.quote_id, q.quote_number, q.valid_until, q.status, c.customer_name FROM quotes q
            LEFT JOIN customers c ON c.customer_id = q.customer_id
            WHERE q.status IN ({",".join("?" for _ in QUOTE_OPEN_STATUSES)})
              AND q.valid_until IS NOT NULL AND q.valid_until != ''""",
        QUOTE_OPEN_STATUSES,
    ).fetchall()
    for q in open_quotes:
        if is_past_validity(q["valid_until"], q["status"]):
            msg = f"Valid until {q['valid_until'][:10]} has passed - review or re-quote before it's accepted at this price."
            all_alerts.append({
                "level": "amber", "type": "Quote Past Validity",
                "job_number": q["quote_number"], "job_name": q["customer_name"] or "",
                "message": msg, "quote_id": q["quote_id"],
                "alert_key": alert_key(q["quote_number"], "Quote Past Validity", msg),
            })
    if not include_accepted:
        all_alerts = [a for a in all_alerts if a["alert_key"] not in accepted]
    return all_alerts


def alert_key(number, alert_type, message):
    """Identifies one alert. The message carries the figures, so when a job's
    numbers move the alert becomes a new one and any earlier acceptance no longer applies."""
    return f"{number}|{alert_type}|{message}"


@app.route("/api/alerts/accept", methods=["POST"])
@permission_required("works_orders:write")
def accept_alert():
    data = request.get_json(force=True) or {}
    reason = (data.get("reason") or "").strip()
    if not reason:
        return jsonify({"error": "Please give a short reason for accepting this alert."}), 400
    db = get_db()
    live = [a for a in compute_all_alerts(db) if a["alert_key"] == data.get("alert_key")]
    if not live:
        return jsonify({"error": "That alert is no longer showing - the job's figures may have changed. Refresh and try again."}), 404
    a = live[0]
    db.execute(
        "INSERT OR IGNORE INTO alert_acceptances (alert_key, job_number, alert_type, message, reason, accepted_by) VALUES (?,?,?,?,?,?)",
        (a["alert_key"], a["job_number"], a["type"], a["message"], reason, current_user_name()))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/alerts/accepted")
@login_required
def list_accepted_alerts():
    db = get_db()
    return jsonify(rows_to_list(db.execute("SELECT * FROM alert_acceptances ORDER BY acceptance_id DESC").fetchall()))


@app.route("/api/alerts/accepted/<int:aid>", methods=["DELETE"])
@permission_required("works_orders:write")
def undo_accepted_alert(aid):
    db = get_db()
    db.execute("DELETE FROM alert_acceptances WHERE acceptance_id=?", (aid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/alerts")
@login_required
def get_alerts():
    """Lightweight alerts-only endpoint, fetched once at sign-in (see
    app.js's afterLogin()) so the sidebar's alert count is accurate from
    the moment someone logs in, without them needing to visit the
    Dashboard first. The Dashboard page keeps refreshing it whenever it's
    opened, same as before."""
    db = get_db()
    alerts = compute_all_alerts(db)
    return jsonify({"alerts": alerts, "count": len(alerts)})


@app.route("/api/dashboard")
@login_required
def dashboard():
    db = get_db()
    rows = db.execute(
        f"""SELECT wo.*, c.customer_name FROM works_orders wo
           JOIN customers c ON c.customer_id = wo.customer_id WHERE {PLACEHOLDER_JOB_SQL}"""
    ).fetchall()
    jobs = [works_order_with_metrics(db, r) for r in rows]

    total_revenue = sum(j["revenue"] for j in jobs)
    total_cost = sum(j["actual_cost"] for j in jobs)
    total_profit = total_revenue - total_cost
    margins = [j["margin_pct"] for j in jobs if j["revenue"] > 0]
    avg_margin = round(sum(margins) / len(margins), 2) if margins else 0.0
    active_statuses = {"Open", "In Progress"}
    active_jobs = sum(1 for j in jobs if j["status"] in active_statuses)
    loss_making_jobs = sum(1 for j in jobs if j["profit"] < 0)

    # Works order due-date pipeline: live jobs (same definition as Active
    # Jobs) due in each of the next three calendar months - discrete,
    # non-overlapping buckets by month, not cumulative windows. A job's
    # End Date puts it in exactly one bucket (whichever calendar month it
    # falls in) or none at all, if that month isn't one of the three
    # being shown. A job overdue from an earlier month doesn't roll into
    # "this month" - it's just not one of these three months.
    today = date.today()

    def month_key(months_ahead):
        month_index = today.month - 1 + months_ahead
        year = today.year + month_index // 12
        month = month_index % 12 + 1
        return (year, month)

    target_months = [month_key(i) for i in range(3)]  # this month, next, the one after
    live_jobs_with_dates = [j for j in jobs if j["status"] in active_statuses and j["end_date"]]

    def _parse_end_date(j):
        try:
            return datetime.strptime(j["end_date"][:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None

    due_counts = [0, 0, 0]
    for j in live_jobs_with_dates:
        ed = _parse_end_date(j)
        if ed is None:
            continue
        key = (ed.year, ed.month)
        if key in target_months:
            due_counts[target_months.index(key)] += 1

    # Late works orders: live jobs whose End Date has already passed.
    # Money is left out entirely for Service Engineers (the dashboard is not
    # in their menu, but the figures must not be reachable either way).
    late_rows = []
    for j in live_jobs_with_dates:
        ed = _parse_end_date(j)
        if ed is not None and ed < today:
            late_rows.append((j, (today - ed).days))
    late_rows.sort(key=lambda t: t[1], reverse=True)
    late = {"late_count": len(late_rows), "late_jobs": []}
    if not is_service_engineer():
        def _sum(key):
            return round(sum(j.get(key) or 0 for j, _ in late_rows), 2)
        late.update({
            "late_po_value": _sum("contract_value"),
            "late_purchases": _sum("supplier_invoice_total"),
            "late_hours": _sum("labour_total"),
            "late_stock": _sum("stock_cost"),
            "late_cost_total": _sum("actual_cost"),
            "late_avg_days": round(sum(d for _, d in late_rows) / len(late_rows)) if late_rows else 0,
            "late_jobs": [{
                "job_number": j["job_number"], "customer_name": j["customer_name"],
                "project_manager": j["project_manager"], "days_late": d,
                "po_value": j["contract_value"], "purchases": j["supplier_invoice_total"],
                "hours": j["labour_total"], "stock": j["stock_cost"], "cost": j["actual_cost"],
            } for j, d in late_rows[:10]],
        })

    due_month_labels = [f"{calendar.month_name[m]} {y}" for y, m in target_months]
    # Machine-readable YYYY-MM form of the same three months, so the
    # frontend can link each Due card straight to the matching filtered
    # Works Orders list without recomputing "this month" itself - which
    # would risk drifting out of sync with the server's own idea of today
    # (timezone differences, the clock ticking over between page load and
    # click, etc.). Keeping one source of truth for what "this month"
    # means avoids the two ever disagreeing.
    due_month_keys = [f"{y:04d}-{m:02d}" for y, m in target_months]

    total_stock_value = db.execute(
        """SELECT COALESCE(SUM(sa.quantity * si.purchase_cost), 0)
           FROM stock_allocations sa JOIN stock_items si ON si.stock_item_id = sa.stock_item_id"""
    ).fetchone()[0]

    # --- Dashboard chart area: deliberately always "this year to date," never
    # a user-adjustable range. A dashboard is a snapshot of how things stand
    # right now - comparing this year to a prior one, or the same month
    # across years, is a different question with a different answer shape
    # (a table of two periods side by side), and belongs on a dedicated
    # Reporting view if that's ever needed, not bolted onto this chart area
    # as a date picker.
    current_year = today.year
    year_jobs = job_profit_margin_for_year(db, current_year)

    jobs_by_profit_desc = sorted(year_jobs, key=lambda j: j["profit"], reverse=True)
    # Top is restricted to profit >= 0 - with real data (1,700+ jobs/year)
    # this never matters since there are always 15+ profitable jobs, but it
    # guarantees Top and Bottom can never share a job on a small/quiet
    # dataset (e.g. a new install) where fewer than 15 jobs have any profit
    # at all - without this, a loss-making job could be the "least bad" of
    # too few jobs and get listed as both a top performer and a loss.
    top_profit_jobs = [j for j in jobs_by_profit_desc if j["profit"] >= 0][:15]
    # "Bottom" is jobs actually LOSING money this year, worst first - not
    # simply the 15 lowest-profit jobs regardless of sign. With over a
    # thousand jobs active in a year, many are small (a single low-value
    # part), so a plain "lowest profit" cut fills up with jobs making a
    # positive but tiny profit (e.g. £18 profit on a £60 job) - technically
    # the bottom of the list, but not something that needs attention the
    # way a real loss does. Capped at 15 but never padded past the jobs
    # that are genuinely in loss, so this can legitimately come back empty
    # (a good thing) or with fewer than 15.
    loss_making_this_year = [j for j in year_jobs if j["profit"] < 0]
    bottom_profit_jobs = sorted(loss_making_this_year, key=lambda j: j["profit"])[:15]

    # Monthly revenue / cost / profit, this year only, Jan through the
    # current month - zero-filled so a quiet month still appears on the
    # x-axis rather than silently vanishing.
    monthly_revenue_rows = db.execute(
        """SELECT strftime('%Y-%m', invoice_date) AS ym, COALESCE(SUM(invoice_amount),0) AS total
           FROM customer_invoices WHERE status != 'Cancelled' AND strftime('%Y', invoice_date) = ?
           GROUP BY ym""", (str(current_year),)
    ).fetchall()
    monthly_supplier_cost = db.execute(
        """SELECT strftime('%Y-%m', invoice_date) AS ym, COALESCE(SUM(invoice_amount),0) AS total
           FROM supplier_invoices WHERE status != 'Cancelled' AND strftime('%Y', invoice_date) = ?
           GROUP BY ym""", (str(current_year),)
    ).fetchall()
    monthly_labour_cost = db.execute(
        """SELECT strftime('%Y-%m', work_date) AS ym, COALESCE(SUM(labour_cost),0) AS total
           FROM labour_entries WHERE strftime('%Y', work_date) = ?
           GROUP BY ym""", (str(current_year),)
    ).fetchall()

    revenue_by_month = {r["ym"]: r["total"] for r in monthly_revenue_rows if r["ym"]}
    cost_by_month = {}
    for r in monthly_supplier_cost:
        if not r["ym"]:
            continue
        cost_by_month[r["ym"]] = cost_by_month.get(r["ym"], 0) + r["total"]
    for r in monthly_labour_cost:
        if not r["ym"]:
            continue
        cost_by_month[r["ym"]] = cost_by_month.get(r["ym"], 0) + r["total"]

    month_keys = [f"{current_year}-{m:02d}" for m in range(1, today.month + 1)]
    monthly_trend = []
    for ym in month_keys:
        rev = round(revenue_by_month.get(ym, 0), 2)
        cost = round(cost_by_month.get(ym, 0), 2)
        monthly_trend.append({"month": ym, "revenue": rev, "cost": cost, "profit": round(rev - cost, 2)})

    all_alerts = compute_all_alerts(db, jobs=jobs)

    return jsonify({
        "total_revenue": round(total_revenue, 2),
        "total_cost": round(total_cost, 2),
        "total_profit": round(total_profit, 2),
        "avg_margin": avg_margin,
        "active_jobs": active_jobs,
        "loss_making_jobs": loss_making_jobs,
        "live_works_orders": active_jobs,
        "due_this_month": due_counts[0],
        "due_next_month": due_counts[1],
        "due_month_after": due_counts[2],
        "due_month_labels": due_month_labels,
        "due_month_keys": due_month_keys,
        "total_stock_value": round(total_stock_value, 2),
        **late,
        "current_year": current_year,
        "top_profit_jobs": top_profit_jobs,
        "bottom_profit_jobs": bottom_profit_jobs,
        "job_scatter": year_jobs,
        "monthly_trend": monthly_trend,
        "alerts": all_alerts,
    })


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def build_report_rows(db):
    rows = db.execute(
        f"""SELECT wo.*, c.customer_name FROM works_orders wo
           JOIN customers c ON c.customer_id = wo.customer_id WHERE {PLACEHOLDER_JOB_SQL} ORDER BY wo.job_number"""
    ).fetchall()
    report = []
    for r in rows:
        m = compute_job_metrics(db, r)
        report.append({
            "job_number": r["job_number"],
            "customer_name": r["customer_name"],
            "contract_value": r["contract_value"],
            "actual_cost": m["actual_cost"],
            "committed_cost": m["committed_cost"],
            "forecast_cost": m["forecast_cost"],
            "revenue": m["revenue"],
            "profit": m["profit"],
            "forecast_profit": m["forecast_profit"],
            "margin_pct": m["margin_pct"],
            "margin_target": m["margin_target"],
            "status": r["status"],
            "contract_code": r["contract_code"],
            "location": r["location"] if "location" in r.keys() else None,
            # True once at least one non-cancelled Customer Invoice has been
            # recorded against this job - i.e. m["revenue"] (which is that
            # same sum, see compute_job_metrics) is greater than zero. Drives
            # the "Completed, Not Yet Invoiced" report: a job whose Revenue
            # is still £0 has never actually been invoiced, whatever its
            # Status says.
            "invoiced": m["revenue"] > 0,
        })
    return report


@app.route("/api/reporting")
@login_required
def reporting():
    db = get_db()
    return jsonify(build_report_rows(db))


@app.route("/api/reporting/export")
@login_required
def reporting_export():
    db = get_db()
    report = build_report_rows(db)
    # Mirrors the Reporting page's own client-side filters (see repState in
    # app.js) so a CSV downloaded while a filter is active reflects what's
    # actually on screen, rather than always dumping every job regardless
    # of the current search/customer/contract-code/location/loss-making/
    # not-yet-invoiced selection.
    q = request.args.get("q", "").strip().lower()
    customer = request.args.get("customer", "").strip()
    code = request.args.get("code", "").strip()
    location = request.args.get("location", "").strip()
    loss_only = request.args.get("loss_only", "").strip() == "1"
    not_invoiced = request.args.get("not_invoiced", "").strip() == "1"
    if q:
        report = [r for r in report if q in r["job_number"].lower() or q in r["customer_name"].lower()
                  or q in (r["contract_code"] or "").lower()]
    if customer:
        report = [r for r in report if r["customer_name"] == customer]
    if code:
        report = [r for r in report if (r["contract_code"] or "") == code]
    if location:
        report = [r for r in report if (r["location"] or "") == location]
    if loss_only:
        report = [r for r in report if r["profit"] < 0]
    if not_invoiced:
        # Same contract_value > 0 exclusion as the Reporting page's own
        # client-side filter - a £0 "No Charge" job has nothing to invoice,
        # so it shouldn't show up as an outstanding one.
        report = [r for r in report if r["status"] == "Completed" and not r["invoiced"] and r["contract_value"] > 0]
    output = io.StringIO()
    # (dict key in each report row, display column header) - keeps the CSV
    # header text in sync with the Reporting page's on-screen column names
    # without renaming the underlying field names used elsewhere (Works
    # Orders, Dashboard, the JSON API).
    columns = [
        ("job_number", "job_number"), ("customer_name", "customer_name"),
        ("contract_value", "purchase_order_value"), ("actual_cost", "actual_cost"),
        ("committed_cost", "total_purchases"), ("forecast_cost", "quoted"),
        ("revenue", "revenue"), ("profit", "profit"), ("forecast_profit", "forecast_profit"),
        ("margin_pct", "margin_pct"), ("margin_target", "margin_target"),
        ("status", "status"), ("contract_code", "contract_code"),
        ("location", "location"), ("invoiced", "invoiced"),
    ]
    writer = csv.writer(output)
    writer.writerow([header for _, header in columns])
    for row in report:
        writer.writerow([row.get(key, "") for key, _ in columns])
    log_document_access(db, "Reporting CSV export", f"{len(report)} job(s)")
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=job_profitability_report.csv"},
    )


# Spend by Supplier: total non-cancelled supplier invoice spend per
# supplier, independent of which job(s) it was raised against - the Job
# Profitability report above already breaks cost down per job, but doesn't
# answer "who are we spending the most with overall". Every supplier is
# returned (LEFT JOIN), including ones with no invoices yet at all, so the
# frontend can offer an "include suppliers with no spend" toggle rather
# than silently hiding them - the status filter lives in the JOIN clause
# itself rather than a WHERE so a supplier with only Cancelled invoices
# still shows up with zero spend instead of vanishing.
def build_spend_by_supplier(db, year=""):
    params = []
    year_clause = ""
    if year:
        year_clause = "AND strftime('%Y', si.invoice_date) = ?"
        params.append(year)
    rows = db.execute(
        f"""SELECT s.supplier_id, s.supplier_name,
                   COUNT(si.supplier_invoice_id) AS invoice_count,
                   COALESCE(SUM(si.invoice_amount), 0) AS total_spend,
                   MAX(si.invoice_date) AS last_invoice_date
            FROM suppliers s
            LEFT JOIN supplier_invoices si
                   ON si.supplier_id = s.supplier_id AND si.status != 'Cancelled' {year_clause}
            GROUP BY s.supplier_id
            ORDER BY s.supplier_name""",
        params,
    ).fetchall()
    years = db.execute(
        "SELECT DISTINCT strftime('%Y', invoice_date) AS yr FROM supplier_invoices "
        "WHERE invoice_date IS NOT NULL AND status != 'Cancelled' ORDER BY yr DESC"
    ).fetchall()
    return {
        "rows": [{
            "supplier_id": r["supplier_id"],
            "supplier_name": r["supplier_name"],
            "invoice_count": r["invoice_count"],
            "total_spend": round(r["total_spend"], 2),
            "last_invoice_date": r["last_invoice_date"],
        } for r in rows],
        "years": [y["yr"] for y in years if y["yr"]],
    }


@app.route("/api/reporting/spend-by-supplier")
@login_required
def reporting_spend_by_supplier():
    db = get_db()
    return jsonify(build_spend_by_supplier(db, request.args.get("year", "").strip()))


@app.route("/api/reporting/spend-by-supplier/export")
@login_required
def reporting_spend_by_supplier_export():
    db = get_db()
    year = request.args.get("year", "").strip()
    data = build_spend_by_supplier(db, year)
    rows = data["rows"]
    if not request.args.get("include_zero"):
        rows = [r for r in rows if r["invoice_count"] > 0]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Supplier", "Invoices", "Total Spend", "Last Invoice Date"])
    for r in rows:
        writer.writerow([r["supplier_name"], r["invoice_count"], f"{r['total_spend']:.2f}", r["last_invoice_date"] or ""])
    log_document_access(db, "Reporting CSV export", f"Spend by Supplier ({year or 'all years'}), {len(rows)} supplier(s)")
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=spend_by_supplier.csv"},
    )


# Monthly Income & Spend: the Dashboard's own "this year to date" chart
# (see monthly_trend in dashboard() above) generalised into a standalone
# report that can look at any year with data, not just the current one -
# same definitions (Income = non-cancelled Customer Invoices; Spend =
# non-cancelled Supplier Invoices + Labour Entries, by invoice/work date),
# kept deliberately identical so the two views never disagree with each
# other about what a given month's numbers are.
def build_monthly_income_spend(db, requested_year=""):
    years_rows = db.execute(
        """SELECT DISTINCT strftime('%Y', d) AS yr FROM (
               SELECT invoice_date AS d FROM customer_invoices WHERE status != 'Cancelled'
               UNION ALL
               SELECT invoice_date AS d FROM supplier_invoices WHERE status != 'Cancelled'
               UNION ALL
               SELECT work_date AS d FROM labour_entries
           ) WHERE d IS NOT NULL"""
    ).fetchall()
    current_year = date.today().year
    years = sorted({y["yr"] for y in years_rows if y["yr"]}, reverse=True)
    if str(current_year) not in years:
        years.insert(0, str(current_year))  # always offer the current year, even on a fresh install with no data yet
        years.sort(reverse=True)

    year = requested_year if requested_year in years else years[0]

    income_rows = db.execute(
        """SELECT strftime('%Y-%m', invoice_date) AS ym, COALESCE(SUM(invoice_amount),0) AS total
           FROM customer_invoices WHERE status != 'Cancelled' AND strftime('%Y', invoice_date) = ?
           GROUP BY ym""", (year,)
    ).fetchall()
    supplier_cost_rows = db.execute(
        """SELECT strftime('%Y-%m', invoice_date) AS ym, COALESCE(SUM(invoice_amount),0) AS total
           FROM supplier_invoices WHERE status != 'Cancelled' AND strftime('%Y', invoice_date) = ?
           GROUP BY ym""", (year,)
    ).fetchall()
    labour_cost_rows = db.execute(
        """SELECT strftime('%Y-%m', work_date) AS ym, COALESCE(SUM(labour_cost),0) AS total
           FROM labour_entries WHERE strftime('%Y', work_date) = ?
           GROUP BY ym""", (year,)
    ).fetchall()

    income_by_month = {r["ym"]: r["total"] for r in income_rows if r["ym"]}
    spend_by_month = {}
    for r in supplier_cost_rows:
        if r["ym"]:
            spend_by_month[r["ym"]] = spend_by_month.get(r["ym"], 0) + r["total"]
    for r in labour_cost_rows:
        if r["ym"]:
            spend_by_month[r["ym"]] = spend_by_month.get(r["ym"], 0) + r["total"]

    # A past/complete year always shows all 12 months; the current year
    # stops at the current month, same zero-filled-but-not-padded-into-
    # the-future approach as the Dashboard chart.
    last_month = date.today().month if int(year) == current_year else 12
    months = []
    for m in range(1, last_month + 1):
        ym = f"{year}-{m:02d}"
        income = round(income_by_month.get(ym, 0), 2)
        spend = round(spend_by_month.get(ym, 0), 2)
        months.append({"month": ym, "income": income, "spend": spend, "net": round(income - spend, 2)})

    totals = {
        "income": round(sum(m["income"] for m in months), 2),
        "spend": round(sum(m["spend"] for m in months), 2),
    }
    totals["net"] = round(totals["income"] - totals["spend"], 2)

    return {"year": year, "years": years, "months": months, "totals": totals}


@app.route("/api/reporting/monthly-income-spend")
@login_required
def reporting_monthly_income_spend():
    db = get_db()
    return jsonify(build_monthly_income_spend(db, request.args.get("year", "").strip()))


@app.route("/api/reporting/monthly-income-spend/export")
@login_required
def reporting_monthly_income_spend_export():
    db = get_db()
    data = build_monthly_income_spend(db, request.args.get("year", "").strip())
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Month", "Income", "Spend", "Net"])
    for m in data["months"]:
        writer.writerow([fmt_month_for_csv(m["month"]), f"{m['income']:.2f}", f"{m['spend']:.2f}", f"{m['net']:.2f}"])
    writer.writerow(["Total", f"{data['totals']['income']:.2f}", f"{data['totals']['spend']:.2f}", f"{data['totals']['net']:.2f}"])
    log_document_access(db, "Reporting CSV export", f"Monthly Income & Spend ({data['year']})")
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=monthly_income_spend_{data['year']}.csv"},
    )


def fmt_month_for_csv(ym):
    try:
        y, m = ym.split("-")
        return f"{calendar.month_name[int(m)]} {y}"
    except (ValueError, IndexError):
        return ym


# --------------------------------------------------------------------------
# Summary panels at the top of each screen (Quotes, Works Orders, Purchase
# Orders, Goods In, Despatch Notes, Customers, Suppliers). One call returns
# every figure so a screen needs a single quick fetch. Money is left out for
# Service Engineers, and so are the Quotes / Customers / Suppliers sections
# they have no access to.
# --------------------------------------------------------------------------
@app.route("/api/screen-summaries")
@login_required
def screen_summaries():
    db = get_db()
    today = date.today()
    year = str(today.year)
    month = today.strftime("%Y-%m")
    engineer = is_service_engineer()
    one = lambda sql, params=(): db.execute(sql, params).fetchone()

    def money(v):
        return None if engineer else round(v or 0, 2)

    out = {"year": year}

    if not engineer:
        sent = one("SELECT COUNT(*), COALESCE(SUM(estimated_value),0) FROM quotes WHERE status='Sent'")
        won = one("SELECT COUNT(*), COALESCE(SUM(estimated_value),0) FROM quotes "
                  "WHERE status='Won' AND substr(COALESCE(quote_date, created_date),1,4)=?", (year,))
        out["quotes"] = {"awaiting_count": sent[0], "awaiting_value": round(sent[1], 2),
                         "po_received_count": won[0], "po_received_value": round(won[1], 2)}

    wo_open = one(f"""SELECT COUNT(*), COALESCE(SUM(contract_value),0) FROM works_orders wo
                      WHERE {PLACEHOLDER_JOB_SQL} AND status IN ('Open','In Progress')""")
    wo_done = one(f"""SELECT COUNT(*), COALESCE(SUM(contract_value),0) FROM works_orders wo
                      WHERE {PLACEHOLDER_JOB_SQL} AND status='Completed' AND substr(end_date,1,4)=?""", (year,))
    out["works_orders"] = {"open_count": wo_open[0], "open_value": money(wo_open[1]),
                           "completed_ytd_count": wo_done[0], "completed_ytd_value": money(wo_done[1])}

    live_po = "po.is_historic=0 AND po.status IN ('Issued','Approved')"
    awaiting = one(f"""SELECT COUNT(*), COALESCE(SUM(po.po_value),0) FROM purchase_orders po
                       WHERE {live_po} AND NOT EXISTS (SELECT 1 FROM goods_in g WHERE g.purchase_order_id=po.purchase_order_id)""")
    part = one(f"""SELECT COUNT(*), COALESCE(SUM(po.po_value),0) FROM purchase_orders po
                   WHERE {live_po}
                     AND EXISTS (SELECT 1 FROM goods_in g WHERE g.purchase_order_id=po.purchase_order_id)
                     AND NOT EXISTS (SELECT 1 FROM goods_in g WHERE g.purchase_order_id=po.purchase_order_id
                                     AND g.quantity_status='Full')""")
    out["purchase_orders"] = {"awaiting_count": awaiting[0], "awaiting_value": money(awaiting[1]),
                              "part_received_count": part[0]}

    out["goods_in"] = {
        "total": one("SELECT COUNT(*) FROM goods_in")[0],
        "ytd": one("SELECT COUNT(*) FROM goods_in WHERE substr(received_date,1,4)=?", (year,))[0],
        "month": one("SELECT COUNT(*) FROM goods_in WHERE substr(received_date,1,7)=?", (month,))[0],
    }

    desp = "FROM despatch_notes WHERE status='Despatched'"
    out["despatch_notes"] = {
        "jobs_complete": one(f"SELECT COUNT(DISTINCT works_order_id) {desp}")[0],
        "ytd": one(f"SELECT COUNT(DISTINCT works_order_id) {desp} AND substr(despatch_date,1,4)=?", (year,))[0],
        "month": one(f"SELECT COUNT(DISTINCT works_order_id) {desp} AND substr(despatch_date,1,7)=?", (month,))[0],
    }

    if not engineer:
        out["customers"] = {"total": one("SELECT COUNT(*) FROM customers")[0]}
        out["suppliers"] = {"total": one("SELECT COUNT(*) FROM suppliers")[0]}
    return jsonify(out)


# Hours Booked report: hours from Labour Entries (the "Hours Quoted & Booked"
# screen), by month for one year, grouped by employee, job, customer or
# contract code. Filters narrow the entries before they are grouped.
HOURS_GROUPS = {"employee", "job", "customer", "contract_code"}


def build_hours_booked(db, args):
    years = [r[0] for r in db.execute(
        "SELECT DISTINCT substr(work_date,1,4) FROM labour_entries WHERE work_date IS NOT NULL AND work_date != '' "
        "ORDER BY 1 DESC").fetchall() if r[0]]
    current_year = str(date.today().year)
    if current_year not in years:
        years.insert(0, current_year)
    year = args.get("year", "").strip()
    year = year if year in years else years[0]
    group = args.get("group", "employee").strip()
    group = group if group in HOURS_GROUPS else "employee"
    employee = args.get("employee", "").strip()
    customer = args.get("customer", "").strip()
    code = args.get("contract_code", "").strip()

    sql = f"""SELECT le.employee_name, le.hours_worked, substr(le.work_date,6,2) AS mm, wo.works_order_id,
                     wo.job_number, wo.contract_code, c.customer_name
              FROM labour_entries le
              JOIN works_orders wo ON wo.works_order_id = le.works_order_id
              JOIN customers c ON c.customer_id = wo.customer_id
              WHERE {PLACEHOLDER_JOB_SQL} AND substr(le.work_date,1,4) = ?"""
    params = [year]
    if employee:
        sql += " AND le.employee_name = ?"; params.append(employee)
    if customer:
        sql += " AND c.customer_name = ?"; params.append(customer)
    if code:
        sql += " AND wo.contract_code = ?"; params.append(code)
    entries = db.execute(sql, params).fetchall()

    rows, jobs = {}, set()
    for e in entries:
        key = {"employee": e["employee_name"], "job": e["job_number"],
               "customer": e["customer_name"], "contract_code": e["contract_code"] or "(none)"}[group]
        r = rows.setdefault(key, {"name": key, "months": [0.0] * 12, "total": 0.0})
        try:
            mi = int(e["mm"]) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= mi < 12:
            hrs = e["hours_worked"] or 0
            r["months"][mi] += hrs
            r["total"] += hrs
            jobs.add(e["works_order_id"])
    out_rows = sorted(rows.values(), key=lambda r: r["total"], reverse=True)
    for r in out_rows:
        r["months"] = [round(h, 2) for h in r["months"]]
        r["total"] = round(r["total"], 2)
    total = round(sum(r["total"] for r in out_rows), 2)
    employees = [r[0] for r in db.execute("SELECT DISTINCT employee_name FROM labour_entries ORDER BY 1").fetchall()]
    return {"year": year, "years": years, "group": group, "rows": out_rows, "employees": employees,
            "totals": {"hours": total, "jobs": len(jobs),
                       "avg_per_job": round(total / len(jobs), 1) if jobs else 0}}


@app.route("/api/reporting/hours-booked")
@login_required
def reporting_hours_booked():
    return jsonify(build_hours_booked(get_db(), request.args))


@app.route("/api/reporting/hours-booked/export")
@login_required
def reporting_hours_booked_export():
    db = get_db()
    data = build_hours_booked(db, request.args)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([data["group"].replace("_", " ").title()] + [calendar.month_abbr[m] for m in range(1, 13)] + ["Total hours"])
    for r in data["rows"]:
        writer.writerow([r["name"]] + [f"{h:g}" for h in r["months"]] + [f"{r['total']:g}"])
    writer.writerow(["Total"] + [""] * 12 + [f"{data['totals']['hours']:g}"])
    log_document_access(db, "Reporting CSV export", f"Hours Booked ({data['year']}, by {data['group']})")
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=hours_booked_{data['year']}.csv"})


# Income by Contract Code: the same Revenue figure as the Job Profitability
# report (non-cancelled Customer Invoices against a job), grouped by
# Contract Code instead of by job - and, since Income ultimately rolls up
# to whoever is running the job, filterable by the Works Order's own
# Project Manager field (the same "person" field already used as a filter
# everywhere else a job underpins the record - see the cross-cutting
# filter dropdowns on Quotes/Works Orders/POs/etc.). Raw per-job rows are
# returned rather than pre-aggregated, same pattern as /api/reporting,
# so the frontend can filter by person/customer/code and re-aggregate
# without another round trip.
@app.route("/api/reporting/income-by-contract-code")
@login_required
def reporting_income_by_contract_code():
    db = get_db()
    rows = db.execute(
        f"""SELECT wo.works_order_id, wo.job_number, wo.contract_code, wo.project_manager,
                  c.customer_name,
                  COALESCE((SELECT SUM(ci.invoice_amount) FROM customer_invoices ci
                            WHERE ci.works_order_id = wo.works_order_id
                              AND ci.status != 'Cancelled'), 0) AS revenue,
                  (SELECT COUNT(*) FROM customer_invoices ci
                   WHERE ci.works_order_id = wo.works_order_id
                     AND ci.status != 'Cancelled') AS invoice_count
           FROM works_orders wo
           JOIN customers c ON c.customer_id = wo.customer_id
           WHERE {PLACEHOLDER_JOB_SQL}
           ORDER BY wo.job_number"""
    ).fetchall()
    project_managers = sorted({r["project_manager"] for r in rows if r["project_manager"]})
    return jsonify({
        "rows": [{
            "job_number": r["job_number"],
            "contract_code": r["contract_code"],
            "project_manager": r["project_manager"],
            "customer_name": r["customer_name"],
            "revenue": round(r["revenue"], 2),
            "invoice_count": r["invoice_count"],
        } for r in rows],
        "project_managers": project_managers,
    })


# --------------------------------------------------------------------------
# Users (Administrator only)
# --------------------------------------------------------------------------
@app.route("/api/users", methods=["GET"])
@permission_required("all")
def list_users():
    db = get_db()
    rows = db.execute(
        "SELECT user_id, username, full_name, role, email, created_date, must_change_password, totp_enabled "
        "FROM users ORDER BY full_name"
    ).fetchall()
    return jsonify(rows_to_list(rows))


@app.route("/api/users", methods=["POST"])
@permission_required("all")
def create_user():
    data = request.get_json(force=True) or {}
    explicit_password = data.get("password") or None
    # Only an explicitly-typed password needs this check - an auto-generated
    # one (generate_temporary_password(), ~12 random URL-safe characters) is
    # already far stronger than this minimum.
    if explicit_password and len(explicit_password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400
    auto_generated = not explicit_password
    password = explicit_password or generate_temporary_password()
    db = get_db()
    try:
        # Every new account starts with must_change_password set, whether
        # the password was auto-generated or chosen by whoever set it up -
        # either way it's a password someone else picked, not the account
        # holder's own, so it gets replaced before the account is used.
        cur = db.execute(
            "INSERT INTO users (username, password_hash, full_name, role, email, must_change_password) VALUES (?,?,?,?,?,1)",
            (data.get("username"), generate_password_hash(password), data.get("full_name"), data.get("role"), data.get("email")),
        )
        db.commit()
    except sqlite3.IntegrityError as e:
        return jsonify({"error": str(e)}), 400
    resp = {"user_id": cur.lastrowid}
    if auto_generated:
        resp["temporary_password"] = password
    return jsonify(resp), 201


@app.route("/api/users/<int:uid>", methods=["PUT"])
@permission_required("all")
def update_user(uid):
    data = request.get_json(force=True) or {}
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    if not target:
        return jsonify({"error": "User not found"}), 404

    new_role = data.get("role", target["role"])
    # Same safety net as delete: don't let the last Administrator demote
    # themselves (or be demoted) with no other admin left to undo it.
    if target["role"] == "Administrator" and new_role != "Administrator":
        admin_count = db.execute(
            "SELECT COUNT(*) FROM users WHERE role='Administrator'"
        ).fetchone()[0]
        if admin_count <= 1:
            return jsonify({
                "error": "Can't change the role of the last remaining Administrator - "
                         "create another admin account first, or promote someone else to Administrator."
            }), 400

    new_email = data.get("email", target["email"])
    new_password = data.get("password") or None  # blank/omitted = keep current password
    if new_password and len(new_password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400

    try:
        if new_password:
            # A password set here was chosen by whoever's doing the reset,
            # not the account holder - same reasoning as a brand new
            # account, so it must be changed again on their next sign-in.
            db.execute(
                "UPDATE users SET role=?, email=?, password_hash=?, must_change_password=1 WHERE user_id=?",
                (new_role, new_email, generate_password_hash(new_password), uid),
            )
        else:
            db.execute(
                "UPDATE users SET role=?, email=? WHERE user_id=?",
                (new_role, new_email, uid),
            )
        db.commit()
    except sqlite3.IntegrityError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/users/<int:uid>", methods=["DELETE"])
@permission_required("all")
def delete_user(uid):
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    if not target:
        return jsonify({"error": "User not found"}), 404
    if target["role"] == "Administrator":
        admin_count = db.execute(
            "SELECT COUNT(*) FROM users WHERE role='Administrator'"
        ).fetchone()[0]
        if admin_count <= 1:
            return jsonify({
                "error": "Can't delete the last remaining Administrator - "
                         "create another admin account first, or promote someone else to Administrator."
            }), 400
    db.execute("DELETE FROM users WHERE user_id=?", (uid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/users/<int:uid>/reset-2fa", methods=["POST"])
@permission_required("all")
def reset_2fa(uid):
    """Administrator action for e.g. a lost or replaced phone with no
    backup codes to hand - wipes the account's current secret, its
    enabled flag, and any remaining backup codes, so their next login
    goes through 2FA setup again from scratch. Doesn't touch their
    password."""
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    if not target:
        return jsonify({"error": "User not found"}), 404
    db.execute("UPDATE users SET totp_secret=NULL, totp_enabled=0 WHERE user_id=?", (uid,))
    db.execute("DELETE FROM totp_backup_codes WHERE user_id=?", (uid,))
    db.commit()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Go-live reset (Administrator only) - wipes every job transaction so the
# app can start again with a genuinely clean slate, e.g. moving from a
# testing/trial period into real day-to-day use. See RESET_JOB_DATA_TABLES
# below for exactly what that does and doesn't touch.
# --------------------------------------------------------------------------
# Every table this clears, in an order that always satisfies its foreign
# keys (a child table - one with a reference to another table in this same
# list - always comes before the table it references). goods_in_documents,
# quote_labour_lines, quote_purchase_lines, purchase_order_lines, and
# despatch_note_lines aren't listed because each has ON DELETE CASCADE back
# to one of these, so deleting the parent row removes them automatically.
# Reference data (Customers, Suppliers, Contract Codes, Rate Card,
# Settings, Ship To addresses, user accounts) and every audit/security
# trail (audit_log, login_events, document_access_log) are deliberately
# left out - this is a reset of job history, not of the whole database.
RESET_JOB_DATA_TABLES = [
    "quotes", "goods_in", "supplier_invoices", "despatch_notes",
    "customer_invoices", "labour_entries", "purchase_orders", "works_orders",
]


@app.route("/api/admin/reset-job-data", methods=["POST"])
@permission_required("all")
def reset_job_data():
    """Wipes every job transaction (see RESET_JOB_DATA_TABLES above, plus
    the stock allocations/movements that were tied to one of those jobs)
    so the app can go live with a clean slate, while leaving reference
    data and every audit/security trail completely untouched. Requires
    the exact confirmation phrase (RESET_JOB_DATA_CONFIRMATION) typed by
    the caller, and takes a fresh backup first via the same backup_db()
    every app startup already uses - see db_location.py and
    README.md's "Deploying updates safely" for how to restore from one
    if this is ever run by mistake."""
    data = request.get_json(force=True) or {}
    if (data.get("confirm") or "").strip() != RESET_JOB_DATA_CONFIRMATION:
        return jsonify({"error": f'Type "{RESET_JOB_DATA_CONFIRMATION}" exactly to confirm.'}), 400

    db = get_db()
    db_location.backup_db()

    counts = {
        table: db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        for table in RESET_JOB_DATA_TABLES
    }
    counts["stock_allocations"] = db.execute(
        "SELECT COUNT(*) AS n FROM stock_allocations WHERE works_order_id IS NOT NULL"
    ).fetchone()["n"]
    counts["stock_movements"] = db.execute(
        "SELECT COUNT(*) AS n FROM stock_movements "
        "WHERE from_works_order_id IS NOT NULL OR to_works_order_id IS NOT NULL"
    ).fetchone()["n"]

    doc_files = [r["stored_filename"] for r in
                 db.execute("SELECT stored_filename FROM goods_in_documents").fetchall()]

    # works_orders is deleted last of all: stock_allocations/stock_movements
    # reference it without ON DELETE CASCADE (unlike the others, which
    # cascade or are deleted in dependency order above), so those rows
    # must be gone first or this would fail the foreign-key check.
    for table in RESET_JOB_DATA_TABLES:
        if table != "works_orders":
            db.execute(f"DELETE FROM {table}")
    db.execute("DELETE FROM stock_allocations WHERE works_order_id IS NOT NULL")
    db.execute(
        "DELETE FROM stock_movements "
        "WHERE from_works_order_id IS NOT NULL OR to_works_order_id IS NOT NULL"
    )
    db.execute("DELETE FROM works_orders")
    db.execute("DELETE FROM alert_acceptances")
    db.execute(
        """INSERT INTO audit_log (table_name, record_id, field_name, old_value, new_value, changed_by)
           VALUES ('system', 0, 'reset_job_data', ?, 'reset for go-live', ?)""",
        (", ".join(f"{k}: {v}" for k, v in counts.items()), current_user_name()),
    )
    db.commit()

    for stored_name in doc_files:
        try:
            os.remove(_goods_in_doc_path(stored_name))
        except OSError:
            pass

    return jsonify({"ok": True, "deleted": counts})


# --------------------------------------------------------------------------
# Settings - margin targets (Administrator only)
# --------------------------------------------------------------------------
@app.route("/api/settings/margin-targets", methods=["GET"])
@login_required
def get_margin_targets():
    db = get_db()
    default_target, code_targets = get_margin_settings(db)
    # Job counts per code give useful context in the Settings screen without
    # a second round trip from the frontend.
    counts = {
        (row["contract_code"] or "").strip().upper(): row["n"]
        for row in db.execute(
            "SELECT contract_code, COUNT(*) AS n FROM works_orders WHERE contract_code IS NOT NULL GROUP BY contract_code"
        ).fetchall()
    }
    pricing_rows = db.execute("SELECT contract_code, standard_value, standard_cost FROM contract_code_targets").fetchall()
    pricing = {r["contract_code"]: (r["standard_value"], r["standard_cost"]) for r in pricing_rows}
    targets = [
        {"contract_code": code, "target_pct": pct, "job_count": counts.get(code, 0),
         "standard_value": pricing.get(code, (None, None))[0], "standard_cost": pricing.get(code, (None, None))[1]}
        for code, pct in sorted(code_targets.items())
    ]
    return jsonify({"default_target": default_target, "contract_code_targets": targets})


@app.route("/api/settings/margin-targets/default", methods=["PUT"])
@permission_required("all")
def update_default_margin_target():
    data = request.get_json(force=True) or {}
    target = safe_float(data.get("target_pct"), None)
    if target is None or target < 0 or target > 100:
        return jsonify({"error": "target_pct must be a number between 0 and 100"}), 400
    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('default_margin_target', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(target),),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/settings/margin-targets/<code>", methods=["PUT"])
@permission_required("all")
def upsert_contract_code_target(code):
    data = request.get_json(force=True) or {}
    target = safe_float(data.get("target_pct"), None)
    if target is None or target < 0 or target > 100:
        return jsonify({"error": "target_pct must be a number between 0 and 100"}), 400
    code = code.strip().upper()
    if not code:
        return jsonify({"error": "Contract code is required"}), 400
    standard_value = data.get("standard_value")
    standard_cost = data.get("standard_cost")
    standard_value = safe_float(standard_value) if standard_value not in (None, "") else None
    standard_cost = safe_float(standard_cost) if standard_cost not in (None, "") else None
    db = get_db()
    db.execute(
        "INSERT INTO contract_code_targets (contract_code, target_pct, standard_value, standard_cost) VALUES (?,?,?,?) "
        "ON CONFLICT(contract_code) DO UPDATE SET target_pct=excluded.target_pct, standard_value=excluded.standard_value, standard_cost=excluded.standard_cost",
        (code, target, standard_value, standard_cost),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/settings/margin-targets/<code>", methods=["DELETE"])
@permission_required("all")
def delete_contract_code_target(code):
    db = get_db()
    db.execute("DELETE FROM contract_code_targets WHERE contract_code=?", (code.strip().upper(),))
    db.commit()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Settings - Ship To addresses (Administrator only to manage)
# --------------------------------------------------------------------------
@app.route("/api/settings/ship-to-addresses", methods=["GET"])
@login_required
def list_ship_to_addresses():
    db = get_db()
    rows = db.execute("SELECT * FROM ship_to_addresses ORDER BY label").fetchall()
    return jsonify(rows_to_list(rows))


@app.route("/api/settings/ship-to-addresses", methods=["POST"])
@permission_required("all")
def create_ship_to_address():
    data = request.get_json(force=True) or {}
    label = (data.get("label") or "").strip()
    if not label:
        return jsonify({"error": "Label is required"}), 400
    db = get_db()
    try:
        cur = db.execute(
            """INSERT INTO ship_to_addresses (label, company_name, address_line1, address_line2, city, postcode)
               VALUES (?,?,?,?,?,?)""",
            (label, data.get("company_name"), data.get("address_line1"), data.get("address_line2"),
             data.get("city"), data.get("postcode")),
        )
        db.commit()
    except sqlite3.IntegrityError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ship_to_id": cur.lastrowid}), 201


@app.route("/api/settings/ship-to-addresses/<int:sid>", methods=["PUT"])
@permission_required("all")
def update_ship_to_address(sid):
    data = request.get_json(force=True) or {}
    label = (data.get("label") or "").strip()
    if not label:
        return jsonify({"error": "Label is required"}), 400
    db = get_db()
    try:
        db.execute(
            """UPDATE ship_to_addresses SET label=?, company_name=?, address_line1=?, address_line2=?,
               city=?, postcode=? WHERE ship_to_id=?""",
            (label, data.get("company_name"), data.get("address_line1"), data.get("address_line2"),
             data.get("city"), data.get("postcode"), sid),
        )
        db.commit()
    except sqlite3.IntegrityError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/settings/ship-to-addresses/<int:sid>", methods=["DELETE"])
@permission_required("all")
def delete_ship_to_address(sid):
    db = get_db()
    try:
        db.execute("DELETE FROM ship_to_addresses WHERE ship_to_id=?", (sid,))
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "This address is used on one or more Purchase Orders and can't be deleted."}), 400
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Reference data for dropdowns
# --------------------------------------------------------------------------
# ---- Version, and the optional "update available" notice ----
# Off unless whoever hosts the app sets TEAMWORKS360_UPDATE_URL to a small JSON file
# ({"version": "1.1.0", "notes": "..."}). Only the server operator can set it (it is
# an environment variable, not something editable in the app), only https is used,
# and failure is silent - the app never depends on it.
_UPDATE_CACHE = {"at": 0, "data": None}
UPDATE_CHECK_SECONDS = 6 * 60 * 60


def fetch_latest_release():
    url = os.environ.get("TEAMWORKS360_UPDATE_URL", "").strip()
    if not url.startswith("https://"):
        return None
    now = time.time()
    if _UPDATE_CACHE["data"] is not None and now - _UPDATE_CACHE["at"] < UPDATE_CHECK_SECONDS:
        return _UPDATE_CACHE["data"]
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=5) as resp:
            payload = json.loads(resp.read(65536).decode("utf-8"))
        latest = str(payload.get("version", ""))
        data = {"version": latest, "notes": str(payload.get("notes", ""))[:500]} \
            if app_version.parse_version(latest) else None
    except Exception:
        data = None
    _UPDATE_CACHE.update(at=now, data=data)
    return data


@app.route("/api/version")
@login_required
def get_version():
    out = {"version": app_version.APP_VERSION, "build_date": app_version.APP_BUILD_DATE,
           "update_available": False}
    if session.get("role") == "Administrator":
        latest = fetch_latest_release()
        if latest and app_version.is_newer(latest["version"], app_version.APP_VERSION):
            out.update(update_available=True, latest_version=latest["version"], notes=latest["notes"])
    return jsonify(out)


@app.route("/api/reference")
@login_required
def reference():
    db = get_db()
    return jsonify({
        "customers": rows_to_list(db.execute("SELECT customer_id, customer_name, contact_name, address FROM customers ORDER BY customer_name").fetchall()),
        "suppliers": rows_to_list(db.execute("SELECT supplier_id, supplier_name FROM suppliers ORDER BY supplier_name").fetchall()),
        # project_manager is included here (not just on the live /api/works-orders
        # list) because the Works Orders page's own Owner filter dropdown builds
        # its option list from this reference set, not from the page's current
        # (possibly already-filtered) rows - see renderWorksOrders() in app.js.
        "works_orders": rows_to_list(db.execute("SELECT works_order_id, job_number, job_name, customer_id, contract_code, location, project_manager FROM works_orders wo WHERE " + PLACEHOLDER_JOB_SQL + " ORDER BY job_number").fetchall()),
        # placeholder jobs the historic POs sit on: not offered in pick-lists, but the
        # Purchase Orders page still needs them to show the customer for those POs
        "placeholder_jobs": rows_to_list(db.execute("SELECT works_order_id, job_number, job_name, customer_id, contract_code, location, project_manager FROM works_orders wo WHERE wo.job_number LIKE 'HIST-PO-%' ORDER BY job_number").fetchall()),
        # Real people only - used to keep the PO "Raised by" filter free of initials,
        # combined entries and other non-names.
        "staff_names": sorted({n for n in __import__("tidy_raised_by").PEOPLE}
                              | {r[0] for r in db.execute("SELECT full_name FROM users WHERE full_name IS NOT NULL AND TRIM(full_name) != ''")}),
        "locations": [r[0] for r in db.execute(
            "SELECT DISTINCT location FROM works_orders WHERE location IS NOT NULL AND TRIM(location) != '' ORDER BY location")],
        "purchase_orders": rows_to_list(db.execute(
            "SELECT purchase_order_id, po_number, works_order_id, po_value FROM purchase_orders "
            "WHERE is_historic=0 ORDER BY po_number").fetchall()),
        "ship_to_addresses": rows_to_list(db.execute(
            "SELECT ship_to_id, label, company_name, address_line1, address_line2, city, postcode FROM ship_to_addresses ORDER BY label").fetchall()),
        "contract_code_targets": [] if is_service_engineer() else rows_to_list(db.execute(
            "SELECT contract_code, target_pct, standard_value, standard_cost FROM contract_code_targets ORDER BY contract_code").fetchall()),
    })


def run_startup_tasks():
    """Database setup that must happen before the app starts serving
    requests, regardless of which server runs it (Flask's own dev server
    for a single local user, or waitress for shared hosting) - migrating
    a legacy co-located database, taking a backup, and running the
    self-healing schema/data migrations. Kept in one place so the two
    launch paths can never drift out of sync with each other.
    Returns True if a usable database is ready, False if none exists yet
    and the caller should tell the person to run seed.py first."""
    db_location.migrate_legacy_files(BASE_DIR)
    if not os.path.exists(DB_PATH):
        print("No database found - run 'python seed.py' first.")
        return False
    db_location.backup_db()
    _migration_conn = sqlite3.connect(DB_PATH, timeout=10)
    _migration_conn.row_factory = sqlite3.Row
    _migration_conn.execute("PRAGMA foreign_keys = ON")
    try:
        migrations.run_migrations(_migration_conn, BASE_DIR)
    except Exception as e:
        # Belt and braces on top of run_migrations' own internal safety
        # net: migrations improve an existing database, they must never
        # be the reason the app fails to start at all.
        print(f"Startup migrations skipped due to an unexpected error: {e}. Continuing to start normally.")
    ok = check_and_stamp_database_version(_migration_conn)
    _migration_conn.close()
    if not ok:
        return False
    print(f"Using database at {DB_PATH}")
    print(f"TEAMworks360 version {app_version.APP_VERSION}")
    return True


def check_and_stamp_database_version(conn):
    """Protects the data from an old copy of the app. The database remembers the
    newest version that has opened it; if this (older) code is started against a
    database a newer version has already upgraded, it stops with a clear message
    instead of risking damage. Otherwise the current version is recorded, and any
    change of version is written to the audit log. Returns False to stop startup."""
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='app_version'").fetchone()
        stored = row[0] if row else None
        if stored and app_version.is_newer(stored, app_version.APP_VERSION) \
                and os.environ.get("TEAMWORKS360_ALLOW_OLDER") != "1":
            print("=" * 70)
            print(f"STOPPED: this database was last used by TEAMworks360 {stored},")
            print(f"but this copy of the app is older ({app_version.APP_VERSION}).")
            print("Running older code on a newer database could damage your data.")
            print("Install the newer version of the app and start it again.")
            print("(Your data has not been changed.)")
            print("=" * 70)
            return False
        if stored != app_version.APP_VERSION:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('app_version', ?)",
                         (app_version.APP_VERSION,))
            conn.execute(
                "INSERT INTO audit_log (table_name, record_id, field_name, old_value, new_value, changed_by) "
                "VALUES ('system', 0, 'app_version', ?, ?, 'startup')", (stored, app_version.APP_VERSION))
            conn.commit()
    except Exception as e:
        # Version bookkeeping must never be the reason the app can't start.
        print(f"Version check skipped: {e}")
    return True


if __name__ == "__main__":
    # serve_shared.py already stops here if there's no database; this path
    # used to ignore the answer and start serving anyway, so the first
    # login attempt hit a missing "users" table - a 500 the login screen
    # could only show as a bare "Request failed" - and quietly created an
    # empty jpm.db that later made the launchers think a real database
    # existed, so they never seeded one.
    if not run_startup_tasks():
        sys.exit(1)
    # Debug mode (Werkzeug's interactive debugger) allows arbitrary code
    # execution from the browser if an unhandled error is triggered - never
    # enable it outside your own local machine. Opt in explicitly for local
    # development only: FLASK_DEBUG=1 python app.py
    debug_mode = os.environ.get("FLASK_DEBUG") == "1"
    if debug_mode:
        print("WARNING: Running in debug mode - do not expose this to a network. Local dev only.")
    # Bind to localhost only by default so the app isn't reachable from
    # other devices on the network unless explicitly opted into.
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    port = 5000

    # Flask's own development server (which this __main__ block runs) says
    # in its own startup banner that it isn't meant to serve anyone beyond
    # one local user - that warning is easy to miss, so make it loud and
    # specific here too, the moment someone opts into exposing it. Using
    # serve_shared.py (waitress) is the real fix for multi-user access; see
    # its own startup message and the README's "Shared hosting" section.
    if host not in ("127.0.0.1", "localhost"):
        print("=" * 70)
        print(f"WARNING: binding to {host} - this is reachable from other devices")
        print("on the network. Flask's own development server (used here) is not")
        print("a production server and is not meant to serve more than one local")
        print("user - use serve_shared.py (waitress) instead for real multi-user")
        print("access.")
        if os.environ.get("FLASK_HTTPS") != "1":
            print()
            print("Also: traffic - including login passwords - will travel UNENCRYPTED")
            print("over the network from here. Put a reverse proxy doing HTTPS in")
            print("front of this (see README.md, \"Securing shared hosting with")
            print("HTTPS\") before exposing this beyond your own machine, and set")
            print("FLASK_HTTPS=1 once you do.")
        print("=" * 70)

    # Auto-open the browser so starting the app is a single double-click.
    # Guarded so it only fires once even when the debug reloader spawns a
    # second process (WERKZEUG_RUN_MAIN is only set in that child process).
    if os.environ.get("NO_BROWSER") != "1" and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        import threading
        import webbrowser

        def _open_browser():
            time.sleep(1.2)
            try:
                webbrowser.open(f"http://{host}:{port}")
            except Exception:
                pass  # headless environment or no browser configured - not fatal

        threading.Thread(target=_open_browser, daemon=True).start()

    app.run(debug=debug_mode, host=host, port=port)
