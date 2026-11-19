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

from io import BytesIO
from flask import (Flask, g, jsonify, request, session, send_from_directory, send_file,
                    Response, render_template, has_request_context)
from vercel.blob import sync as blob
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
import libsql
import globals

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
app.secret_key = os.environ.get("SECRET_KEY", "8f9d2b4e7a1c6f3e0b5d9a8c2e4f1b7d9a3c5e8f1b2d4a6c8e0f2b4d6a8c0e2f")
app.config.update(
    SESSION_COOKIE_SECURE=True,      # Required for HTTPS (Vercel uses HTTPS)
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

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
GOODS_IN_MAX_UPLOAD_BYTES = 4 * 1024 * 1024  # 4 MB combined cap per upload

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
        row = globals.query_db(db, "SELECT value FROM settings WHERE key='default_margin_target'", one=True)
        default_target = safe_float(row["value"], DEFAULT_MARGIN_TARGET) if row else DEFAULT_MARGIN_TARGET
        code_rows = globals.query_db(db, "SELECT contract_code, target_pct FROM contract_code_targets", one=False)
        code_targets = {r["contract_code"].strip().upper(): r["target_pct"] for r in code_rows}
        g._margin_settings = (default_target, code_targets)
    return g._margin_settings


def margin_below_target(margin_pct, target_pct):
    """True only if the margin is below target as it is DISPLAYED (1 decimal place). A job at 19.996% shows as 20.0%,
    so it must not be flagged against a 20% target; one at 19.94% shows as 19.9% and is."""
    return round(margin_pct + 1e-9, 1) < round(target_pct + 1e-9, 1)


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
                         "despatch_notes:write", "fixed_pos:write", "quality:write", "enquiries:write", "read"},
    "Procurement": {"purchase_orders:write", "inventory:write", "goods_in:write", "fixed_pos:write", "quality:write", "enquiries:write", "read"},
    "Finance": {"supplier_invoices:write", "customer_invoices:write", "goods_in:write", "fixed_pos:write", "read"},
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
    globals.query_db(db, 
        "INSERT INTO login_events (username, ip_address, event_type) VALUES (?,?,?)",
        (username or "(blank)", request.remote_addr, event_type), commit=True
    )


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
# change_password is exempt here too: a new account has to change its temporary password FIRST and
# set up 2FA second, so the password screen must work while 2FA setup is still outstanding (it still
# needs the current password, so being reachable at this point gives nobody anything extra).
_2FA_GATE_EXEMPT_ENDPOINTS = {"logout", "me", "get_2fa_setup", "get_2fa_setup_qr", "confirm_2fa_setup", "verify_2fa",
                              "change_password"}

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
    globals.query_db(db, "DELETE FROM totp_backup_codes WHERE user_id=?", (user_id,), commit=False)
    codes = []
    for _ in range(count):
        code = totp.generate_backup_code()
        codes.append(code)
        globals.query_db(db, 
            "INSERT INTO totp_backup_codes (user_id, code_hash) VALUES (?,?)",
            (user_id, generate_password_hash(code)), commit=False
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
    rows = globals.query_db(db, 
        "SELECT backup_code_id, code_hash FROM totp_backup_codes WHERE user_id=? AND used_date IS NULL",
        (user_id,), one=False
    )
    for row in rows:
        if check_password_hash(row["code_hash"], code):
            globals.query_db(db,
                "UPDATE totp_backup_codes SET used_date=CURRENT_TIMESTAMP WHERE backup_code_id=?",
                (row["backup_code_id"],), commit=True
            )
            return True
    return False


def log_document_access(db, document_type, reference):
    """Durable record of every PDF download and CSV export - who,
    which document, when - independent of audit_log (field-value edits)
    and login_events (sign-ins). Read via the admin-only
    /api/document-access-log endpoint. Never pruned automatically, same
    as those two."""
    globals.query_db(db, 
        "INSERT INTO document_access_log (username, document_type, reference, ip_address) VALUES (?,?,?,?)",
        (current_user_name(), document_type, reference, request.remote_addr), commit=True
    )


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
    # A PDF carries no page script, and a page-style policy on it breaks the browsers' built-in PDF viewers:
    # Firefox shows a blank tab for Preview (v1.13.1 fix), Chrome needs object-src. So PDFs get no policy
    # of their own (X-Content-Type-Options and X-Frame-Options above still apply).
    if response.mimetype == "application/pdf":
        response.headers.pop("Content-Security-Policy", None)
    else:
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
    rows = globals.query_db(db, "SELECT quote_number FROM quotes", one=False)
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
    rows = globals.query_db(db, "SELECT despatch_number FROM despatch_notes", one=False)
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
    for r in globals.query_db(db, f"SELECT {column} FROM {table}", one=False):
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


def _add_column_if_missing(conn, table_name, column_name, column_def):
    """Adds a column to an existing table only if it does not already exist."""
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()]
    if column_name not in cols:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")

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
          database = os.environ["TURSO_DATABASE_URL"],
          auth_token = os.environ["TURSO_AUTH_TOKEN"],
        )

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
            globals.query_db(db, 
                """INSERT INTO audit_log (table_name, record_id, field_name, old_value, new_value, changed_by)
                    VALUES (?,?,?,?,?,?)""",
                (table, record_id, field, old_val, new_val, changed_by), commit=False
            )


def get_audit_history(db, table, record_id):
    rows = globals.query_db(db, 
        "SELECT * FROM audit_log WHERE table_name=? AND record_id=? ORDER BY audit_id DESC",
        (table, record_id), one=False
    )
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
  
    row = globals.query_db(db, "SELECT role FROM users WHERE user_id=?", (session.get("user_id"),), one=True)
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

    rev_row = globals.query_db(db, 
        "SELECT COALESCE(SUM(invoice_amount),0) AS total FROM customer_invoices "
        "WHERE works_order_id=? AND status != 'Cancelled'", (wo_id,), one=True)
    revenue = rev_row['total'] if rev_row else 0

    sup_row = globals.query_db(db, 
        "SELECT COALESCE(SUM(invoice_amount),0) AS total FROM supplier_invoices "
        "WHERE works_order_id=? AND status != 'Cancelled'", (wo_id,), one=True)
    supplier_invoice_total = sup_row['total'] if sup_row else 0

    lab_row = globals.query_db(db, 
        "SELECT COALESCE(SUM(labour_cost),0) AS total FROM labour_entries WHERE works_order_id=?", (wo_id,), one=True)
    labour_total = lab_row['total'] if lab_row else 0

    po_row = globals.query_db(db, 
        "SELECT COALESCE(SUM(po_value),0) AS total FROM purchase_orders "
        "WHERE works_order_id=? AND status != 'Cancelled' AND is_historic=0", (wo_id,), one=True)
    po_total = po_row['total'] if po_row else 0

    sto_row = globals.query_db(db, 
        """SELECT COALESCE(SUM(sa.quantity * si.purchase_cost), 0) AS total
            FROM stock_allocations sa JOIN stock_items si ON si.stock_item_id = sa.stock_item_id
            WHERE sa.works_order_id=?""", (wo_id,), one=True)
    stock_cost = sto_row['total'] if sto_row else 0

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
    if revenue and margin_below_target(forecast_margin_pct, margin_target):
        alerts.append({"level": "amber", "type": "Margin Risk",
                        "message": f"Forecast margin {forecast_margin_pct:.1f}% is below the {margin_target:.0f}% target"})

    # Over-invoiced PO check (per PO within this job). Excludes a Cancelled
    # PO itself, same as po_total/supplier_invoice_total above - a
    # cancelled PO is no longer a live commitment, so it shouldn't trigger
    # a "£X exceeds PO value - approval required" alert just because a
    # non-cancelled invoice happens to still reference it.
    over_invoiced_pos = globals.query_db(db, 
        """SELECT po.po_number, po.po_value, COALESCE(SUM(si.invoice_amount),0) AS invoiced
           FROM purchase_orders po
           LEFT JOIN supplier_invoices si ON si.purchase_order_id = po.purchase_order_id AND si.status != 'Cancelled'
           WHERE po.works_order_id = ? AND po.status != 'Cancelled' AND po.is_historic = 0
           GROUP BY po.purchase_order_id
           HAVING invoiced > po.po_value""",
        (wo_id,), one=False
    )
    for po in over_invoiced_pos:
        alerts.append({
            "level": "red", "type": "Over-Invoiced PO",
            "message": f"{po['po_number']}: invoiced £{po['invoiced']:,.2f} exceeds PO value £{po['po_value']:,.2f} - approval required",
        })

    if profit < 0:
        status_light = "red"
    elif margin_below_target(margin_pct, margin_target):
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
    rows = globals.query_db(db,
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
        (yr, yr, yr), one=False
    )

    result = []
    for r in rows:
        revenue = r["revenue"]
        actual_cost = r["actual_cost"]
        profit = revenue - actual_cost
        margin_pct = (profit / revenue * 100) if revenue else 0.0
        margin_target = get_margin_target(db, r["contract_code"])
        if profit < 0:
            status_light = "red"
        elif margin_below_target(margin_pct, margin_target):
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

    user = globals.query_db(db, "SELECT * FROM users WHERE username=?", (username,), one=True)  

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
    user = globals.query_db(db, "SELECT * FROM users WHERE user_id=?", (session["user_id"],), one=True)
    if not user or not check_password_hash(user["password_hash"], current_password):
        return jsonify({"error": "Current password is incorrect."}), 400
    globals.query_db(db,
        "UPDATE users SET password_hash=?, must_change_password=0 WHERE user_id=?",
        (generate_password_hash(new_password), session["user_id"]), commit=True
    )
    session["must_change_password"] = False

    # Check if this user still needs 2FA set up (e.g. totp_enabled is 0 / False)
    user = globals.query_db(db, "SELECT totp_enabled FROM users WHERE user_id=?", (session["user_id"],), one=True)
    needs_2fa_setup = not bool(user and user.get("totp_enabled"))
    session["must_setup_2fa"] = needs_2fa_setup

    return jsonify({
        "ok": True,
        "must_setup_2fa": needs_2fa_setup,
    })


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
        globals.query_db(db, "UPDATE users SET totp_secret=? WHERE user_id=?", (secret, user["user_id"]), commit=True)
    return secret


@app.route("/api/auth/2fa/setup", methods=["GET"])
@login_required
def get_2fa_setup():
    """Returns the manual setup key (and otpauth:// link) for whichever
    secret is currently pending for this user."""
    db = get_db()
    user = globals.query_db(db, "SELECT * FROM users WHERE user_id=?", (session["user_id"],), one=True)
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
    user = globals.query_db(db, "SELECT * FROM users WHERE user_id=?", (session["user_id"],), one=True)
    if user["totp_enabled"]:
        return Response("Two-factor authentication is already set up for this account.", status=400)
    secret = _pending_totp_secret(db, user)
    uri = totp.provisioning_uri(secret, user["username"])
    try:
        png_bytes = qr_png.render_png(uri)
    except Exception as e:
        # Seen on a machine whose Python packages differ from the pinned ones: say what failed, in the
        # window the app was started from, instead of just showing a broken picture on the sign-in page.
        print(f"Two-factor QR code could not be drawn: {type(e).__name__}: {e}. "
              "The setup key on the same screen still works. Run: pip install -r requirements.txt")
        return Response("The QR code could not be drawn on this computer. Use the setup key instead.",
                         status=500, mimetype="text/plain")
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
    user = globals.query_db(db, "SELECT * FROM users WHERE user_id=?", (session["user_id"],), one=True)
    if user["totp_enabled"]:
        return jsonify({"error": "Two-factor authentication is already set up for this account."}), 400
    if not user["totp_secret"]:
        return jsonify({"error": "Start setup first to get a setup key."}), 400
    if not totp.verify(user["totp_secret"], code):
        return jsonify({"error": "That code didn't match. Check the time on your phone and try again."}), 400
    globals.query_db(db, "UPDATE users SET totp_enabled=1 WHERE user_id=?",(session["user_id"],), commit=True)
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
    user = globals.query_db(db, "SELECT * FROM users WHERE user_id=?", (session["user_id"],), one=True)  
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
    "customer_invoices", "supplier_invoices", "labour_entries", "fixed_customer_pos",
    "non_conformances", "suppliers", "enquiries",
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
    rows = globals.query_db(db,
        "SELECT * FROM login_events ORDER BY event_id DESC LIMIT 200", one=False
    )
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
    rows = globals.query_db(db, "SELECT * FROM document_access_log ORDER BY access_id DESC LIMIT 200", one=False)
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
    rows = globals.query_db(db,
        """SELECT q.*, c.customer_name, wo.job_number AS linked_job_number FROM quotes q
           LEFT JOIN customers c ON c.customer_id = q.customer_id
           LEFT JOIN works_orders wo ON wo.works_order_id = q.works_order_id
           ORDER BY q.quote_id DESC""", one=False
    )
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
        quote_id = globals.query_db(db, 
            """INSERT INTO quotes
               (quote_number, customer_id, description, owner, status, estimated_value, quote_date, valid_until, notes, sharepoint_url,
                contact_name, your_ref, works_order_ref, works_order_id, scope_items, delivery_terms, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (quote_number, data.get("customer_id") or None, data.get("description"), data.get("owner"),
             data.get("status", "Draft"), safe_float(data.get("estimated_value")),
             data.get("quote_date"), valid_until, data.get("notes"), data.get("sharepoint_url"),
             data.get("contact_name"), data.get("your_ref"), data.get("works_order_ref"), data.get("works_order_id") or None,
             data.get("scope_items"), data.get("delivery_terms"), current_user_name()), commit=False
        )

        # Optional cost buildup from the New Quote wizard - a plain quote
        # created without these fields behaves exactly as before.
        for line in data.get("labour_lines") or []:
            std_hours = safe_float(line.get("standard_hours"))
            ot_hours = safe_float(line.get("ot_hours"))
            if std_hours <= 0 and ot_hours <= 0:
                continue  # skip rate-card rows the user left blank
            globals.query_db(db, 
                """INSERT INTO quote_labour_lines
                   (quote_id, category, activity, standard_rate, ot_rate, standard_hours, ot_hours)
                   VALUES (?,?,?,?,?,?,?)""",
                (quote_id, line.get("category"), line.get("activity"),
                 safe_float(line.get("standard_rate")), safe_float(line.get("ot_rate")), std_hours, ot_hours), commit=False
            )
        for line in data.get("purchase_lines") or []:
            if not (line.get("description") or safe_float(line.get("purchase_amount")) > 0):
                continue  # skip fully-blank rows
            globals.query_db(db, 
                """INSERT INTO quote_purchase_lines
                   (quote_id, invoice_no, line_date, supplier, description, quantity, purchase_amount, sales_value)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (quote_id, line.get("invoice_no"), line.get("line_date"), line.get("supplier"),
                 line.get("description"), safe_float(line.get("quantity")),
                 safe_float(line.get("purchase_amount")), safe_float(line.get("sales_value"))), commit=False
            )
        db.commit()
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"quote_id": quote_id, "quote_number": quote_number}), 201


@app.route("/api/quotes/<int:qid>/cost-breakdown")
@login_required
def get_quote_cost_breakdown(qid):
    db = get_db()
    labour = globals.query_db(db,
        "SELECT * FROM quote_labour_lines WHERE quote_id=? ORDER BY line_id", (qid,), one=False
    )
    purchases = globals.query_db(db,
        "SELECT * FROM quote_purchase_lines WHERE quote_id=? ORDER BY line_id", (qid,), one=False
    )
    return jsonify({
        "labour_lines": rows_to_list(labour),
        "purchase_lines": rows_to_list(purchases),
    })

def pdf_response(pdf_bytes, filename):
    """Send a PDF. ?preview=1 opens it in the browser tab (inline); otherwise it downloads."""
    preview = request.args.get("preview") == "1"
    return Response(pdf_bytes, mimetype="application/pdf",
                    headers={"Content-Disposition": f"{'inline' if preview else 'attachment'}; filename={filename}.pdf"})


@app.route("/api/quotes/<int:qid>/pdf")
@login_required
def get_quote_pdf(qid):
    db = get_db()
    quote = globals.query_db(db, 
        """SELECT q.*, c.customer_name, c.address AS customer_address, c.contact_name AS customer_contact_name
           FROM quotes q LEFT JOIN customers c ON c.customer_id = q.customer_id
           WHERE q.quote_id=?""",
        (qid,), one=True
    )
    if not quote:
        return jsonify({"error": "Quote not found"}), 404
    pdf_bytes = build_quote_pdf(quote)
    log_document_access(db, "Quote PDF" + (" (preview)" if request.args.get("preview") == "1" else ""), quote["quote_number"])
    return pdf_response(pdf_bytes, quote["quote_number"])


@app.route("/api/rate-card")
@login_required
def get_rate_card():
    db = get_db()
    rows = globals.query_db(db, "SELECT * FROM rate_card ORDER BY sort_order", one=False)
    return jsonify(rows_to_list(rows))


@app.route("/api/quotes/<int:qid>", methods=["PUT"])
@permission_required("quotes:write")
def update_quote(qid):
    data = request.get_json(force=True) or {}
    db = get_db()
    old = globals.query_db(db, "SELECT * FROM quotes WHERE quote_id=?", (qid,), one=True)
    valid_until = data.get("valid_until") or default_valid_until(data.get("quote_date"))
    log_field_changes(db, "quotes", qid, old,
                       {"status": data.get("status"), "estimated_value": safe_float(data.get("estimated_value"))},
                       ["status", "estimated_value"], current_user_name())
    # quote_number is intentionally not accepted here - it's fixed once
    # assigned, the same way a SharePoint folder name wouldn't be renamed
    # just because the record was edited.
    globals.query_db(db,
        """UPDATE quotes SET customer_id=?, description=?, owner=?, status=?,
           estimated_value=?, quote_date=?, valid_until=?, notes=?, sharepoint_url=?,
           contact_name=?, your_ref=?, works_order_ref=?, works_order_id=?, scope_items=?, delivery_terms=?,
           updated_by=?, updated_date=datetime('now') WHERE quote_id=?""",
        (data.get("customer_id") or None, data.get("description"), data.get("owner"),
         data.get("status"), safe_float(data.get("estimated_value")),
         data.get("quote_date"), valid_until, data.get("notes"), data.get("sharepoint_url"),
         data.get("contact_name"), data.get("your_ref"), data.get("works_order_ref"), data.get("works_order_id") or None,
         data.get("scope_items"), data.get("delivery_terms"), current_user_name(), qid), commit=True
    )
    return jsonify({"ok": True})


@app.route("/api/quotes/<int:qid>", methods=["DELETE"])
@permission_required("all")
def delete_quote(qid):
    db = get_db()
    globals.query_db(db, "DELETE FROM quotes WHERE quote_id=?", (qid,), commit=True)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Customers
# --------------------------------------------------------------------------
@app.route("/api/customers", methods=["GET"])
@login_required
def list_customers():
    db = get_db()
    return jsonify(rows_to_list(globals.query_db(db, "SELECT * FROM customers ORDER BY customer_name", one=False)))


def clean_terms_days(value):
    """Payment terms in days, or None when not set / not a sensible number."""
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if 0 <= n <= 365 else None


def customer_terms(name, days, eow):
    """(days, end_of_week) that apply to a customer: its own terms if set, otherwise 75 days end of week for any
    Rolls Royce site (the agreed terms for all of them), otherwise (None, False) = not set."""
    if days is not None:
        return days, bool(eow)
    if (name or "").lower().startswith("rolls royce"):
        return 75, True
    return None, False


def compute_due_date(invoice_date, days, eow):
    """Invoice date + `days`; with `eow`, then on to the Friday on or after that day. None if it can't be worked out."""
    if days is None:
        return None
    try:
        d = datetime.strptime((invoice_date or "")[:10], "%Y-%m-%d").date() + timedelta(days=days)
    except ValueError:
        return None
    if eow:
        d += timedelta(days=(4 - d.weekday()) % 7)
    return d


def terms_text(days, eow):
    if days is None:
        return "Not set"
    return f"{days} days" + (", end of week" if eow else "")


@app.route("/api/customers", methods=["POST"])
@permission_required("all")
def create_customer():
    data = request.get_json(force=True) or {}
    db = get_db()
    cust_id = globals.query_db(db, 
        "INSERT INTO customers (customer_name, address, contact_name, email, phone, payment_terms_days, payment_terms_eow, created_by, created_date) VALUES (?,?,?,?,?,?,?,?,datetime('now'))",
        (data.get("customer_name"), data.get("address"), data.get("contact_name"), data.get("email"), data.get("phone"),
         clean_terms_days(data.get("payment_terms_days")), 1 if data.get("payment_terms_eow") else 0, current_user_name()), commit=True
    )
    return jsonify({"customer_id": cust_id}), 201


@app.route("/api/customers/<int:cid>", methods=["PUT"])
@permission_required("all")
def update_customer(cid):
    data = request.get_json(force=True) or {}
    db = get_db()
    globals.query_db(db,
        "UPDATE customers SET customer_name=?, address=?, contact_name=?, email=?, phone=?, payment_terms_days=?, payment_terms_eow=?, updated_by=?, updated_date=datetime('now') WHERE customer_id=?",
        (data.get("customer_name"), data.get("address"), data.get("contact_name"), data.get("email"), data.get("phone"),
         clean_terms_days(data.get("payment_terms_days")), 1 if data.get("payment_terms_eow") else 0, current_user_name(), cid), commit=True
    )
    return jsonify({"ok": True})


@app.route("/api/customers/<int:cid>", methods=["DELETE"])
@permission_required("all")
def delete_customer(cid):
    db = get_db()
    try:
        globals.query_db(db, "DELETE FROM customers WHERE customer_id=?", (cid,), commit=True)
    except Exception:
        return jsonify({"error": "This customer has Works Orders linked to it and cannot be deleted."}), 400
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Suppliers
# --------------------------------------------------------------------------
@app.route("/api/suppliers", methods=["GET"])
@login_required
def list_suppliers():
    db = get_db()
    return jsonify(rows_to_list(globals.query_db(db, "SELECT * FROM suppliers ORDER BY supplier_name", one=False)))


@app.route("/api/suppliers", methods=["POST"])
@permission_required("all")
def create_supplier():
    data = request.get_json(force=True) or {}
    db = get_db()
    supp_id = globals.query_db(db, 
        """INSERT INTO suppliers (supplier_name, supply_of, reason_for_use, authorised_by, address, contact_name, email, vat_number, created_by, created_date)
           VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))""",
        (data.get("supplier_name"), data.get("supply_of"), data.get("reason_for_use"), data.get("authorised_by"),
         data.get("address"), data.get("contact_name"), data.get("email"), data.get("vat_number"), current_user_name()), commit=True
    )
    return jsonify({"supplier_id": supp_id}), 201


@app.route("/api/suppliers/<int:sid>", methods=["PUT"])
@permission_required("all")
def update_supplier(sid):
    data = request.get_json(force=True) or {}
    db = get_db()
    globals.query_db(db,
        """UPDATE suppliers SET supplier_name=?, supply_of=?, reason_for_use=?, authorised_by=?,
           address=?, contact_name=?, email=?, vat_number=?, updated_by=?, updated_date=datetime('now') WHERE supplier_id=?""",
        (data.get("supplier_name"), data.get("supply_of"), data.get("reason_for_use"), data.get("authorised_by"),
         data.get("address"), data.get("contact_name"), data.get("email"), data.get("vat_number"), current_user_name(), sid), commit=True
    )
    return jsonify({"ok": True})


@app.route("/api/suppliers/<int:sid>", methods=["DELETE"])
@permission_required("all")
def delete_supplier(sid):
    db = get_db()
    try:
        globals.query_db(db, "DELETE FROM suppliers WHERE supplier_id=?", (sid,), commit=True)
    except Exception:
        return jsonify({"error": "This supplier has Purchase Orders linked to it and cannot be deleted."}), 400
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Quality (v1.11.0): supplier approval, non-conformances, goods-in traceability, contract review
# --------------------------------------------------------------------------
SUPPLIER_APPROVAL_STATUSES = ("Not assessed", "Approved", "Conditional", "Suspended")
NCR_SOURCES = ("Supplier delivery", "Customer complaint", "Internal", "Late delivery", "Other")
NCR_STATUSES = ("Raised", "Contained", "Root cause", "Action", "Effectiveness check", "Closed")
NCR_DISPOSITIONS = ("Use as is", "Rework", "Scrap", "Return to supplier", "Concession")
GOODS_IN_CERT_STATUSES = ("Received", "Not received", "Not required")
GOODS_IN_INSPECTION_RESULTS = ("Accepted", "Rejected")
CERT_DUE_SOON_DAYS = 60
CONTRACT_REVIEW_CHECKS = (
    ("price", "Price agrees with the quote"),
    ("quantity", "Quantity agrees with the quote"),
    ("delivery", "We can meet the delivery date"),
    ("drawing", "Drawing and revision are current and we hold them"),
    ("special", "Special requirements and flow-downs reviewed"),
    ("capacity", "Capability and capacity confirmed"),
    ("lead_times", "Supplier lead times fit"),
    ("customer_property", "Customer property or free-issue material involved"),
)
CONTRACT_REVIEW_RESULTS = ("Yes", "No", "N/A")
CONTRACT_REVIEW_OUTCOMES = ("Accepted as ordered", "Query raised with customer")


def _parse_day(value):
    try:
        return datetime.strptime((value or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def cert_state(expiry_date, today=None):
    """'expired', 'due' (within 60 days) or 'ok' for one certificate's expiry date; 'ok' when no date is held."""
    exp = _parse_day(expiry_date)
    if exp is None:
        return "ok"
    today = today or date.today()
    if exp < today:
        return "expired"
    return "due" if (exp - today).days <= CERT_DUE_SOON_DAYS else "ok"


def supplier_quality_rows(db):
    """Every supplier with its approval fields plus certificate state and delivery/NCR performance worked out from
    existing records. Score = on-time delivery % (100 when nothing is recorded yet) less 8 points per NCR raised in the
    last 12 months, never below 0; blank when there is nothing to base it on."""
    today = date.today()
    perf = {p["supplier_name"]: p for p in build_overdue_deliveries(db, {})["performance"]}

    ncr_rows = globals.query_db(db,
        "SELECT supplier_id, COUNT(*) AS cnt FROM non_conformances WHERE supplier_id IS NOT NULL AND raised_date >= date('now','-12 months') GROUP BY supplier_id",
        one=False)
    ncrs = {r["supplier_id"]: r["cnt"] for r in ncr_rows}

    certs = {}
    cert_exp_rows = globals.query_db(db,
        "SELECT supplier_id, expiry_date FROM supplier_certificates WHERE COALESCE(expiry_date,'') != ''",
        one=False)
    for r in cert_exp_rows:
        certs.setdefault(r["supplier_id"], []).append(r["expiry_date"][:10])

    cert_count_rows = globals.query_db(db,
        "SELECT supplier_id, COUNT(*) AS cnt FROM supplier_certificates GROUP BY supplier_id",
        one=False)
    cert_count = {r["supplier_id"]: r["cnt"] for r in cert_count_rows}

    suppliers = globals.query_db(db, "SELECT * FROM suppliers ORDER BY supplier_name", one=False)
    out = []
    for s in suppliers:
        row = dict(s)
        p = perf.get(s["supplier_name"])
        n = ncrs.get(s["supplier_id"], 0)
        dates = sorted(certs.get(s["supplier_id"], []))
        row["certificate_count"] = cert_count.get(s["supplier_id"], 0)
        row["next_cert_expiry"] = dates[0] if dates else None
        states = [cert_state(d, today) for d in dates]
        row["cert_state"] = "expired" if "expired" in states else ("due" if "due" in states else "ok")
        row["deliveries"] = p["received"] if p else 0
        row["on_time_pct"] = p["on_time_pct"] if p else None
        row["ncrs_12m"] = n
        row["score"] = max(0, (p["on_time_pct"] if p else 100) - 8 * n) if (p or n) else None
        nr = _parse_day(s["next_review_date"])
        row["review_overdue"] = bool(nr and nr < today)
        out.append(row)
    return out


@app.route("/api/suppliers/quality", methods=["GET"])
@login_required
def list_suppliers_quality():
    return jsonify(supplier_quality_rows(get_db()))


@app.route("/api/suppliers/<int:sid>/quality", methods=["GET"])
@login_required
def get_supplier_quality(sid):
    db = get_db()
    row = next((r for r in supplier_quality_rows(db) if r["supplier_id"] == sid), None)
    if not row:
        return jsonify({"error": "Supplier not found"}), 404
    certs = rows_to_list(globals.query_db(db, "SELECT * FROM supplier_certificates WHERE supplier_id=? ORDER BY expiry_date IS NULL, expiry_date", (sid,), one=False))
    for c in certs:
        c["state"] = cert_state(c["expiry_date"])
        exp = _parse_day(c["expiry_date"])
        c["days_left"] = (exp - date.today()).days if exp else None
    evals = rows_to_list(globals.query_db(db, "SELECT * FROM supplier_evaluations WHERE supplier_id=? ORDER BY eval_date DESC, evaluation_id DESC", (sid,), one=False))
    return jsonify({"supplier": row, "certificates": certs, "evaluations": evals})


@app.route("/api/suppliers/<int:sid>/approval", methods=["POST"])
@permission_required("quality:write")
def save_supplier_approval(sid):
    data = request.get_json(force=True) or {}
    db = get_db()
    old = globals.query_db(db, "SELECT * FROM suppliers WHERE supplier_id=?", (sid,), one=True)
    if not old:
        return jsonify({"error": "Supplier not found"}), 404
    status = data.get("approval_status")
    if status not in SUPPLIER_APPROVAL_STATUSES:
        return jsonify({"error": "Choose an approval status."}), 400
    new = {
        "approval_status": status,
        "approved_for": (data.get("approved_for") or "").strip() or None,
        "approval_condition": (data.get("approval_condition") or "").strip() or None,
        "next_review_date": (data.get("next_review_date") or "").strip() or None,
    }
    who, today = current_user_name(), date.today().isoformat()
    log_field_changes(db, "suppliers", sid, old, new, list(new), who)
    globals.query_db(db, """UPDATE suppliers SET approval_status=?, approved_for=?, approval_condition=?, next_review_date=?,
                  approved_by=?, approved_date=?, updated_by=?, updated_date=datetime('now') WHERE supplier_id=?""",
               (status, new["approved_for"], new["approval_condition"], new["next_review_date"], who, today, who, sid), commit=False)
    globals.query_db(db, "INSERT INTO supplier_evaluations (supplier_id, eval_date, outcome, notes, evaluated_by) VALUES (?,?,?,?,?)",
               (sid, today, status, (data.get("notes") or "").strip() or None, who), commit=False)
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/suppliers/<int:sid>/certificates", methods=["POST"])
@permission_required("quality:write")
def add_supplier_certificate(sid):
    data = request.get_json(force=True) or {}
    db = get_db()
    if not globals.query_db(db, "SELECT 1 FROM suppliers WHERE supplier_id=?", (sid,), one=True):
        return jsonify({"error": "Supplier not found"}), 404
    if not (data.get("cert_type") or "").strip():
        return jsonify({"error": "Enter the certificate type, for example ISO 9001."}), 400
    expiry = (data.get("expiry_date") or "").strip() or None
    if expiry and not _parse_day(expiry):
        return jsonify({"error": "The expiry date isn't a valid date."}), 400
    certid = globals.query_db(db, "INSERT INTO supplier_certificates (supplier_id, cert_type, cert_number, expiry_date, created_by) VALUES (?,?,?,?,?)",
                     (sid, data["cert_type"].strip(), (data.get("cert_number") or "").strip() or None, expiry, current_user_name()), commit=True)
    return jsonify({"certificate_id": certid}), 201


@app.route("/api/supplier-certificates/<int:cid>", methods=["PUT"])
@permission_required("quality:write")
def update_supplier_certificate(cid):
    data = request.get_json(force=True) or {}
    db = get_db()
    if not globals.query_db(db, "SELECT 1 FROM supplier_certificates WHERE certificate_id=?", (cid,), one=True):
        return jsonify({"error": "Certificate not found"}), 404
    if not (data.get("cert_type") or "").strip():
        return jsonify({"error": "Enter the certificate type, for example ISO 9001."}), 400
    expiry = (data.get("expiry_date") or "").strip() or None
    if expiry and not _parse_day(expiry):
        return jsonify({"error": "The expiry date isn't a valid date."}), 400
    globals.query_db(db, "UPDATE supplier_certificates SET cert_type=?, cert_number=?, expiry_date=? WHERE certificate_id=?",
               (data["cert_type"].strip(), (data.get("cert_number") or "").strip() or None, expiry, cid), commit=True)
    return jsonify({"ok": True})


@app.route("/api/supplier-certificates/<int:cid>", methods=["DELETE"])
@permission_required("quality:write")
def delete_supplier_certificate(cid):
    db = get_db()
    globals.query_db(db, "DELETE FROM supplier_certificates WHERE certificate_id=?", (cid,), commit=True)
    return jsonify({"ok": True})


def check_supplier_allowed(db, supplier_id, data, old_supplier_id=None):
    """A Suspended supplier can't be ordered from, except by an Administrator who gives a reason (recorded).
    Returns (error_response_or_None, override_reason_or_None)."""
    if supplier_id is None or (old_supplier_id is not None and str(old_supplier_id) == str(supplier_id)):
        return None, None
    sup = globals.query_db(db, "SELECT supplier_name, approval_status FROM suppliers WHERE supplier_id=?", (supplier_id,), one=True)
    if not sup or sup["approval_status"] != "Suspended":
        return None, None
    if session.get("role") != "Administrator":
        return (jsonify({"error": f"{sup['supplier_name']} is suspended as a supplier. Ask an Administrator to place this order."}), 400), None
    reason = (data.get("suspended_override_reason") or "").strip()
    if not reason:
        return (jsonify({"error": f"{sup['supplier_name']} is suspended. Enter the reason for ordering anyway.", "needs_override_reason": True}), 400), None
    return None, reason


# ---- Non-conformances -------------------------------------------------------
def get_next_ncr_number(db):
    max_n = 0
    rows = globals.query_db(db, "SELECT ncr_number FROM non_conformances", one=False)
    for r in rows:
        m = re.search(r"(\d+)\s*$", r["ncr_number"] or "")
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"NCR-{max_n + 1:04d}"



NCR_EDIT_FIELDS = ("raised_date", "source", "works_order_id", "supplier_id", "goods_in_id", "purchase_order_id",
                   "part_description", "quantity_affected", "description", "containment", "disposition",
                   "cost_of_poor_quality", "owner", "action_due", "root_cause", "corrective_action",
                   "effectiveness_check_date", "effectiveness_result", "status")


def _ncr_values(data, db, existing=None):
    """Cleaned NCR field values from a request body; returns (values, error_message)."""
    v = {}
    for f in NCR_EDIT_FIELDS:
        raw = data.get(f, existing[f] if existing is not None and f in existing.keys() else None)
        if isinstance(raw, str):
            raw = raw.strip() or None
        v[f] = raw
    for f in ("works_order_id", "supplier_id", "goods_in_id", "purchase_order_id"):
        v[f] = safe_int(v[f])
    v["cost_of_poor_quality"] = safe_float(v["cost_of_poor_quality"], None) if v["cost_of_poor_quality"] not in (None, "") else None
    gi = globals.query_db(db, "SELECT gi.purchase_order_id, po.supplier_id, po.works_order_id FROM goods_in gi "
                    "JOIN purchase_orders po ON po.purchase_order_id = gi.purchase_order_id WHERE gi.goods_in_id=?",
                    (v["goods_in_id"],), one=True) if v["goods_in_id"] else None
    if gi:
        v["purchase_order_id"] = v["purchase_order_id"] or gi["purchase_order_id"]
        v["supplier_id"] = v["supplier_id"] or gi["supplier_id"]
        v["works_order_id"] = v["works_order_id"] or gi["works_order_id"]
    v["raised_date"] = v["raised_date"] or date.today().isoformat()
    v["status"] = v["status"] or "Raised"
    if v["source"] not in NCR_SOURCES:
        return v, "Choose where the problem came from (source)."
    if not v["description"]:
        return v, "Describe what was wrong."
    if v["status"] not in NCR_STATUSES:
        return v, "That status isn't valid."
    if v["disposition"] and v["disposition"] not in NCR_DISPOSITIONS:
        return v, "That disposition isn't valid."
    if v["status"] == "Closed":
        missing = [label for key, label in (("root_cause", "root cause"), ("corrective_action", "corrective action"),
                                            ("effectiveness_result", "effectiveness check result")) if not v[key]]
        if missing:
            return v, "Before closing, fill in the " + ", ".join(missing) + "."
    for f in ("works_order_id", "supplier_id"):
        table, col = ("works_orders", "works_order_id") if f == "works_order_id" else ("suppliers", "supplier_id")
        if v[f] and not globals.query_db(db, f"SELECT 1 FROM {table} WHERE {col}=?", (v[f],), one=True):
            return v, "That job or supplier doesn't exist."
    return v, None


@app.route("/api/non-conformances", methods=["GET"])
@login_required
def list_non_conformances():
    db = get_db()
    rows = rows_to_list(globals.query_db(db, 
        """SELECT n.*, wo.job_number, s.supplier_name, po.po_number, c.customer_name
           FROM non_conformances n
           LEFT JOIN works_orders wo ON wo.works_order_id = n.works_order_id
           LEFT JOIN customers c ON c.customer_id = wo.customer_id
           LEFT JOIN suppliers s ON s.supplier_id = n.supplier_id
           LEFT JOIN purchase_orders po ON po.purchase_order_id = n.purchase_order_id
           ORDER BY n.ncr_id DESC""", one=False))
    today = date.today().isoformat()
    for r in rows:
        r["overdue"] = bool(r["status"] != "Closed" and r["action_due"] and r["action_due"][:10] < today)
    return jsonify(rows)


@app.route("/api/non-conformances", methods=["POST"])
@permission_required("quality:write")
def create_non_conformance():
    data = request.get_json(force=True) or {}
    db = get_db()
    v, err = _ncr_values(data, db)
    if err:
        return jsonify({"error": err}), 400
    who = current_user_name()
    for attempt in range(5):
        number = get_next_ncr_number(db)
        try:
            cols = ", ".join(NCR_EDIT_FIELDS)
            ncrid = globals.query_db(db, 
                f"INSERT INTO non_conformances (ncr_number, {cols}, closed_date, created_by) VALUES (?,{','.join('?' * len(NCR_EDIT_FIELDS))},?,?)",
                (number, *[v[f] for f in NCR_EDIT_FIELDS], date.today().isoformat() if v["status"] == "Closed" else None, who), commit=True)
            return jsonify({"ncr_id": ncrid, "ncr_number": number}), 201
        except Exception as e:
            db.rollback()
            if "ncr_number" in str(e) and attempt < 4:
                continue
            return jsonify({"error": str(e)}), 400


@app.route("/api/non-conformances/<int:nid>", methods=["PUT"])
@permission_required("quality:write")
def update_non_conformance(nid):
    data = request.get_json(force=True) or {}
    db = get_db()
    old = globals.query_db(db, "SELECT * FROM non_conformances WHERE ncr_id=?", (nid,), one=True)
    if not old:
        return jsonify({"error": "Not found"}), 404
    v, err = _ncr_values(data, db, existing=old)
    if err:
        return jsonify({"error": err}), 400
    who = current_user_name()
    log_field_changes(db, "non_conformances", nid, old, v, list(NCR_EDIT_FIELDS), who)
    closed = old["closed_date"] if v["status"] == "Closed" and old["closed_date"] else (date.today().isoformat() if v["status"] == "Closed" else None)
    sets = ", ".join(f"{f}=?" for f in NCR_EDIT_FIELDS)
    globals.query_db(db, f"UPDATE non_conformances SET {sets}, closed_date=?, updated_by=?, updated_date=datetime('now') WHERE ncr_id=?",
               (*[v[f] for f in NCR_EDIT_FIELDS], closed, who, nid), commit=True)
    return jsonify({"ok": True})


@app.route("/api/non-conformances/<int:nid>", methods=["DELETE"])
@permission_required("all")
def delete_non_conformance(nid):
    db = get_db()
    globals.query_db(db, "DELETE FROM non_conformances WHERE ncr_id=?", (nid,), commit=True)
    return jsonify({"ok": True})


# ---- Batch / certificate trace -----------------------------------------------
@app.route("/api/trace", methods=["GET"])
@login_required
def trace_batch():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"error": "Enter at least two characters of a batch, serial or heat number."}), 400
    db = get_db()
    like = f"%{q.lower()}%"
    rows = globals.query_db(db,
        """SELECT gi.*, po.po_number, po.works_order_id, s.supplier_name, s.approval_status,
                  wo.job_number, wo.job_name, c.customer_name,
                  (SELECT COUNT(*) FROM goods_in_documents d WHERE d.goods_in_id = gi.goods_in_id) AS document_count
           FROM goods_in gi
           JOIN purchase_orders po ON po.purchase_order_id = gi.purchase_order_id
           JOIN suppliers s ON s.supplier_id = po.supplier_id
           LEFT JOIN works_orders wo ON wo.works_order_id = po.works_order_id
           LEFT JOIN customers c ON c.customer_id = wo.customer_id
           WHERE lower(COALESCE(gi.batch_number,'')) LIKE ? OR lower(COALESCE(gi.heat_number,'')) LIKE ?
           ORDER BY gi.received_date DESC""",
        (like, like), one=False)
    if not rows:
        return jsonify([])

    wo_ids = sorted({r["works_order_id"] for r in rows if r.get("works_order_id")})
    despatches_by_wo = {}
    if wo_ids:
        placeholders = ",".join("?" for _ in wo_ids)
        desp_rows = globals.query_db(db,
            f"SELECT works_order_id, despatch_number, despatch_date, status FROM despatch_notes "
            f"WHERE works_order_id IN ({placeholders}) AND status != 'Cancelled' ORDER BY despatch_date",
            tuple(wo_ids), one=False)
        for d in desp_rows:
            despatches_by_wo.setdefault(d["works_order_id"], []).append({
                "despatch_number": d["despatch_number"],
                "despatch_date": d["despatch_date"],
                "status": d["status"],
            })

    gi_ids = sorted({r["goods_in_id"] for r in rows})
    gi_placeholders = ",".join("?" for _ in gi_ids)
    ncr_rows = globals.query_db(db,
        f"SELECT goods_in_id, ncr_id, ncr_number, status FROM non_conformances "
        f"WHERE goods_in_id IN ({gi_placeholders})",
        tuple(gi_ids), one=False)
    ncrs_by_gi = {}
    for n in ncr_rows:
        ncrs_by_gi.setdefault(n["goods_in_id"], []).append({
            "ncr_id": n["ncr_id"],
            "ncr_number": n["ncr_number"],
            "status": n["status"],
        })

    out = []
    for r in rows:
        item = dict(r)
        item["despatches"] = despatches_by_wo.get(r["works_order_id"], []) if r.get("works_order_id") else []
        item["ncrs"] = ncrs_by_gi.get(r["goods_in_id"], [])
        out.append(item)
    return jsonify(out)


# ---- Contract review -----------------------------------------------------------
def _review_status_for(signed_off, outcome):
    if signed_off:
        return "Signed off"
    return "Query open" if outcome == "Query raised with customer" else "Not reviewed"


@app.route("/api/works-orders/<int:wid>/contract-review", methods=["GET"])
@login_required
def get_contract_review(wid):
    if is_service_engineer():
        return jsonify({"error": "Your role (Service Engineer) can't open contract reviews."}), 403
    db = get_db()
    wo = globals.query_db(db, """SELECT wo.*, c.customer_name FROM works_orders wo JOIN customers c ON c.customer_id = wo.customer_id
                       WHERE wo.works_order_id=?""", (wid,), one=True)
    if not wo:
        return jsonify({"error": "Job not found"}), 404
    quote = globals.query_db(db, "SELECT * FROM quotes WHERE works_order_id=? ORDER BY quote_id DESC LIMIT 1", (wid,), one=True)
    rv = globals.query_db(db, "SELECT * FROM contract_reviews WHERE works_order_id=?", (wid,), one=True)
    saved = json.loads(rv["checks_json"]) if rv and rv["checks_json"] else {}
    comparison = []
    if quote:
        q_val, j_val = round(quote["estimated_value"] or 0, 2), round(wo["contract_value"] or 0, 2)
        comparison.append({"item": "Price", "quote": q_val, "order": j_val, "kind": "money", "match": abs(q_val - j_val) < 0.005})
    comparison.append({"item": "Customer PO recorded", "quote": None,
                       "order": (wo["customer_po"] or "") + (f" line {wo['customer_po_line']}" if wo["customer_po_line"] else "") or None,
                       "kind": "text", "match": bool((wo["customer_po"] or "").strip())})
    comparison.append({"item": "Required by date set", "quote": None, "order": wo["required_by"], "kind": "text", "match": bool(wo["required_by"])})
    return jsonify({
        "job": {"works_order_id": wid, "job_number": wo["job_number"], "job_name": wo["job_name"], "customer_name": wo["customer_name"],
                "contract_code": wo["contract_code"], "review_status": wo["review_status"]},
        "quote": {"quote_id": quote["quote_id"], "quote_number": quote["quote_number"]} if quote else None,
        "comparison": comparison,
        "checks": [{"key": k, "label": label, **(saved.get(k) or {"result": "", "comment": ""})} for k, label in CONTRACT_REVIEW_CHECKS],
        "outcome": rv["outcome"] if rv else None, "comment": rv["comment"] if rv else None,
        "reviewed_by": rv["reviewed_by"] if rv else None, "review_date": rv["review_date"] if rv else None,
        "signed_off": bool(rv["signed_off"]) if rv else False,
    })


@app.route("/api/works-orders/<int:wid>/contract-review", methods=["PUT"])
@permission_required("quality:write")
def save_contract_review(wid):
    data = request.get_json(force=True) or {}
    db = get_db()
    wo = globals.query_db(db, "SELECT * FROM works_orders WHERE works_order_id=?", (wid,), one=True)
    if not wo:
        return jsonify({"error": "Job not found"}), 404
    outcome = data.get("outcome") or None
    if outcome and outcome not in CONTRACT_REVIEW_OUTCOMES:
        return jsonify({"error": "That outcome isn't valid."}), 400
    checks = {}
    for key, _label in CONTRACT_REVIEW_CHECKS:
        item = (data.get("checks") or {}).get(key) or {}
        result = item.get("result") or ""
        if result and result not in CONTRACT_REVIEW_RESULTS:
            return jsonify({"error": "A check result must be Yes, No or N/A."}), 400
        checks[key] = {"result": result, "comment": (item.get("comment") or "").strip()}
    sign_off = bool(data.get("sign_off"))
    reviewed_by = (data.get("reviewed_by") or "").strip() or current_user_name()
    if sign_off:
        if outcome != "Accepted as ordered":
            return jsonify({"error": "To sign off, the outcome must be \"Accepted as ordered\". If there is a query, save it as \"Query raised with customer\"."}), 400
        unanswered = [label for key, label in CONTRACT_REVIEW_CHECKS if not checks[key]["result"]]
        if unanswered:
            return jsonify({"error": "Answer every check before signing off. Still to answer: " + "; ".join(unanswered) + "."}), 400
    quote = globals.query_db(db, "SELECT quote_id FROM quotes WHERE works_order_id=? ORDER BY quote_id DESC LIMIT 1", (wid,), one=True)
    who, today = current_user_name(), date.today().isoformat()
    existing = globals.query_db(db, "SELECT review_id FROM contract_reviews WHERE works_order_id=?", (wid,), one=True)
    vals = (quote["quote_id"] if quote else None, json.dumps(checks), outcome, (data.get("comment") or "").strip() or None,
            reviewed_by, today if (sign_off or outcome) else None, 1 if sign_off else 0)
    if existing:
        globals.query_db(db, """UPDATE contract_reviews SET quote_id=?, checks_json=?, outcome=?, comment=?, reviewed_by=?, review_date=?, signed_off=?,
                      updated_by=?, updated_date=datetime('now') WHERE works_order_id=?""", (*vals, who, wid), commit=False)
    else:
        globals.query_db(db, """INSERT INTO contract_reviews (quote_id, checks_json, outcome, comment, reviewed_by, review_date, signed_off, created_by, works_order_id)
                      VALUES (?,?,?,?,?,?,?,?,?)""", (*vals, who, wid), commit=False)
    new_status = _review_status_for(sign_off, outcome)
    log_field_changes(db, "works_orders", wid, wo, {"review_status": new_status}, ["review_status"], who)
    globals.query_db(db, "UPDATE works_orders SET review_status=? WHERE works_order_id=?", (new_status, wid), commit=False)
    db.commit()
    return jsonify({"ok": True, "review_status": new_status})


# --------------------------------------------------------------------------
# Enquiries (sales pipeline, v1.13.0)
# --------------------------------------------------------------------------
ENQUIRY_OPEN_STAGES = ["New", "Contacted", "Info / visit needed", "Ready to quote"]
ENQUIRY_DEFAULT_WEIGHTS = {"New": 10, "Contacted": 25, "Info / visit needed": 40, "Ready to quote": 70}
ENQUIRY_FIELDS = ["customer_id", "prospect_name", "contact_name", "owner", "source", "received_date",
                  "estimated_value", "description", "stage", "next_follow_up", "next_action"]


def get_enquiry_weights(db):
    weights = dict(ENQUIRY_DEFAULT_WEIGHTS)
    row = globals.query_db(db, "SELECT value FROM settings WHERE key='enquiry_stage_weights'", one=True)
    if row and row["value"]:
        try:
            stored = json.loads(row["value"])
            for stage in ENQUIRY_OPEN_STAGES:
                if stage in stored:
                    weights[stage] = max(0, min(100, safe_float(stored[stage])))
        except (ValueError, TypeError):
            pass
    return weights


def get_next_enquiry_number(db):
    max_n = 0
    for r in globals.query_db(db, "SELECT enquiry_number FROM enquiries", one=False):
        m = re.search(r"(\d+)\s*$", r["enquiry_number"] or "")
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"ENQ-{max_n + 1:04d}"


def enquiry_follow_up_state(row, today=None):
    """'overdue' / 'today' / 'upcoming' / 'none' for an open enquiry."""
    if row["stage"] not in ENQUIRY_OPEN_STAGES:
        return "closed"
    fu = (row["next_follow_up"] or "")[:10]
    if not fu:
        return "none"
    today_s = (today or date.today()).isoformat()
    if fu < today_s:
        return "overdue"
    return "today" if fu == today_s else "upcoming"


def enquiry_rows(db, where="", params=()):
    weights = get_enquiry_weights(db)
    rows = globals.query_db(db, 
        f"""SELECT e.*, c.customer_name,
                   COALESCE(c.customer_name, e.prospect_name) AS display_name,
                   q.quote_number,
                   (SELECT COUNT(*) FROM enquiry_activity a WHERE a.enquiry_id = e.enquiry_id) AS activity_count,
                   (SELECT MAX(activity_date) FROM enquiry_activity a WHERE a.enquiry_id = e.enquiry_id) AS last_activity
            FROM enquiries e
            LEFT JOIN customers c ON c.customer_id = e.customer_id
            LEFT JOIN quotes q ON q.quote_id = e.quote_id
            {where} ORDER BY e.enquiry_id DESC""", params, one=False)
    out = []
    today = date.today()
    for r in rows:
        d = dict(r)
        d["follow_up_state"] = enquiry_follow_up_state(r, today)
        fu = (r["next_follow_up"] or "")[:10]
        d["days_overdue"] = (today - date.fromisoformat(fu)).days if d["follow_up_state"] == "overdue" else 0
        d["weight_pct"] = weights.get(r["stage"], 0) if r["stage"] in ENQUIRY_OPEN_STAGES else 0
        d["weighted_value"] = round((r["estimated_value"] or 0) * d["weight_pct"] / 100.0, 2)
        out.append(d)
    return out


def enquiry_summary(items):
    open_items = [e for e in items if e["stage"] in ENQUIRY_OPEN_STAGES]
    return {
        "open_count": len(open_items),
        "overdue_count": sum(1 for e in open_items if e["follow_up_state"] == "overdue"),
        "due_today_count": sum(1 for e in open_items if e["follow_up_state"] == "today"),
        "no_follow_up_count": sum(1 for e in open_items if e["follow_up_state"] == "none"),
        "estimated_value": round(sum(e["estimated_value"] or 0 for e in open_items), 2),
        "weighted_value": round(sum(e["weighted_value"] for e in open_items), 2),
    }


@app.route("/api/enquiries")
@login_required
def list_enquiries():
    db = get_db()
    status = request.args.get("status", "open")
    where = ""
    if status == "open":
        where = "WHERE e.stage IN (%s)" % ",".join("?" for _ in ENQUIRY_OPEN_STAGES)
        params = ENQUIRY_OPEN_STAGES
    elif status == "closed":
        where = "WHERE e.stage NOT IN (%s)" % ",".join("?" for _ in ENQUIRY_OPEN_STAGES)
        params = ENQUIRY_OPEN_STAGES
    else:
        params = ()
    items = enquiry_rows(db, where, params)
    all_open = enquiry_rows(db, "WHERE e.stage IN (%s)" % ",".join("?" for _ in ENQUIRY_OPEN_STAGES), ENQUIRY_OPEN_STAGES)
    return jsonify({"enquiries": items, "summary": enquiry_summary(all_open),
                    "stages": ENQUIRY_OPEN_STAGES, "weights": get_enquiry_weights(db)})


@app.route("/api/enquiries/<int:eid>")
@login_required
def get_enquiry(eid):
    db = get_db()
    found = enquiry_rows(db, "WHERE e.enquiry_id=?", (eid,))
    if not found:
        return jsonify({"error": "Enquiry not found"}), 404
    activity = rows_to_list(globals.query_db(db, 
        "SELECT * FROM enquiry_activity WHERE enquiry_id=? ORDER BY activity_date DESC, activity_id DESC", (eid,), one=False))
    return jsonify({**found[0], "activity": activity})


@app.route("/api/enquiries", methods=["POST"])
@permission_required("enquiries:write")
def create_enquiry():
    data = request.get_json(force=True) or {}
    description = (data.get("description") or "").strip()
    prospect = (data.get("prospect_name") or "").strip()
    customer_id = data.get("customer_id") or None
    if not description:
        return jsonify({"error": "Please describe what the enquiry is for."}), 400
    if not customer_id and not prospect:
        return jsonify({"error": "Choose an existing customer or type the prospect's name."}), 400
    stage = data.get("stage") or "New"
    if stage not in ENQUIRY_OPEN_STAGES:
        return jsonify({"error": "Unknown stage."}), 400
    db = get_db()
    number = get_next_enquiry_number(db)
    enq_id = globals.query_db(db, 
        """INSERT INTO enquiries (enquiry_number, customer_id, prospect_name, contact_name, owner, source, received_date,
               estimated_value, description, stage, next_follow_up, next_action, created_by)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (number, customer_id, None if customer_id else prospect, data.get("contact_name"),
         data.get("owner") or current_user_name(), data.get("source"),
         data.get("received_date") or date.today().isoformat(), safe_float(data.get("estimated_value"), None),
         description, stage, data.get("next_follow_up") or None, data.get("next_action"), current_user_name()), commit=True)
    return jsonify({"enquiry_id": enq_id, "enquiry_number": number}), 201


@app.route("/api/enquiries/<int:eid>", methods=["PUT"])
@permission_required("enquiries:write")
def update_enquiry(eid):
    data = request.get_json(force=True) or {}
    db = get_db()
    old = globals.query_db(db, "SELECT * FROM enquiries WHERE enquiry_id=?", (eid,), one=True)
    if not old:
        return jsonify({"error": "Enquiry not found"}), 404
    if old["stage"] not in ENQUIRY_OPEN_STAGES:
        return jsonify({"error": "This enquiry is closed. It can no longer be edited."}), 400
    new = {f: (data[f] if f in data else old[f]) for f in ENQUIRY_FIELDS}
    if new["stage"] not in ENQUIRY_OPEN_STAGES:
        return jsonify({"error": "Unknown stage. Use Convert to quote or Close as lost to finish an enquiry."}), 400
    if not (new["description"] or "").strip():
        return jsonify({"error": "Please describe what the enquiry is for."}), 400
    if new["customer_id"]:
        new["prospect_name"] = None
    elif not (new["prospect_name"] or "").strip():
        return jsonify({"error": "Choose an existing customer or type the prospect's name."}), 400
    new["customer_id"] = new["customer_id"] or None
    new["estimated_value"] = safe_float(new["estimated_value"], None) if new["estimated_value"] not in (None, "") else None
    new["next_follow_up"] = new["next_follow_up"] or None
    who = current_user_name()
    log_field_changes(db, "enquiries", eid, old, new, ["stage", "estimated_value", "owner", "next_follow_up", "customer_id"], who)
    globals.query_db(db, 
        """UPDATE enquiries SET customer_id=?, prospect_name=?, contact_name=?, owner=?, source=?, received_date=?,
               estimated_value=?, description=?, stage=?, next_follow_up=?, next_action=?,
               updated_by=?, updated_date=datetime('now') WHERE enquiry_id=?""",
        (new["customer_id"], new["prospect_name"], new["contact_name"], new["owner"], new["source"], new["received_date"],
         new["estimated_value"], new["description"], new["stage"], new["next_follow_up"], new["next_action"], who, eid), commit=True)
    return jsonify({"ok": True})


@app.route("/api/enquiries/<int:eid>/activity", methods=["POST"])
@permission_required("enquiries:write")
def add_enquiry_activity(eid):
    data = request.get_json(force=True) or {}
    note = (data.get("note") or "").strip()
    if not note:
        return jsonify({"error": "Please write a short note about the contact."}), 400
    db = get_db()
    enq = globals.query_db(db, "SELECT * FROM enquiries WHERE enquiry_id=?", (eid,), one=True)
    if not enq:
        return jsonify({"error": "Enquiry not found"}), 404
    who = current_user_name()
    globals.query_db(db, "INSERT INTO enquiry_activity (enquiry_id, activity_date, method, note, logged_by) VALUES (?,?,?,?,?)",
               (eid, data.get("activity_date") or date.today().isoformat(), data.get("method"), note, who), commit=False)
    if enq["stage"] in ENQUIRY_OPEN_STAGES and "next_follow_up" in data:
        new = {"next_follow_up": data.get("next_follow_up") or None}
        log_field_changes(db, "enquiries", eid, enq, new, ["next_follow_up"], who)
        globals.query_db(db, "UPDATE enquiries SET next_follow_up=?, next_action=COALESCE(?, next_action), updated_by=?, updated_date=datetime('now') WHERE enquiry_id=?",
                   (new["next_follow_up"], data.get("next_action") or None, who, eid), commit=False)
    db.commit()
    return jsonify({"ok": True}), 201


@app.route("/api/enquiries/<int:eid>/lose", methods=["POST"])
@permission_required("enquiries:write")
def lose_enquiry(eid):
    data = request.get_json(force=True) or {}
    reason = (data.get("reason") or "").strip()
    if not reason:
        return jsonify({"error": "Please give a reason (lost to a competitor, no bid, customer went quiet...)."}), 400
    db = get_db()
    enq = globals.query_db(db, "SELECT * FROM enquiries WHERE enquiry_id=?", (eid,), one=True)
    if not enq:
        return jsonify({"error": "Enquiry not found"}), 404
    if enq["stage"] not in ENQUIRY_OPEN_STAGES:
        return jsonify({"error": "This enquiry is already closed."}), 400
    who = current_user_name()
    log_field_changes(db, "enquiries", eid, enq, {"stage": "Lost"}, ["stage"], who)
    globals.query_db(db, """UPDATE enquiries SET stage='Lost', lost_reason=?, closed_date=date('now'), next_follow_up=NULL,
                  updated_by=?, updated_date=datetime('now') WHERE enquiry_id=?""", (reason, who, eid), commit=True)
    return jsonify({"ok": True})


@app.route("/api/enquiries/<int:eid>/convert", methods=["POST"])
@permission_required("enquiries:write")
def convert_enquiry_to_quote(eid):
    """Turns an enquiry into a Draft quote. A prospect becomes a customer record at this point."""
    db = get_db()
    enq = globals.query_db(db, "SELECT * FROM enquiries WHERE enquiry_id=?", (eid,), one=True)
    if not enq:
        return jsonify({"error": "Enquiry not found"}), 404
    if enq["stage"] not in ENQUIRY_OPEN_STAGES:
        return jsonify({"error": "This enquiry is already closed."}), 400
    who = current_user_name()
    customer_id = enq["customer_id"]
    if not customer_id:
        name = (enq["prospect_name"] or "").strip()
        existing = globals.query_db(db, "SELECT customer_id FROM customers WHERE LOWER(customer_name)=LOWER(?)", (name,), one=True)
        if existing:
            customer_id = existing["customer_id"]
        else:
            customer_id = globals.query_db(db, "INSERT INTO customers (customer_name, contact_name, created_by, created_date) VALUES (?,?,?,datetime('now'))",
                             (name, enq["contact_name"], who), commit=False)
    quote_number = get_next_quote_number(db)
    today_s = date.today().isoformat()
    quote_id = globals.query_db(db, 
        """INSERT INTO quotes (quote_number, customer_id, description, owner, status, estimated_value, quote_date, valid_until,
               notes, contact_name, created_by)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (quote_number, customer_id, enq["description"], enq["owner"], "Draft", enq["estimated_value"], today_s,
         default_valid_until(today_s), f"Created from enquiry {enq['enquiry_number']}", enq["contact_name"], who), commit=False)
    log_field_changes(db, "enquiries", eid, enq, {"stage": "Quoted"}, ["stage"], who)
    globals.query_db(db, """UPDATE enquiries SET stage='Quoted', quote_id=?, customer_id=?, closed_date=date('now'), next_follow_up=NULL,
                  updated_by=?, updated_date=datetime('now') WHERE enquiry_id=?""", (quote_id, customer_id, who, eid), commit=False)
    globals.query_db(db, "INSERT INTO enquiry_activity (enquiry_id, activity_date, method, note, logged_by) VALUES (?,?,?,?,?)",
               (eid, today_s, "System", f"Converted to quote {quote_number}", who), commit=False)
    db.commit()
    return jsonify({"quote_id": quote_id, "quote_number": quote_number, "customer_id": customer_id}), 201


@app.route("/api/enquiries/<int:eid>", methods=["DELETE"])
@permission_required("all")
def delete_enquiry(eid):
    db = get_db()
    if not globals.query_db(db, "SELECT 1 FROM enquiries WHERE enquiry_id=?", (eid,), one=True):
        return jsonify({"error": "Enquiry not found"}), 404
    globals.query_db(db, "DELETE FROM enquiry_activity WHERE enquiry_id=?", (eid,), commit=False)
    globals.query_db(db, "DELETE FROM enquiries WHERE enquiry_id=?", (eid,), commit=False)
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/settings/enquiry-weights", methods=["GET"])
@login_required
def get_enquiry_weights_route():
    return jsonify({"weights": get_enquiry_weights(get_db()), "stages": ENQUIRY_OPEN_STAGES})


@app.route("/api/settings/enquiry-weights", methods=["PUT"])
@permission_required("all")
def update_enquiry_weights():
    data = request.get_json(force=True) or {}
    incoming = data.get("weights") or {}
    clean = {}
    for stage in ENQUIRY_OPEN_STAGES:
        v = safe_float(incoming.get(stage), None)
        if v is None or v < 0 or v > 100:
            return jsonify({"error": f"The chance for '{stage}' must be a number from 0 to 100."}), 400
        clean[stage] = v
    db = get_db()
    globals.query_db(db, "INSERT INTO settings (key, value) VALUES ('enquiry_stage_weights', ?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(clean),), commit=True)
    return jsonify({"ok": True})


def enquiry_alerts(db):
    out = []
    for e in enquiry_rows(db, "WHERE e.stage IN (%s)" % ",".join("?" for _ in ENQUIRY_OPEN_STAGES), ENQUIRY_OPEN_STAGES):
        if e["follow_up_state"] == "overdue":
            msg = f"Follow-up was due {e['next_follow_up'][:10]} - chase or re-date it."
            out.append({"level": "amber", "type": "Enquiry Follow-up", "job_number": e["enquiry_number"],
                        "job_name": e["display_name"] or "", "message": msg, "enquiry_id": e["enquiry_id"],
                        "alert_key": alert_key(e["enquiry_number"], "Enquiry Follow-up", msg)})
    return out


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
        sql += " AND (wo.job_number LIKE ? OR wo.job_name LIKE ? OR c.customer_name LIKE ? OR wo.project_manager LIKE ? OR wo.customer_po LIKE ?)"
        params += [f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"]
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
    rows = globals.query_db(db, sql, params, one=False)
    # Highest job number at the top (TM-9596 above TM-9595).
    rows = sorted(rows, key=lambda r: number_sort_key(r["job_number"]), reverse=True)
    result = load_all_jobs_with_metrics(db, rows=rows)
    return jsonify(result)


@app.route("/api/works-orders/<int:wid>", methods=["GET"])
@login_required
def get_works_order(wid):
    db = get_db()
    row = globals.query_db(db, 
        """SELECT wo.*, c.customer_name FROM works_orders wo
           JOIN customers c ON c.customer_id = wo.customer_id WHERE wo.works_order_id=?""",
        (wid,), one=True
    )
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
    # Customer PO and line: what the form sent, otherwise the fixed yearly one for this site + contract code + year.
    po_for_new = (data.get("customer_po") or "").strip() or None
    line_for_new = (data.get("customer_po_line") or "").strip() or None
    if not po_for_new:
        found = lookup_fixed_po(db, data.get("customer_id"), data.get("contract_code"),
                                job_year(data.get("start_date")))
        if found:
            po_for_new, line_for_new = found["po_number"], found["line_number"]
    # The job number is always server-assigned - never taken from the request.
    # (Retries cover the rare case of two people saving at the same instant.)
    for attempt in range(5):
        job_number = get_next_works_order_number(db)
        try:
            works_id = globals.query_db(db, 
                """INSERT INTO works_orders
                   (job_number, job_name, customer_id, project_manager, status, start_date, end_date, contract_value, budget_cost, budget_is_estimated, sharepoint_url, contract_code, location, created_by, required_by, customer_po, customer_po_line, review_status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'Not reviewed')""",
                (job_number, data.get("job_name"), data.get("customer_id"), data.get("project_manager"),
                 data.get("status", "Draft"), data.get("start_date"), data.get("end_date"),
                 safe_float(data.get("contract_value")), safe_float(data.get("budget_cost")), 0, data.get("sharepoint_url"),
                 data.get("contract_code") or None, data.get("location") or None, current_user_name(),
                 data.get("required_by") or None, po_for_new, line_for_new), commit=True
            )
            break
        except Exception as e:
            db.rollback()
            if "job_number" in str(e) and attempt < 4:
                continue
            return jsonify({"error": str(e)}), 400
    return jsonify({"works_order_id": works_id, "job_number": job_number}), 201


# --------------------------------------------------------------------------
# Fixed Yearly Customer PO's: one purchase order + line per site + contract code + year
# --------------------------------------------------------------------------
FIXED_PO_SITES = migrations.FIXED_PO_SITES
FIXED_PO_CODES = migrations.FIXED_PO_CODES
FIXED_PO_BACKFILL_CODES = ("FS - L", "FS - M", "FS - H")   # existing jobs on these codes are filled in; FR/TL jobs are not


def canonical_fixed_code(code):
    c = " ".join((code or "").upper().replace("-", " - ").split())
    c = c.replace(" -  ", " - ")
    for known in FIXED_PO_CODES:
        if " ".join(known.upper().split()) == c:
            return known
    return None


def job_year(start_date):
    """The year a job belongs to for its fixed PO: its start date's year, else this year."""
    try:
        return int((start_date or "")[:4])
    except ValueError:
        return date.today().year


def lookup_fixed_po(db, customer_id, contract_code, year):
    code = canonical_fixed_code(contract_code)
    if not code or not customer_id:
        return None
    try:
        row = globals.query_db(db, "SELECT po_number, line_number FROM fixed_customer_pos WHERE customer_id=? AND contract_code=? AND year=?",
                         (int(customer_id), code, int(year)), one=True)
    except (TypeError, ValueError):
        return None
    return {"po_number": row["po_number"], "line_number": row["line_number"] or ""} if row else None


@app.route("/api/works-orders/fixed-po-lookup")
@login_required
def works_order_fixed_po_lookup():
    found = lookup_fixed_po(get_db(), request.args.get("customer_id"), request.args.get("contract_code"),
                            job_year(request.args.get("start_date")))
    return jsonify(found or {})


@app.route("/api/fixed-pos")
@login_required
def list_fixed_pos():
    db = get_db()
    try:
        year = int(request.args.get("year") or date.today().year)
    except ValueError:
        year = date.today().year
    placeholders = ",".join("?" * len(FIXED_PO_SITES))
    sites = rows_to_list(globals.query_db(db,
        f"SELECT customer_id, customer_name FROM customers WHERE customer_name IN ({placeholders})",
        tuple(FIXED_PO_SITES),one=False))
    order = {n: i for i, n in enumerate(FIXED_PO_SITES)}
    sites.sort(key=lambda s: order[s["customer_name"]])
    cells = {}
    for r in globals.query_db(db, "SELECT * FROM fixed_customer_pos WHERE year=?", (year,), one=False):
        cells[f"{r['customer_id']}|{r['contract_code']}"] = {
            "fixed_po_id": r["fixed_po_id"],
            "po_number": r["po_number"],
            "line_number": r["line_number"] or "",
            "updated_by": r["updated_by"] or r["created_by"],
            "updated_date": r["updated_date"] or r["created_date"],
        }
    years = {r["year"] for r in globals.query_db(db, "SELECT DISTINCT year FROM fixed_customer_pos", one=False)}
    years |= {date.today().year, date.today().year + 1}
    return jsonify({"year": year, "years": sorted(years), "sites": sites, "codes": FIXED_PO_CODES, "cells": cells})


@app.route("/api/fixed-pos", methods=["PUT"])
@permission_required("fixed_pos:write")
def save_fixed_po():
    data = request.get_json(force=True) or {}
    db = get_db()
    code = canonical_fixed_code(data.get("contract_code"))
    try:
        year, cid = int(data.get("year")), int(data.get("customer_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "A site, contract code and year are needed."}), 400
    if not code or not (2000 <= year <= 2100):
        return jsonify({"error": "That contract code or year isn't one of the fixed yearly ones."}), 400
    cust = globals.query_db(db, "SELECT customer_name FROM customers WHERE customer_id=?", (cid,), one=True)
    if not cust or cust["customer_name"] not in FIXED_PO_SITES:
        return jsonify({"error": "That customer isn't one of the fixed yearly PO sites."}), 400
    po = (data.get("po_number") or "").strip()
    line = (data.get("line_number") or "").strip()
    who = current_user_name()
    label = f"{cust['customer_name']} / {code} / {year}"
    old = globals.query_db(db, "SELECT * FROM fixed_customer_pos WHERE customer_id=? AND contract_code=? AND year=?", (cid, code, year), one=True)

    def audit(field, o, n, rid):
        if (o or "") != (n or ""):
            globals.query_db(db, "INSERT INTO audit_log (table_name, record_id, field_name, old_value, new_value, changed_by) VALUES (?,?,?,?,?,?)",
                       ("fixed_customer_pos", rid, f"{label}: {field}", o, n, who), commit=False)

    applied = 0
    if not po:
        if old:
            audit("PO number", old["po_number"], None, old["fixed_po_id"])
            audit("Line number", old["line_number"], None, old["fixed_po_id"])
            globals.query_db(db, "DELETE FROM fixed_customer_pos WHERE fixed_po_id=?", (old["fixed_po_id"],), commit=False)
        db.commit()
        return jsonify({"ok": True, "cleared": True, "applied_to_existing_jobs": 0})
    if old:
        audit("PO number", old["po_number"], po, old["fixed_po_id"])
        audit("Line number", old["line_number"], line, old["fixed_po_id"])
        globals.query_db(db, "UPDATE fixed_customer_pos SET po_number=?, line_number=?, updated_by=?, updated_date=datetime('now') WHERE fixed_po_id=?",
                   (po, line or None, who, old["fixed_po_id"]), commit=False)
    else:
        po_num = globals.query_db(db, "INSERT INTO fixed_customer_pos (customer_id, contract_code, year, po_number, line_number, created_by) VALUES (?,?,?,?,?,?)",
                         (cid, code, year, po, line or None, who), commit=False)
        audit("PO number", None, po, po_num)
        audit("Line number", None, line, po_num)
    # Existing jobs already on FS - L / M / H for this site and year that have no customer PO yet get this one.
    # A job that already has one is never changed (it keeps what it was raised under).
    if code in FIXED_PO_BACKFILL_CODES:
        jobs = globals.query_db(db,
            """SELECT works_order_id FROM works_orders WHERE customer_id=? AND UPPER(REPLACE(contract_code,' ',''))=?
               AND COALESCE(customer_po,'')='' AND job_number NOT LIKE 'HIST-PO-%'
               AND CAST(substr(COALESCE(NULLIF(start_date,''), NULLIF(created_date,'')), 1, 4) AS INTEGER)=?""",
            (cid, code.upper().replace(" ", ""), year), one=False,)
        for j in jobs:
            wo_id = j["works_order_id"]
            globals.query_db(db,
                "UPDATE works_orders SET customer_po=?, customer_po_line=? WHERE works_order_id=?",
                (po, line or None, wo_id), commit=False,)
            globals.query_db(db,
                "INSERT INTO audit_log (table_name, record_id, field_name, old_value, new_value, changed_by) VALUES (?,?,?,?,?,?)",
                ("works_orders", wo_id, "customer_po", None, po, who), commit=False,)
            applied += 1
    db.commit()
    return jsonify({"ok": True, "applied_to_existing_jobs": applied})


@app.route("/api/fixed-pos/history")
@login_required
def fixed_po_history():
    rows = globals.query_db(get_db(), "SELECT * FROM audit_log WHERE table_name='fixed_customer_pos' ORDER BY audit_id DESC LIMIT 1000", one=False)
    return jsonify(rows_to_list(rows))


@app.route("/api/works-orders/<int:wid>", methods=["PUT"])
@permission_required("works_orders:write")
def update_works_order(wid):
    data = request.get_json(force=True) or {}
    db = get_db()
    old = globals.query_db(db, "SELECT * FROM works_orders WHERE works_order_id=?", (wid,), one=True)
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
    # Customer PO / line are only changed when the request actually carries them - never blanked by omission.
    new_po = (data["customer_po"] if "customer_po" in data else old["customer_po"]) or ""
    new_line = (data["customer_po_line"] if "customer_po_line" in data else old["customer_po_line"]) or ""
    new_po, new_line = str(new_po).strip(), str(new_line).strip()
    log_field_changes(db, "works_orders", wid, old, {"required_by": data.get("required_by") or None,
                                                      "customer_po": new_po or None, "customer_po_line": new_line or None},
                      ["required_by", "customer_po", "customer_po_line"], current_user_name())
    globals.query_db(db,
        """UPDATE works_orders SET job_number=?, job_name=?, customer_id=?, project_manager=?, status=?,
           start_date=?, end_date=?, contract_value=?, budget_cost=?, budget_is_estimated=?, sharepoint_url=?, contract_code=?,
           location=?, required_by=?, customer_po=?, customer_po_line=?, updated_by=?, updated_date=datetime('now') WHERE works_order_id=?""",
        (old["job_number"], data.get("job_name"), data.get("customer_id"), data.get("project_manager"),
         data.get("status"), data.get("start_date"), data.get("end_date"),
         safe_float(data.get("contract_value")), safe_float(data.get("budget_cost")), keep_estimated, data.get("sharepoint_url"),
         data.get("contract_code") or None, data.get("location") or None, data.get("required_by") or None,
         new_po or None, new_line or None, current_user_name(), wid), commit=True
    )
    return jsonify({"ok": True})


WORKS_ORDER_STATUSES = ("Draft", "Open", "In Progress", "On Hold", "Completed", "Closed", "Cancelled")


@app.route("/api/works-orders/bulk-status", methods=["POST"])
@permission_required("works_orders:write")
def bulk_update_works_order_status():
    """Change the status of a batch of jobs in one go (e.g. mark 40 jobs
    Completed). Each job's change is written to its History like a normal
    edit, and each job counts as edited in the app, exactly as if it had been
    changed one at a time. Nothing else about a job is touched."""
    data = request.get_json(force=True) or {}
    status = data.get("status")
    if status not in WORKS_ORDER_STATUSES:
        return jsonify({"error": "Choose a status to change them to."}), 400
    try:
        ids = sorted({int(i) for i in (data.get("ids") or [])})
    except (TypeError, ValueError):
        return jsonify({"error": "Those jobs could not be read."}), 400
    if not ids:
        return jsonify({"error": "No jobs were selected."}), 400
    if len(ids) > 5000:
        return jsonify({"error": "That is too many jobs at once - select 5,000 or fewer."}), 400
    db = get_db()
    who = current_user_name()
    updated = unchanged = 0
    for wid in ids:
        old = globals.query_db(db, "SELECT * FROM works_orders WHERE works_order_id=?", (wid,), one=True)
        if not old or old["job_number"].startswith("HIST-PO-"):
            continue
        if old["status"] == status:
            unchanged += 1
            continue
        log_field_changes(db, "works_orders", wid, old, {"status": status}, ["status"], who)
        globals.query_db(db, "UPDATE works_orders SET status=?, updated_by=?, updated_date=datetime('now') WHERE works_order_id=?",
                   (status, who, wid), commit=False)
        updated += 1
    db.commit()
    return jsonify({"ok": True, "updated": updated, "unchanged": unchanged, "status": status})


@app.route("/api/works-orders/bulk-contract-code", methods=["POST"])
@permission_required("works_orders:write")
def bulk_update_works_order_contract_code():
    """Change the contract code of a batch of jobs (e.g. 390 plain 'FS' jobs to 'FS - L'). Each change goes into the
    job's History. Jobs moved onto a fixed-yearly-PO code (FS - L / M / H / FR/TL) that have no customer PO yet are given
    the fixed one for their site and year if it has been set. A backup of the database is taken first. Not open to
    Service Engineers: the contract code decides a job's margin target."""
    if is_service_engineer():
        return jsonify({"error": "Your role (Service Engineer) can't change contract codes."}), 403
    data = request.get_json(force=True) or {}
    db = get_db()
    known = {
        r["contract_code"]
        for r in globals.query_db(db, "SELECT DISTINCT contract_code FROM works_orders WHERE contract_code IS NOT NULL AND contract_code != ''",one=False,)
    }
    known |= {
        r["contract_code"]
        for r in globals.query_db(db, "SELECT contract_code FROM contract_code_targets", one=False)
    }
    known |= set(FIXED_PO_CODES) | {"FS - HQ"}
    by_upper = {k.upper(): k for k in known}
    wanted = " ".join((data.get("contract_code") or "").split())
    code = by_upper.get(wanted.upper()) or canonical_fixed_code(wanted) or (wanted if wanted.upper() in by_upper else None)
    if not code:
        return jsonify({"error": "Choose a contract code to change them to."}), 400
    try:
        ids = sorted({int(i) for i in (data.get("ids") or [])})
    except (TypeError, ValueError):
        return jsonify({"error": "Those jobs could not be read."}), 400
    if not ids:
        return jsonify({"error": "No jobs were selected."}), 400
    if len(ids) > 5000:
        return jsonify({"error": "That is too many jobs at once - select 5,000 or fewer."}), 400

    db_location.backup_db()
    who = current_user_name()
    fixed_site_ids = {
        r["customer_id"]
        for r in globals.query_db(db, f"SELECT customer_id FROM customers WHERE customer_name IN ({','.join('?' * len(FIXED_PO_SITES))})", tuple(FIXED_PO_SITES), one=False)
    }

    # Fetch all selected works orders in chunks of 500 instead of one SELECT per job
    existing_by_id = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = globals.query_db(db, f"SELECT * FROM works_orders WHERE works_order_id IN ({placeholders})", tuple(chunk), one=False)
        for row in rows:
            existing_by_id[row["works_order_id"]] = row

    updated = unchanged = po_filled = po_missing = 0
    for wid in ids:
        old = existing_by_id.get(wid)
        if not old or old["job_number"].startswith("HIST-PO-"):
            continue
        if (old["contract_code"] or "") == code:
            unchanged += 1
            continue
        log_field_changes(db, "works_orders", wid, old, {"contract_code": code}, ["contract_code"], who)
        # Not stamped as "edited by a person": the Order Book refresh keeps this code (see update_order_book.py).
        globals.query_db( db, "UPDATE works_orders SET contract_code=? WHERE works_order_id=?", (code, wid), commit=False)
        updated += 1
        if canonical_fixed_code(code) and old["customer_id"] in fixed_site_ids and not (old["customer_po"] or "").strip():
            found = lookup_fixed_po(db, old["customer_id"], code, job_year(old["start_date"] or old["created_date"]))
            if found:
                globals.query_db(db,
                    "UPDATE works_orders SET customer_po=?, customer_po_line=? WHERE works_order_id=?",
                    (found["po_number"], found["line_number"] or None, wid),
                    commit=False
                )
                globals.query_db(db,
                    "INSERT INTO audit_log (table_name, record_id, field_name, old_value, new_value, changed_by) VALUES (?,?,?,?,?,?)",
                    ("works_orders", wid, "customer_po", None, found["po_number"], who),
                    commit=False,
                )
                po_filled += 1
            else:
                po_missing += 1
    db.commit()
    return jsonify({
        "ok": True,
        "updated": updated,
        "unchanged": unchanged,
        "contract_code": code,
        "po_filled": po_filled,
        "po_missing": po_missing,
    })


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
    held = globals.query_db(db, 
        """SELECT s.part_number, a.quantity FROM stock_allocations a
           JOIN stock_items s ON s.stock_item_id = a.stock_item_id
           WHERE a.works_order_id=?""", (wid,), one=False)
    if held:
        items = ", ".join(f"{r['part_number']} x {r['quantity']:g}" for r in held)
        raise ValueError(f"Stock is still allocated to this works order ({items}). "
                         "On the Inventory page, move it back to a site (or remove the allocation), then delete the works order.")
    files_goods, files_sigs = [], []
    po_ids = [r["purchase_order_id"] for r in globals.query_db(db,
        "SELECT purchase_order_id FROM purchase_orders WHERE works_order_id=?", (wid,), one=False)]
    for pid in po_ids:
        gids = [r["goods_in_id"] for r in globals.query_db(db,
            "SELECT goods_in_id FROM goods_in WHERE purchase_order_id=?", (pid,), one=False)]
        for gid in gids:
            files_goods += [d["stored_filename"] for d in globals.query_db(db,
                "SELECT stored_filename FROM goods_in_documents WHERE goods_in_id=?", (gid,), one=False)]
            globals.query_db(db, "DELETE FROM goods_in WHERE goods_in_id=?", (gid,), commit=False)
    files_sigs = [d["signature_filename"] for d in globals.query_db(db,
        "SELECT signature_filename FROM despatch_notes WHERE works_order_id=? AND signature_filename IS NOT NULL", (wid,), one=False)]
    globals.query_db(db, "DELETE FROM despatch_notes WHERE works_order_id=?", (wid,), commit=False)
    globals.query_db(db, "DELETE FROM supplier_invoices WHERE works_order_id=?", (wid,), commit=False)
    globals.query_db(db, "DELETE FROM supplier_invoices WHERE purchase_order_id IN "
               "(SELECT purchase_order_id FROM purchase_orders WHERE works_order_id=?)", (wid,), commit=False)
    globals.query_db(db, "DELETE FROM purchase_orders WHERE works_order_id=?", (wid,), commit=False)
    globals.query_db(db, "DELETE FROM labour_entries WHERE works_order_id=?", (wid,), commit=False)
    globals.query_db(db, "DELETE FROM customer_invoices WHERE works_order_id=?", (wid,), commit=False)
    globals.query_db(db, "UPDATE quotes SET works_order_id=NULL WHERE works_order_id=?", (wid,), commit=False)
    globals.query_db(db, "UPDATE stock_movements SET from_works_order_id=NULL WHERE from_works_order_id=?", (wid,), commit=False)
    globals.query_db(db, "UPDATE stock_movements SET to_works_order_id=NULL WHERE to_works_order_id=?", (wid,), commit=False)
    globals.query_db(db, "DELETE FROM works_orders WHERE works_order_id=?", (wid,), commit=False)
    return files_goods, files_sigs


@app.route("/api/works-orders/<int:wid>", methods=["DELETE"])
@permission_required("all")
def delete_works_order(wid):
    db = get_db()
    if not globals.query_db(db, "SELECT 1 FROM works_orders WHERE works_order_id=?", (wid,), one=True):
        return jsonify({"error": "Works order not found"}), 404
    try:
        files_goods, files_sigs = delete_works_order_records(db, wid)
        db.commit()
    except ValueError as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        db.rollback()
        return jsonify({"error": f"This works order is still linked to other records and could not be deleted ({e})."}), 400
    for fn in files_goods:
        if fn and fn.startswith("https://"):
            try:
                blob.delete_blob(fn)
            except Exception:
                pass
    for fn in files_sigs:
        if fn and fn.startswith("https://"):
            try:
                blob.delete_blob(fn)
            except Exception:
                pass
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Purchase Orders
# --------------------------------------------------------------------------
def purchase_order_lines(db, pid):
    rows = globals.query_db(db, 
        "SELECT * FROM purchase_order_lines WHERE purchase_order_id=? ORDER BY line_id", (pid,), one=False
    )
    return rows_to_list(rows)


def save_purchase_order_lines(db, pid, lines):
    """Replace-all: deletes any existing lines for this PO and inserts the
    new set. Returns the computed subtotal (sum of qty * unit_price) so the
    caller can keep po_value in sync with what the lines actually add up
    to, rather than it being a second, independently-editable figure that
    could drift out of sync."""
    globals.query_db(db, "DELETE FROM purchase_order_lines WHERE purchase_order_id=?", (pid,), commit=False)
    subtotal = 0.0
    for ln in lines or []:
        qty = safe_float(ln.get("qty"), 1) or 1
        unit_price = safe_float(ln.get("unit_price"))
        subtotal += qty * unit_price
        globals.query_db(db, 
            """INSERT INTO purchase_order_lines (purchase_order_id, qty, part_number, item_number, description, unit_price)
               VALUES (?,?,?,?,?,?)""",
            (pid, qty, ln.get("part_number"), ln.get("item_number"), ln.get("description"), unit_price), commit=False
        )
    return subtotal


@app.route("/api/purchase-orders", methods=["GET"])
@login_required
def list_purchase_orders():
    db = get_db()
    rows = globals.query_db(db,
        """SELECT po.*, s.supplier_name, wo.job_number, wo.job_name, sta.label AS ship_to_label,
                  COALESCE((SELECT SUM(invoice_amount) FROM supplier_invoices si
                            WHERE si.purchase_order_id = po.purchase_order_id AND si.status != 'Cancelled'), 0) AS invoiced_total
           FROM purchase_orders po
           JOIN suppliers s ON s.supplier_id = po.supplier_id
           LEFT JOIN works_orders wo ON wo.works_order_id = po.works_order_id
           LEFT JOIN ship_to_addresses sta ON sta.ship_to_id = po.ship_to_id
           ORDER BY po.order_date DESC""", one=False
    )

    # Fetch ALL purchase order lines in ONE query instead of one per PO
    all_lines = globals.query_db(db,
        "SELECT * FROM purchase_order_lines ORDER BY purchase_order_id, line_id", one=False)
    lines_by_po = {}
    for ln in all_lines:
        lines_by_po.setdefault(ln["purchase_order_id"], []).append(ln)

    # Highest PO number at the top (PO-3302 above PO-3301).
    rows = sorted(rows, key=lambda r: (number_sort_key(r["po_number"]), r["purchase_order_id"]), reverse=True)
    for r in rows:
        r["lines"] = lines_by_po.get(r["purchase_order_id"], [])
    return jsonify(rows)


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
    if not globals.query_db(db, "SELECT 1 FROM works_orders WHERE works_order_id=? AND job_number NOT LIKE 'HIST-PO-%'",
            (data.get("works_order_id"),), one=True):
        return jsonify({"error": "Choose a works order that has been raised - a purchase order can't be placed against a job number that doesn't exist yet."}), 400
    blocked, override_reason = check_supplier_allowed(db, safe_int(data.get("supplier_id")), data)
    if blocked:
        return blocked
    # The PO number is always server-assigned - never taken from the request.
    for attempt in range(5):
        po_number = get_next_po_number(db)
        try:
            po_id = globals.query_db(db, 
                """INSERT INTO purchase_orders (po_number, works_order_id, supplier_id, description, order_date, po_value, status,
                      sharepoint_url, ship_to_id, date_required, account_number, raised_by, vat_rate)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (po_number, data.get("works_order_id"), data.get("supplier_id"), data.get("description"),
                 data.get("order_date"), po_value, data.get("status", "Draft"),
                 data.get("sharepoint_url"), data.get("ship_to_id") or None, data.get("date_required") or None,
                 data.get("account_number"), data.get("raised_by"), safe_float(data.get("vat_rate"), 20)), commit=False
            )
            if lines:
                subtotal = save_purchase_order_lines(db, po_id, lines)
                globals.query_db(db, "UPDATE purchase_orders SET po_value=? WHERE purchase_order_id=?", (round(subtotal, 2), po_id), commit=False)
            if override_reason:
                globals.query_db(db, "INSERT INTO audit_log (table_name, record_id, field_name, old_value, new_value, changed_by) VALUES (?,?,?,?,?,?)",
                           ("purchase_orders", po_id, "ordered from suspended supplier", None, override_reason, current_user_name()), commit=False)
            db.commit()
            break
        except Exception as e:
            db.rollback()
            if "po_number" in str(e) and attempt < 4:
                continue
            return jsonify({"error": str(e)}), 400
    return jsonify({"purchase_order_id": po_id, "po_number": po_number}), 201


@app.route("/api/purchase-orders/<int:pid>", methods=["PUT"])
@permission_required("purchase_orders:write")
def update_purchase_order(pid):
    data = request.get_json(force=True) or {}
    db = get_db()
    old = globals.query_db(db, "SELECT * FROM purchase_orders WHERE purchase_order_id=?", (pid,), one=True)
    if not old:
        return jsonify({"error": "Not found"}), 404
    if data.get("works_order_id") in (None, ""):
        if not old["is_historic"]:
            return jsonify({"error": "Choose a works order for this purchase order."}), 400
        data["works_order_id"] = None
    elif not globals.query_db(db, "SELECT 1 FROM works_orders WHERE works_order_id=?", (data.get("works_order_id"),), one=True):
        return jsonify({"error": "That works order doesn't exist."}), 400
    blocked, override_reason = check_supplier_allowed(db, safe_int(data.get("supplier_id")), data, old_supplier_id=old["supplier_id"])
    if blocked:
        return blocked
    lines = data.get("lines")
    po_value = safe_float(data.get("po_value"))
    try:
        if override_reason:
            globals.query_db(db, "INSERT INTO audit_log (table_name, record_id, field_name, old_value, new_value, changed_by) VALUES (?,?,?,?,?,?)",
                       ("purchase_orders", pid, "ordered from suspended supplier", None, override_reason, current_user_name()), commit=False)
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
        globals.query_db(db,
            """UPDATE purchase_orders SET po_number=?, works_order_id=?, supplier_id=?, description=?,
               order_date=?, po_value=?, status=?, sharepoint_url=?, ship_to_id=?,
               date_required=?, account_number=?, raised_by=?, vat_rate=? WHERE purchase_order_id=?""",
            (old["po_number"], data.get("works_order_id"), data.get("supplier_id"), data.get("description"),
             data.get("order_date"), po_value, data.get("status"),
             data.get("sharepoint_url"), data.get("ship_to_id") or None, data.get("date_required") or None,
             data.get("account_number"), data.get("raised_by"), safe_float(data.get("vat_rate"), 20), pid), commit=False
        )
        if lines is not None:
            subtotal = save_purchase_order_lines(db, pid, lines)
            if lines:
                globals.query_db(db, "UPDATE purchase_orders SET po_value=? WHERE purchase_order_id=?", (round(subtotal, 2), pid), commit=False)
        db.commit()
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/purchase-orders/<int:pid>/pdf")
@login_required
def get_purchase_order_pdf(pid):
    db = get_db()
    po = globals.query_db(db, 
        """SELECT po.*, s.supplier_name, s.address AS supplier_address, wo.job_number,
                  sta.company_name AS ship_to_company_name, sta.address_line1 AS ship_to_address_line1,
                  sta.address_line2 AS ship_to_address_line2, sta.city AS ship_to_city, sta.postcode AS ship_to_postcode
           FROM purchase_orders po
           JOIN suppliers s ON s.supplier_id = po.supplier_id
           LEFT JOIN works_orders wo ON wo.works_order_id = po.works_order_id
           LEFT JOIN ship_to_addresses sta ON sta.ship_to_id = po.ship_to_id
           WHERE po.purchase_order_id=?""",
        (pid,), one=True
    )
    if not po:
        return jsonify({"error": "Purchase Order not found"}), 404
    lines = purchase_order_lines(db, pid)
    pdf_bytes = build_po_pdf(po, lines)
    log_document_access(db, "Purchase Order PDF" + (" (preview)" if request.args.get("preview") == "1" else ""), po["po_number"])
    return pdf_response(pdf_bytes, po["po_number"])


@app.route("/api/purchase-orders/<int:pid>/approve", methods=["POST"])
@permission_required("purchase_orders:write")
def approve_purchase_order(pid):
    db = get_db()
    globals.query_db(db, "UPDATE purchase_orders SET status='Approved' WHERE purchase_order_id=?", (pid,), commit=True)
    return jsonify({"ok": True})


@app.route("/api/purchase-orders/<int:pid>", methods=["DELETE"])
@permission_required("all")
def delete_purchase_order(pid):
    db = get_db()
    try:
        globals.query_db(db, "DELETE FROM purchase_orders WHERE purchase_order_id=?", (pid,), commit=True)
    except Exception:
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
def save_goods_in_documents(db, goods_in_id, files):
    """Validates and uploads one or more files to Private Vercel Blob storage
    against an existing goods_in row. If uploading or saving fails partway
    through, any blobs already uploaded in this batch are deleted cleanly."""
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

    # Read bytes and validate combined upload size before uploading anything
    prepared_files = []
    total_bytes = 0
    for f in files:
        file_bytes = f.read()
        size = len(file_bytes)
        total_bytes += size
        if total_bytes > GOODS_IN_MAX_UPLOAD_BYTES:
            size_mb = total_bytes / (1024 * 1024)
            return (f"Selected files total {size_mb:.1f} MB, which exceeds the 4 MB maximum per upload. "
                    "Please compress the images or upload them one at a time.", 413)
        prepared_files.append((f, file_bytes, size))

    uploaded_urls = []
    try:
        for f, file_bytes, size in prepared_files:
            original_name = secure_filename(f.filename) or "document"
            ext = os.path.splitext(original_name)[1].lower()
            blob_pathname = f"goods-in/{secrets.token_hex(16)}{ext}"
            
            # Upload to Private Vercel Blob store
            result = blob.put_blob(
                blob_pathname,
                file_bytes,
                access="private",
                content_type=f.mimetype or "application/octet-stream"
            )
            uploaded_urls.append(result.url)
            
            # Store the full Blob URL in the stored_filename column
            globals.query_db(db,
                """INSERT INTO goods_in_documents
                       (goods_in_id, original_filename, stored_filename, content_type, file_size, uploaded_by)
                   VALUES (?,?,?,?,?,?)""",
                (goods_in_id, original_name, result.url, f.mimetype, size, current_user_name()), commit=False
            )
    except Exception:
        for url in uploaded_urls:
            try:
                blob.delete_blob(url)
            except Exception:
                pass
        raise
    return None


@app.route("/api/goods-in", methods=["GET"])
@login_required
def list_goods_in():
    db = get_db()
    rows = globals.query_db(db,
        """SELECT gi.*, po.po_number, po.description AS po_description, s.supplier_name,
                  wo.job_number, sta.label AS ship_to_label,
                  (SELECT COUNT(*) FROM goods_in_documents d WHERE d.goods_in_id = gi.goods_in_id) AS document_count
           FROM goods_in gi
           JOIN purchase_orders po ON po.purchase_order_id = gi.purchase_order_id
           JOIN suppliers s ON s.supplier_id = po.supplier_id
           LEFT JOIN works_orders wo ON wo.works_order_id = po.works_order_id
           LEFT JOIN ship_to_addresses sta ON sta.ship_to_id = gi.ship_to_id
           ORDER BY gi.received_date DESC, gi.goods_in_id DESC""", one=False
    )
    return jsonify(rows)


@app.route("/api/goods-in/<int:gid>/documents", methods=["GET"])
@login_required
def list_goods_in_documents(gid):
    db = get_db()
    rows = globals.query_db(db,
        """SELECT document_id, original_filename, content_type, file_size, uploaded_by, uploaded_date
           FROM goods_in_documents WHERE goods_in_id=? ORDER BY uploaded_date, document_id""",
        (gid,), one=False
    )
    return jsonify(rows)


@app.route("/api/goods-in/documents/<int:doc_id>/download")
@login_required
def download_goods_in_document(doc_id):
    db = get_db()
    doc = globals.query_db(db,
        """SELECT d.*, gi.purchase_order_id, po.po_number
           FROM goods_in_documents d
           JOIN goods_in gi ON gi.goods_in_id = d.goods_in_id
           JOIN purchase_orders po ON po.purchase_order_id = gi.purchase_order_id
           WHERE d.document_id=?""",
        (doc_id,), one=True
    )
    if not doc:
        return jsonify({"error": "Document not found"}), 404
        
    blob_url = doc["stored_filename"]
    if not blob_url or not blob_url.startswith("https://"):
        return jsonify({"error": "That document's file is missing from cloud storage."}), 404
        
    try:
        blob_data = blob.get_blob(blob_url, access="private")
    except Exception:
        return jsonify({"error": "Could not retrieve document from storage - it may have been removed."}), 404

    log_document_access(db, "Goods In document", f"{doc['po_number']} - {doc['original_filename']}")
    
    return send_file(
        BytesIO(blob_data.body),
        mimetype=doc["content_type"] or blob_data.metadata.content_type or "application/octet-stream",
        as_attachment=True,
        download_name=doc["original_filename"],
    )


@app.route("/api/goods-in/<int:gid>/documents", methods=["POST"])
@permission_required("goods_in:write")
def add_goods_in_documents(gid):
    db = get_db()
    exists = globals.query_db(db, "SELECT 1 AS found FROM goods_in WHERE goods_in_id=?", (gid,), one=True)
    if not exists:
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
    """Administrator-only: lets a wrongly-attached document be removed,
    but never the last one on a record."""
    db = get_db()
    doc = globals.query_db(db, "SELECT * FROM goods_in_documents WHERE document_id=?", (doc_id,), one=True)
    if not doc:
        return jsonify({"error": "Document not found"}), 404
        
    count_row = globals.query_db(db,
        "SELECT COUNT(*) AS n FROM goods_in_documents WHERE goods_in_id=?", (doc["goods_in_id"],), one=True)
    remaining = count_row["n"] if count_row else 0
    if remaining <= 1:
        return jsonify({"error": "This is the only document on this Goods In record - upload a replacement "
                                  "before removing it."}), 400
                                  
    globals.query_db(db, "DELETE FROM goods_in_documents WHERE document_id=?", (doc_id,), commit=True)
    
    blob_url = doc["stored_filename"]
    if blob_url and blob_url.startswith("https://"):
        try:
            blob.delete_blob(blob_url)
        except Exception:
            pass
    return jsonify({"ok": True})

def goods_in_trace_values(data):
    """(batch, heat, cert_status, inspection_result) from a request, all optional; returns (tuple, error)."""
    cert = (data.get("cert_status") or "").strip() or None
    insp = (data.get("inspection_result") or "").strip() or None
    if cert and cert not in GOODS_IN_CERT_STATUSES:
        return None, "That certificate status isn't valid."
    if insp and insp not in GOODS_IN_INSPECTION_RESULTS:
        return None, "That inspection result isn't valid."
    return ((data.get("batch_number") or "").strip() or None, (data.get("heat_number") or "").strip() or None, cert, insp), None


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
    trace, trace_err = goods_in_trace_values(data)
    if trace_err:
        return jsonify({"error": trace_err}), 400
    db = get_db()
    goods_id = globals.query_db(db,
        """INSERT INTO goods_in (purchase_order_id, destination_type, ship_to_id, received_date,
             quantity_status, quantity_received, received_by, comment,
             batch_number, heat_number, cert_status, inspection_result)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (safe_int(data.get("purchase_order_id")), data.get("destination_type"),
        safe_int(data.get("ship_to_id")) if data.get("destination_type") == "Stock" else None,
        data.get("received_date"), data.get("quantity_status"),
        safe_float(data.get("quantity_received"), None),
        data.get("received_by"), data.get("comment"), *trace), commit=False
    )
    err = save_goods_in_documents(db, goods_id, request.files.getlist("documents"))
    if err:
        db.rollback()
        return jsonify({"error": err[0]}), err[1]
    db.commit()
    return jsonify({"goods_in_id": goods_id}), 201


@app.route("/api/goods-in/<int:gid>", methods=["PUT"])
@permission_required("goods_in:write")
def update_goods_in(gid):
    data = request.get_json(force=True) or {}
    if data.get("destination_type") not in ("Works Order", "Stock"):
        return jsonify({"error": "Destination must be 'Works Order' or 'Stock'"}), 400
    if data.get("destination_type") == "Stock" and not data.get("ship_to_id"):
        return jsonify({"error": "Choose a site for stock deliveries"}), 400
    trace, trace_err = goods_in_trace_values(data)
    if trace_err:
        return jsonify({"error": trace_err}), 400
    db = get_db()
    globals.query_db(db, 
        """UPDATE goods_in SET purchase_order_id=?, destination_type=?, ship_to_id=?, received_date=?,
              quantity_status=?, quantity_received=?, received_by=?, comment=?,
              batch_number=?, heat_number=?, cert_status=?, inspection_result=? WHERE goods_in_id=?""",
        (data.get("purchase_order_id"), data.get("destination_type"),
         data.get("ship_to_id") if data.get("destination_type") == "Stock" else None,
         data.get("received_date"), data.get("quantity_status"),
         safe_float(data.get("quantity_received"), None),
         data.get("received_by"), data.get("comment"), *trace, gid), commit=True)
    return jsonify({"ok": True})


@app.route("/api/goods-in/<int:gid>", methods=["DELETE"])
@permission_required("all")
def delete_goods_in(gid):
    db = get_db()
    docs = globals.query_db(db, "SELECT stored_filename FROM goods_in_documents WHERE goods_in_id=?", (gid,), one=False)
    globals.query_db(db, "DELETE FROM goods_in WHERE goods_in_id=?", (gid,), commit=True)
    for d in docs:
        fn = d["stored_filename"]
        if fn and fn.startswith("https://"):
            try:
                blob.delete_blob(fn)
            except Exception:
                pass
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Despatch Notes - the outbound counterpart to Goods In: goods physically
# shipped to the customer against a Works Order, with itemised lines and a
# printable PDF built on the same branding as Quotes and Purchase Orders.
# --------------------------------------------------------------------------
def despatch_note_lines(db, did):
    return globals.query_db(db,
        "SELECT * FROM despatch_note_lines WHERE despatch_note_id=? ORDER BY line_id",
        (did,), one=False)


def save_despatch_note_lines(db, did, lines):
    """Replace-all, mirroring save_purchase_order_lines - deletes any
    existing lines for this despatch note and inserts the new set."""
    globals.query_db(db, "DELETE FROM despatch_note_lines WHERE despatch_note_id=?", (did,), commit=False)
    for ln in lines or []:
        qty = safe_float(ln.get("qty"), 1) or 1
        globals.query_db(db,
            "INSERT INTO despatch_note_lines (despatch_note_id, qty, part_number, description) VALUES (?,?,?,?)",
            (did, qty, ln.get("part_number"), ln.get("description")), commit=False
        )


# A signature pad PNG at a sensible on-screen size comes to a few tens of KB;
# this generously caps the decoded file at 2MB so a malformed or hostile
# data: URL can't be used to fill the disk, without being anywhere close to
# a real signature's size.
MAX_SIGNATURE_BYTES = 2 * 1024 * 1024


@app.route("/api/despatch-notes", methods=["GET"])
@login_required
def list_despatch_notes():
    db = get_db()
    result = globals.query_db(db,
        """SELECT dn.*, wo.job_number, wo.job_name, c.customer_name
           FROM despatch_notes dn
           JOIN works_orders wo ON wo.works_order_id = dn.works_order_id
           JOIN customers c ON c.customer_id = wo.customer_id
           ORDER BY dn.despatch_date DESC, dn.despatch_note_id DESC""", one=False
    )

    # Fetch ALL despatch note lines in ONE query instead of one per note
    all_lines = globals.query_db(db,
        "SELECT * FROM despatch_note_lines ORDER BY despatch_note_id, line_id", one=False)
    lines_by_dn = {}
    for ln in all_lines:
        lines_by_dn.setdefault(ln["despatch_note_id"], []).append(ln)

    for r in result:
        r["lines"] = lines_by_dn.get(r["despatch_note_id"], [])
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
    despatch_number = get_next_despatch_number(db)
    try:
        desp_id = globals.query_db(
            db,
            """INSERT INTO despatch_notes (despatch_number, works_order_id, despatch_date, delivery_address,
                  carrier, despatched_by, notes, status, created_by, created_date)
               VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))""",
            (despatch_number, data.get("works_order_id"), data.get("despatch_date"), data.get("delivery_address"),
             data.get("carrier"), data.get("despatched_by"), data.get("notes"), data.get("status", "Draft"),
             current_user_name()), commit=False
        )
        save_despatch_note_lines(db, desp_id, data.get("lines") or [])
        db.commit()
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400
    return jsonify({"despatch_note_id": desp_id}), 201


@app.route("/api/despatch-notes/<int:did>", methods=["PUT"])
@permission_required("despatch_notes:write")
def update_despatch_note(did):
    data = request.get_json(force=True) or {}
    db = get_db()
    old = globals.query_db(db, "SELECT * FROM despatch_notes WHERE despatch_note_id=?", (did,), one=True)
    log_field_changes(db, "despatch_notes", did, old, {"status": data.get("status")}, ["status"], current_user_name())
    try:
        globals.query_db(db,
            """UPDATE despatch_notes SET works_order_id=?, despatch_date=?, delivery_address=?, carrier=?,
               despatched_by=?, notes=?, status=?, updated_by=?, updated_date=datetime('now')
               WHERE despatch_note_id=?""",
            (data.get("works_order_id"), data.get("despatch_date"), data.get("delivery_address"), data.get("carrier"),
             data.get("despatched_by"), data.get("notes"), data.get("status"), current_user_name(), did), commit=False
        )
        if data.get("lines") is not None:
            save_despatch_note_lines(db, did, data.get("lines"))
        db.commit()
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/despatch-notes/<int:did>/pdf")
@login_required
def get_despatch_note_pdf(did):
    db = get_db()
    dn = globals.query_db(db,
        """SELECT dn.*, wo.job_number, c.customer_name
           FROM despatch_notes dn
           JOIN works_orders wo ON wo.works_order_id = dn.works_order_id
           JOIN customers c ON c.customer_id = wo.customer_id
           WHERE dn.despatch_note_id=?""",
        (did,), one=True
    )
    if not dn:
        return jsonify({"error": "Despatch Note not found"}), 404
    lines = despatch_note_lines(db, did)
    
    signature_path = None
    sig_url = dn.get("signature_filename")
    if sig_url and sig_url.startswith("https://"):
        try:
            blob_data = blob.get_blob(sig_url, access="private")
            signature_path = BytesIO(blob_data.body)
        except Exception:
            signature_path = None
    pdf_bytes = build_despatch_pdf(dn, lines, signature_path=signature_path)
    log_document_access(db, "Despatch Note PDF" + (" (preview)" if request.args.get("preview") == "1" else ""), dn["despatch_number"])
    return pdf_response(pdf_bytes, dn["despatch_number"])


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
    dn = globals.query_db(db, "SELECT * FROM despatch_notes WHERE despatch_note_id=?", (did,), one=True)
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

    old_url = dn.get("signature_filename")
    blob_pathname = f"signatures/{secrets.token_hex(16)}.png"
    
    # Upload PNG directly to Private Vercel Blob store
    result = blob.put_blob(
        blob_pathname,
        png_bytes,
        access="private",
        content_type="image/png"
    )

    try:
        globals.query_db(db,
            """UPDATE despatch_notes SET signature_filename=?, signature_name=?, signature_date=?,
               signature_captured_by=? WHERE despatch_note_id=?""",
            (result.url, name, data.get("date") or datetime.now().strftime("%Y-%m-%d"), current_user_name(), did), commit=True
        )
    except Exception:
        # Clean up the new blob if the database update fails
        try:
            blob.delete_blob(result.url)
        except Exception:
            pass
        raise

    # Delete the previous signature blob from Vercel if re-signing
    if old_url and old_url.startswith("https://"):
        try:
            blob.delete_blob(old_url)
        except Exception:
            pass

    return jsonify({"ok": True})


@app.route("/api/despatch-notes/<int:did>/signature")
@login_required
def get_despatch_note_signature(did):
    db = get_db()
    dn = globals.query_db(db, "SELECT * FROM despatch_notes WHERE despatch_note_id=?", (did,), one=True)
    if not dn or not dn.get("signature_filename"):
        return jsonify({"error": "No signature has been captured for this Despatch Note"}), 404
        
    sig_url = dn["signature_filename"]
    if not sig_url.startswith("https://"):
        return jsonify({"error": "That signature's file is missing from cloud storage - it may have been removed "
                                   "outside the app. Contact an Administrator."}), 404
                                   
    try:
        blob_data = blob.get_blob(sig_url, access="private")
    except Exception:
        return jsonify({"error": "That signature's file is missing from cloud storage - it may have been removed "
                                   "outside the app. Contact an Administrator."}), 404

    log_document_access(db, "Despatch Note Signature", dn["despatch_number"])
    return send_file(
        BytesIO(blob_data.body),
        mimetype="image/png",
        as_attachment=False,
        download_name=f"{dn['despatch_number']}-signature.png"
    )


@app.route("/api/despatch-notes/<int:did>", methods=["DELETE"])
@permission_required("all")
def delete_despatch_note(did):
    db = get_db()
    dn = globals.query_db(db, "SELECT signature_filename FROM despatch_notes WHERE despatch_note_id=?", (did,), one=True)
    globals.query_db(db, "DELETE FROM despatch_note_lines WHERE despatch_note_id=?", (did,), commit=False)
    globals.query_db(db, "DELETE FROM despatch_notes WHERE despatch_note_id=?", (did,), commit=False)
    db.commit()
    
    sig_url = dn.get("signature_filename") if dn else None
    if sig_url and sig_url.startswith("https://"):
        try:
            blob.delete_blob(sig_url)
        except Exception:
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
            src = globals.query_db(db, 
                "SELECT * FROM stock_allocations WHERE stock_item_id=? AND ship_to_id=?",
                (stock_item_id, from_ship_to_id), one=True
            )
        else:
            src = globals.query_db(db, 
                "SELECT * FROM stock_allocations WHERE stock_item_id=? AND works_order_id=?",
                (stock_item_id, from_works_order_id), one=True
            )
        if not src:
            raise ValueError("No stock currently allocated at the selected source")
        if src["quantity"] < quantity:
            raise ValueError(f"Only {src['quantity']:g} available at the selected source")
        remaining = src["quantity"] - quantity
        if remaining <= 0:
            globals.query_db(db, "DELETE FROM stock_allocations WHERE allocation_id=?", (src["allocation_id"],), commit=False)
        else:
            globals.query_db(db, "UPDATE stock_allocations SET quantity=? WHERE allocation_id=?", (remaining, src["allocation_id"]), commit=False)

    if to_ship_to_id:
        dest = globals.query_db(db, 
            "SELECT * FROM stock_allocations WHERE stock_item_id=? AND ship_to_id=?",
            (stock_item_id, to_ship_to_id), one=True
        )
    else:
        dest = globals.query_db(db, 
            "SELECT * FROM stock_allocations WHERE stock_item_id=? AND works_order_id=?",
            (stock_item_id, to_works_order_id), one=True
        )
    if dest:
        globals.query_db(db, "UPDATE stock_allocations SET quantity=quantity+? WHERE allocation_id=?", (quantity, dest["allocation_id"]), commit=False)
    else:
        globals.query_db(db, 
            "INSERT INTO stock_allocations (stock_item_id, quantity, ship_to_id, works_order_id, notes) VALUES (?,?,?,?,?)",
            (stock_item_id, quantity, to_ship_to_id, to_works_order_id, notes), commit=False
        )

    globals.query_db(db,
        """INSERT INTO stock_movements
           (stock_item_id, quantity, from_ship_to_id, from_works_order_id, to_ship_to_id, to_works_order_id, moved_by, notes)
           VALUES (?,?,?,?,?,?,?,?)""",
        (stock_item_id, quantity, from_ship_to_id, from_works_order_id, to_ship_to_id, to_works_order_id, moved_by, notes), commit=False
    )


def stock_item_with_allocations(db, item_row):
    item = dict(item_row)
    allocations = globals.query_db(db, 
        """SELECT sa.*, sta.label AS ship_to_label, wo.job_number, wo.job_name
           FROM stock_allocations sa
           LEFT JOIN ship_to_addresses sta ON sta.ship_to_id = sa.ship_to_id
           LEFT JOIN works_orders wo ON wo.works_order_id = sa.works_order_id
           WHERE sa.stock_item_id=? ORDER BY sa.allocated_date DESC""",
        (item["stock_item_id"],), one=False)
    item["allocations"] = rows_to_list(allocations)
    item["total_quantity"] = sum(a["quantity"] for a in item["allocations"])
    item["total_value"] = round(item["total_quantity"] * item["purchase_cost"], 2)
    item["total_sale_value"] = round(item["total_quantity"] * item["sale_price"], 2)
    return item
@app.route("/api/stock-items", methods=["GET"])
@login_required
def list_stock_items():
    db = get_db()
    rows = globals.query_db(db,
        """SELECT si.*, s.supplier_name FROM stock_items si
           LEFT JOIN suppliers s ON s.supplier_id = si.supplier_id
           ORDER BY si.part_number""", one=False
    )

    # Fetch ALL stock allocations in ONE query instead of one per item
    all_allocs = globals.query_db(db,
        """SELECT sa.*, sta.label AS ship_to_label, wo.job_number, wo.job_name
           FROM stock_allocations sa
           LEFT JOIN ship_to_addresses sta ON sta.ship_to_id = sa.ship_to_id
           LEFT JOIN works_orders wo ON wo.works_order_id = sa.works_order_id
           ORDER BY sa.allocated_date DESC""", one=False
    )
    allocs_by_item = {}
    for a in all_allocs:
        allocs_by_item.setdefault(a["stock_item_id"], []).append(a)

    result = []
    for r in rows:
        item = dict(r)
        item["allocations"] = allocs_by_item.get(item["stock_item_id"], [])
        item["total_quantity"] = sum(a["quantity"] for a in item["allocations"])
        item["total_value"] = round(item["total_quantity"] * item["purchase_cost"], 2)
        item["total_sale_value"] = round(item["total_quantity"] * item["sale_price"], 2)
        result.append(item)
    return jsonify(result)


@app.route("/api/stock-items/<int:item_id>", methods=["GET"])
@login_required
def get_stock_item(item_id):
    db = get_db()
    row = globals.query_db(db, 
        """SELECT si.*, s.supplier_name FROM stock_items si
           LEFT JOIN suppliers s ON s.supplier_id = si.supplier_id
           WHERE si.stock_item_id=?""",
        (item_id,), one=True
    )
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
        item_id = globals.query_db(db, 
            """INSERT INTO stock_items (part_number, description, unit, purchase_cost, sale_price, reorder_level, supplier_id, notes)
               VALUES (?,?,?,?,?,?,?,?)""",
            (part_number, data.get("description"), data.get("unit") or "each",
             safe_float(data.get("purchase_cost")), safe_float(data.get("sale_price")),
             safe_float(data.get("reorder_level")), data.get("supplier_id") or None, data.get("notes")), commit=False
        )
        if initial_quantity > 0:
            move_stock(db, item_id, initial_quantity, None, None, initial_ship_to_id, None,
                       "Initial stock on creation", session.get("full_name"))
        db.commit()
    except (Exception, ValueError) as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400
    return jsonify({"stock_item_id": item_id}), 201


@app.route("/api/stock-items/<int:item_id>", methods=["PUT"])
@permission_required("inventory:write")
def update_stock_item(item_id):
    data = request.get_json(force=True) or {}
    part_number = (data.get("part_number") or "").strip()
    if not part_number:
        return jsonify({"error": "Part number is required"}), 400
    db = get_db()
    try:
        globals.query_db(db,
            """UPDATE stock_items SET part_number=?, description=?, unit=?, purchase_cost=?, sale_price=?,
               reorder_level=?, supplier_id=?, notes=? WHERE stock_item_id=?""",
            (part_number, data.get("description"), data.get("unit") or "each",
             safe_float(data.get("purchase_cost")), safe_float(data.get("sale_price")),
             safe_float(data.get("reorder_level")), data.get("supplier_id") or None, data.get("notes"), item_id), commit=True
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/stock-items/<int:item_id>", methods=["DELETE"])
@permission_required("all")
def delete_stock_item(item_id):
    db = get_db()
    globals.query_db(db, "DELETE FROM stock_items WHERE stock_item_id=?", (item_id,), commit=True)
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
        all_id = globals.query_db(db, 
            """INSERT INTO stock_allocations (stock_item_id, quantity, ship_to_id, works_order_id, notes)
               VALUES (?,?,?,?,?)""",
            (item_id, quantity, ship_to_id, works_order_id, data.get("notes")), commit=True
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"allocation_id": all_id}), 201


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
        globals.query_db(db,
            """UPDATE stock_allocations SET quantity=?, ship_to_id=?, works_order_id=?, notes=?
               WHERE allocation_id=?""",
            (quantity, ship_to_id, works_order_id, data.get("notes"), allocation_id), commit=True
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/stock-allocations/<int:allocation_id>", methods=["DELETE"])
@permission_required("inventory:write")
def delete_stock_allocation(allocation_id):
    db = get_db()
    globals.query_db(db, "DELETE FROM stock_allocations WHERE allocation_id=?", (allocation_id,), commit=True)
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
    except (ValueError, Exception) as e:
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
    rows = globals.query_db(db, sql, params, one=False)
    return jsonify(rows_to_list(rows))


# --------------------------------------------------------------------------
# Supplier Invoices
# --------------------------------------------------------------------------
@app.route("/api/supplier-invoices", methods=["GET"])
@login_required
def list_supplier_invoices():
    db = get_db()
    rows = globals.query_db(db, 
        """SELECT si.*, s.supplier_name, wo.job_number, po.po_number, po.po_value
           FROM supplier_invoices si
           JOIN suppliers s ON s.supplier_id = si.supplier_id
           JOIN works_orders wo ON wo.works_order_id = si.works_order_id
           JOIN purchase_orders po ON po.purchase_order_id = si.purchase_order_id
           ORDER BY si.invoice_date DESC""", one=False
    )
    return jsonify(rows_to_list(rows))


@app.route("/api/supplier-invoices", methods=["POST"])
@permission_required("supplier_invoices:write")
def create_supplier_invoice():
    data = request.get_json(force=True) or {}
    db = get_db()
    po = globals.query_db(db, "SELECT * FROM purchase_orders WHERE purchase_order_id=?", (data.get("purchase_order_id"),), one=True)
    if not po:
        return jsonify({"error": "Purchase order not found"}), 400
    sinv_id = globals.query_db(db, 
        """INSERT INTO supplier_invoices (invoice_number, purchase_order_id, works_order_id, supplier_id, invoice_date, invoice_amount, status, created_by, created_date)
           VALUES (?,?,?,?,?,?,?,?,datetime('now'))""",
        (data.get("invoice_number"), po["purchase_order_id"], po["works_order_id"], po["supplier_id"],
         data.get("invoice_date"), safe_float(data.get("invoice_amount")), data.get("status", "Pending"), current_user_name()), commit=True
    )
    # Validate against PO value and surface a warning (record is still saved)

    row = globals.query_db(db, 
        "SELECT COALESCE(SUM(invoice_amount),0) AS total FROM supplier_invoices WHERE purchase_order_id=? AND status != 'Cancelled'",
        (po["purchase_order_id"],), one=True
    )
    existing_total = row['total'] if row else 0

    warning = None
    if existing_total > po["po_value"]:
        warning = f"Total invoiced (£{existing_total:,.2f}) now exceeds PO value (£{po['po_value']:,.2f}) - approval required"
    return jsonify({"supplier_invoice_id": sinv_id, "warning": warning}), 201


@app.route("/api/supplier-invoices/<int:iid>", methods=["PUT"])
@permission_required("supplier_invoices:write")
def update_supplier_invoice(iid):
    data = request.get_json(force=True) or {}
    db = get_db()
    old = globals.query_db(db, "SELECT * FROM supplier_invoices WHERE supplier_invoice_id=?", (iid,), one=True)
    log_field_changes(db, "supplier_invoices", iid, old,
                       {"status": data.get("status"), "invoice_amount": safe_float(data.get("invoice_amount"))},
                       ["status", "invoice_amount"], current_user_name())
    globals.query_db(db,
        """UPDATE supplier_invoices SET invoice_number=?, invoice_date=?, invoice_amount=?, status=?,
           updated_by=?, updated_date=datetime('now') WHERE supplier_invoice_id=?""",
        (data.get("invoice_number"), data.get("invoice_date"), safe_float(data.get("invoice_amount")), data.get("status"), current_user_name(), iid), commit=True
    )
    return jsonify({"ok": True})


@app.route("/api/supplier-invoices/<int:iid>", methods=["DELETE"])
@permission_required("all")
def delete_supplier_invoice(iid):
    db = get_db()
    globals.query_db(db, "DELETE FROM supplier_invoices WHERE supplier_invoice_id=?", (iid,), commit=True)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Labour Entries
# --------------------------------------------------------------------------
@app.route("/api/labour-entries", methods=["GET"])
@login_required
def list_labour_entries():
    db = get_db()
    rows = globals.query_db(db, 
        """SELECT le.*, wo.job_number FROM labour_entries le
           JOIN works_orders wo ON wo.works_order_id = le.works_order_id
           ORDER BY le.work_date DESC""", one=False
    )
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
    quoted = globals.query_db(db, 
        """SELECT wo.works_order_id, wo.job_number, wo.job_name,
                  COALESCE(SUM(qll.standard_hours + qll.ot_hours), 0) AS quoted_hours,
                  COALESCE(SUM(qll.standard_hours * qll.standard_rate + qll.ot_hours * qll.ot_rate), 0) AS quoted_cost
           FROM works_orders wo
           JOIN quotes q ON q.works_order_id = wo.works_order_id
           JOIN quote_labour_lines qll ON qll.quote_id = q.quote_id
           GROUP BY wo.works_order_id""", one=False
    )
    booked = globals.query_db(
        """SELECT wo.works_order_id, wo.job_number, wo.job_name,
                  COALESCE(SUM(le.hours_worked), 0) AS booked_hours,
                  COALESCE(SUM(le.labour_cost), 0) AS booked_cost
           FROM works_orders wo
           JOIN labour_entries le ON le.works_order_id = wo.works_order_id
           GROUP BY wo.works_order_id""", one=False
    )

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
    lab_id = globals.query_db(db, 
        """INSERT INTO labour_entries (works_order_id, employee_name, work_date, hours_worked, hourly_rate, labour_cost, created_by, created_date)
           VALUES (?,?,?,?,?,?,?,datetime('now'))""",
        (data.get("works_order_id"), data.get("employee_name"), data.get("work_date"), hours, rate, cost, current_user_name()), commit=True
    )
    return jsonify({"labour_entry_id": lab_id, "labour_cost": cost}), 201


@app.route("/api/labour-entries/<int:lid>", methods=["PUT"])
@permission_required("labour_entries:write")
def update_labour_entry(lid):
    data = request.get_json(force=True) or {}
    hours = safe_float(data.get("hours_worked"))
    rate = safe_float(data.get("hourly_rate"))
    cost = round(hours * rate, 2)
    db = get_db()
    old = globals.query_db(db, "SELECT * FROM labour_entries WHERE labour_entry_id=?", (lid,), one=True)
    log_field_changes(db, "labour_entries", lid, old,
                       {"hours_worked": hours, "hourly_rate": rate, "labour_cost": cost},
                       ["hours_worked", "hourly_rate", "labour_cost"], current_user_name())
    globals.query_db(db,
        """UPDATE labour_entries SET employee_name=?, work_date=?, hours_worked=?, hourly_rate=?, labour_cost=?,
           updated_by=?, updated_date=datetime('now') WHERE labour_entry_id=?""",
        (data.get("employee_name"), data.get("work_date"), hours, rate, cost, current_user_name(), lid), commit=True
    )
    return jsonify({"ok": True, "labour_cost": cost})


@app.route("/api/labour-entries/<int:lid>", methods=["DELETE"])
@permission_required("all")
def delete_labour_entry(lid):
    db = get_db()
    globals.query_db(db, "DELETE FROM labour_entries WHERE labour_entry_id=?", (lid,), commit=True)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Customer Invoices
# --------------------------------------------------------------------------
@app.route("/api/customer-invoices", methods=["GET"])
@login_required
def list_customer_invoices():
    db = get_db()
    rows = globals.query_db(db, 
        """SELECT ci.*, wo.job_number FROM customer_invoices ci
           JOIN works_orders wo ON wo.works_order_id = ci.works_order_id
           ORDER BY ci.invoice_date DESC""", one=False
    )
    return jsonify(rows_to_list(rows))


@app.route("/api/customer-invoices", methods=["POST"])
@permission_required("customer_invoices:write")
def create_customer_invoice():
    data = request.get_json(force=True) or {}
    db = get_db()
    cinv_id = globals.query_db(db, 
        """INSERT INTO customer_invoices (works_order_id, invoice_number, invoice_date, invoice_amount, status, created_by, created_date)
           VALUES (?,?,?,?,?,?,datetime('now'))""",
        (data.get("works_order_id"), data.get("invoice_number"), data.get("invoice_date"),
         safe_float(data.get("invoice_amount")), data.get("status", "Draft"), current_user_name()), commit=True
    )
    return jsonify({"customer_invoice_id": cinv_id}), 201


@app.route("/api/customer-invoices/<int:iid>", methods=["PUT"])
@permission_required("customer_invoices:write")
def update_customer_invoice(iid):
    data = request.get_json(force=True) or {}
    db = get_db()
    old = globals.query_db(db, "SELECT * FROM customer_invoices WHERE customer_invoice_id=?", (iid,), one=True)
    log_field_changes(db, "customer_invoices", iid, old,
                       {"status": data.get("status"), "invoice_amount": safe_float(data.get("invoice_amount"))},
                       ["status", "invoice_amount"], current_user_name())
    globals.query_db(db,
        """UPDATE customer_invoices SET invoice_number=?, invoice_date=?, invoice_amount=?, status=?,
           updated_by=?, updated_date=datetime('now') WHERE customer_invoice_id=?""",
        (data.get("invoice_number"), data.get("invoice_date"), safe_float(data.get("invoice_amount")), data.get("status"), current_user_name(), iid),
        commit=True
    )
    return jsonify({"ok": True})


@app.route("/api/customer-invoices/<int:iid>", methods=["DELETE"])
@permission_required("all")
def delete_customer_invoice(iid):
    db = get_db()
    globals.query_db(db, "DELETE FROM customer_invoices WHERE customer_invoice_id=?", (iid,), commit=True)
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
        jobs = load_all_jobs_with_metrics(db)

    accepted = {r["alert_key"] for r in globals.query_db(db, "SELECT alert_key FROM alert_acceptances", one=False)}

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
    open_quotes = globals.query_db(db, 
        f"""SELECT q.quote_id, q.quote_number, q.valid_until, q.status, c.customer_name FROM quotes q
            LEFT JOIN customers c ON c.customer_id = q.customer_id
            WHERE q.status IN ({",".join("?" for _ in QUOTE_OPEN_STATUSES)})
              AND q.valid_until IS NOT NULL AND q.valid_until != ''""",
        tuple(QUOTE_OPEN_STATUSES), one=False
    )
    for q in open_quotes:
        if is_past_validity(q["valid_until"], q["status"]):
            msg = f"Valid until {q['valid_until'][:10]} has passed - review or re-quote before it's accepted at this price."
            all_alerts.append({
                "level": "amber", "type": "Quote Past Validity",
                "job_number": q["quote_number"], "job_name": q["customer_name"] or "",
                "message": msg, "quote_id": q["quote_id"],
                "alert_key": alert_key(q["quote_number"], "Quote Past Validity", msg),
            })
    all_alerts.extend(enquiry_alerts(db))
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
    globals.query_db(db,
        "INSERT OR IGNORE INTO alert_acceptances (alert_key, job_number, alert_type, message, reason, accepted_by) VALUES (?,?,?,?,?,?)",
        (a["alert_key"], a["job_number"], a["type"], a["message"], reason, current_user_name()),
        commit=True
    )
    return jsonify({"ok": True})


@app.route("/api/alerts/accepted")
@login_required
def list_accepted_alerts():
    db = get_db()
    return jsonify(rows_to_list(globals.query_db(db, "SELECT * FROM alert_acceptances ORDER BY acceptance_id DESC", one=False)))


@app.route("/api/alerts/accepted/<int:aid>", methods=["DELETE"])
@permission_required("works_orders:write")
def undo_accepted_alert(aid):
    db = get_db()
    globals.query_db(db, "DELETE FROM alert_acceptances WHERE acceptance_id=?", (aid,), commit=True)
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


def load_all_jobs_with_metrics(db, rows=None):
    """Computes financial metrics and alerts for a list of Works Orders (or
    all non-placeholder Works Orders if `rows` is None) in 6 bulk queries
    instead of 6 queries per job."""
    if rows is None:
        rows = globals.query_db(db,
            f"""SELECT wo.*, c.customer_name FROM works_orders wo
                JOIN customers c ON c.customer_id = wo.customer_id WHERE {PLACEHOLDER_JOB_SQL}""", one=False)
    if not rows:
        return []

    rev_map = {
        r["works_order_id"]: r["total"]
        for r in globals.query_db(db,
            "SELECT works_order_id, COALESCE(SUM(invoice_amount),0) AS total "
            "FROM customer_invoices WHERE status != 'Cancelled' GROUP BY works_order_id", one=False)
    }
    sup_map = {
        r["works_order_id"]: r["total"]
        for r in globals.query_db(db,
            "SELECT works_order_id, COALESCE(SUM(invoice_amount),0) AS total "
            "FROM supplier_invoices WHERE status != 'Cancelled' GROUP BY works_order_id", one=False)
    }
    lab_map = {
        r["works_order_id"]: r["total"]
        for r in globals.query_db(db,
            "SELECT works_order_id, COALESCE(SUM(labour_cost),0) AS total "
            "FROM labour_entries GROUP BY works_order_id", one=False)
    }
    po_map = {
        r["works_order_id"]: r["total"]
        for r in globals.query_db(db,
            "SELECT works_order_id, COALESCE(SUM(po_value),0) AS total "
            "FROM purchase_orders WHERE status != 'Cancelled' AND is_historic=0 GROUP BY works_order_id", one=False)
    }
    sto_map = {
        r["works_order_id"]: r["total"]
        for r in globals.query_db(db,
            """SELECT sa.works_order_id, COALESCE(SUM(sa.quantity * si.purchase_cost), 0) AS total
               FROM stock_allocations sa JOIN stock_items si ON si.stock_item_id = sa.stock_item_id
               WHERE sa.works_order_id IS NOT NULL GROUP BY sa.works_order_id""", one=False)
    }

    over_invoiced_map = {}
    over_invoiced_rows = globals.query_db(db,
        """SELECT po.works_order_id, po.po_number, po.po_value, COALESCE(SUM(si.invoice_amount),0) AS invoiced
           FROM purchase_orders po
           LEFT JOIN supplier_invoices si ON si.purchase_order_id = po.purchase_order_id AND si.status != 'Cancelled'
           WHERE po.status != 'Cancelled' AND po.is_historic = 0
           GROUP BY po.purchase_order_id
           HAVING invoiced > po.po_value""", one=False)
    for po in over_invoiced_rows:
        over_invoiced_map.setdefault(po["works_order_id"], []).append(po)

    jobs = []
    for wo in rows:
        wo_id = wo["works_order_id"]
        revenue = rev_map.get(wo_id, 0)
        supplier_invoice_total = sup_map.get(wo_id, 0)
        labour_total = lab_map.get(wo_id, 0)
        po_total = po_map.get(wo_id, 0)
        stock_cost = sto_map.get(wo_id, 0)

        actual_cost = supplier_invoice_total + labour_total + stock_cost
        outstanding_commitment = max(0.0, po_total - supplier_invoice_total)
        forecast_cost = actual_cost + outstanding_commitment
        profit = revenue - actual_cost
        forecast_profit = revenue - forecast_cost
        margin_pct = (profit / revenue * 100) if revenue else 0.0
        forecast_margin_pct = (forecast_profit / revenue * 100) if revenue else 0.0

        margin_target = get_margin_target(db, wo["contract_code"] if "contract_code" in wo else None)

        alerts = []
        if actual_cost > wo["budget_cost"] and wo["budget_cost"] > 0:
            is_estimated = bool(wo["budget_is_estimated"]) if "budget_is_estimated" in wo else False
            if is_estimated:
                alerts.append({
                    "level": "amber", "type": "Cost Above Estimated Target",
                    "message": f"Actual cost (£{actual_cost:,.2f}) is above the estimated target of "
                               f"£{wo['budget_cost']:,.2f} (35% of invoice value - no confirmed budget was set for this job)"
                })
            else:
                alerts.append({
                    "level": "red", "type": "Budget Exceeded",
                    "message": f"Actual cost (£{actual_cost:,.2f}) exceeds budget (£{wo['budget_cost']:,.2f})"
                })
        if revenue and forecast_margin_pct < margin_target:
            alerts.append({
                "level": "amber", "type": "Margin Risk",
                "message": f"Forecast margin {forecast_margin_pct:.1f}% is below the {margin_target:.0f}% target"
            })

        for po in over_invoiced_map.get(wo_id, []):
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

        jobs.append({
            **wo,
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
        })
    return jobs


@app.route("/api/dashboard")
@login_required
def dashboard():
    db = get_db()
    jobs = load_all_jobs_with_metrics(db)

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
        chase = [e for e in enquiry_rows(db, "WHERE e.stage IN (%s)" % ",".join("?" for _ in ENQUIRY_OPEN_STAGES), ENQUIRY_OPEN_STAGES)
                 if e["follow_up_state"] in ("overdue", "today")]
        chase.sort(key=lambda e: e["next_follow_up"] or "")
        late["enquiry_chase_count"] = len(chase)
        late["enquiry_chase"] = [{k: e[k] for k in ("enquiry_id", "enquiry_number", "display_name", "description", "owner",
                                  "stage", "next_follow_up", "next_action", "estimated_value", "follow_up_state", "days_overdue")}
                                 for e in chase[:8]]
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

    stock_row = globals.query_db(db, 
        """SELECT COALESCE(SUM(sa.quantity * si.purchase_cost), 0) AS total
        FROM stock_allocations sa JOIN stock_items si ON si.stock_item_id = sa.stock_item_id""", one=True)
    total_stock_value = stock_row["total"] if stock_row else 0

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
    monthly_revenue_rows = globals.query_db(db, 
        """SELECT strftime('%Y-%m', invoice_date) AS ym, COALESCE(SUM(invoice_amount),0) AS total
           FROM customer_invoices WHERE status != 'Cancelled' AND strftime('%Y', invoice_date) = ?
           GROUP BY ym""", (str(current_year),), one=False)
    monthly_supplier_cost = globals.query_db(db, 
        """SELECT strftime('%Y-%m', invoice_date) AS ym, COALESCE(SUM(invoice_amount),0) AS total
           FROM supplier_invoices WHERE status != 'Cancelled' AND strftime('%Y', invoice_date) = ?
           GROUP BY ym""", (str(current_year),), one=False)
    monthly_labour_cost = globals.query_db(db, 
        """SELECT strftime('%Y-%m', work_date) AS ym, COALESCE(SUM(labour_cost),0) AS total
           FROM labour_entries WHERE strftime('%Y', work_date) = ?
           GROUP BY ym""", (str(current_year),), one=False)

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
        "on_hold_works_orders": sum(1 for j in jobs if j["status"] == "On Hold"),
        "on_hold_value": round(sum(j["contract_value"] or 0 for j in jobs if j["status"] == "On Hold"), 2),
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
# "Invoiced" for the Awaiting Invoicing report: the job has a customer invoice that really went out.
# Cancelled and Draft invoices never count. Paid and Overdue always count. A Sent invoice counts only if
# somebody raised it in the app - an invoice that came in from the Order Book with status Sent is the
# Order Book's "delivered, Date Invoiced still blank", i.e. NOT yet invoiced.
INVOICED_SQL = """EXISTS (SELECT 1 FROM customer_invoices ci WHERE ci.works_order_id = wo.works_order_id
                   AND ci.status != 'Cancelled' AND ci.status != 'Draft'
                   AND (ci.status IN ('Paid','Overdue') OR COALESCE(ci.created_by,'') != ''))"""


def build_awaiting_invoicing(db, args):
    customer = args.get("customer", "").strip()
    location = args.get("location", "").strip()
    owner = args.get("owner", "").strip()
    code = args.get("contract_code", "").strip()
    base = f"""SELECT wo.*, c.customer_name
               FROM works_orders wo
               JOIN customers c ON c.customer_id = wo.customer_id
               LEFT JOIN (
                   SELECT DISTINCT works_order_id
                   FROM customer_invoices
                   WHERE status != 'Cancelled' AND status != 'Draft'
                     AND (status IN ('Paid','Overdue') OR COALESCE(created_by,'') != '')
               ) inv ON inv.works_order_id = wo.works_order_id
               WHERE {PLACEHOLDER_JOB_SQL}
                 AND wo.status = 'Completed'
                 AND wo.contract_value > 0
                 AND inv.works_order_id IS NULL"""
    all_rows = globals.query_db(db, base, one=False)
    today = date.today()
    rows = []
    for r in all_rows:
        if customer and r["customer_name"] != customer: continue
        if location and (r["location"] or "") != location: continue
        if owner and (r["project_manager"] or "") != owner: continue
        if code and (r["contract_code"] or "") != code: continue
        days = None
        try:
            days = (today - datetime.strptime((r["end_date"] or "")[:10], "%Y-%m-%d").date()).days
        except ValueError:
            pass
        rows.append({"works_order_id": r["works_order_id"], "job_number": r["job_number"], "job_name": r["job_name"],
                     "customer_name": r["customer_name"], "location": r["location"], "project_manager": r["project_manager"],
                     "contract_code": r["contract_code"], "end_date": r["end_date"], "days_waiting": days,
                     "customer_po": r["customer_po"], "customer_po_line": r["customer_po_line"],
                     "po_value": round(r["contract_value"] or 0, 2)})
    rows.sort(key=lambda x: (x["days_waiting"] is None, -(x["days_waiting"] or 0)))
    aged = [x["days_waiting"] for x in rows if x["days_waiting"] is not None]
    return {
        "rows": rows,
        "totals": {"jobs": len(rows), "value": round(sum(x["po_value"] for x in rows), 2),
                   "oldest_days": max(aged) if aged else 0,
                   "over_30": sum(1 for d in aged if d > 30),
                   "avg_days": round(sum(aged) / len(aged)) if aged else 0},
        # filter lists come from every job waiting, not just the filtered ones, so one filter never shrinks another
        "customers": sorted({r["customer_name"] for r in all_rows}),
        "locations": sorted({r["location"] for r in all_rows if r["location"]}),
        "owners": sorted({r["project_manager"] for r in all_rows if r["project_manager"]}),
        "contract_codes": sorted({r["contract_code"] for r in all_rows if r["contract_code"]}),
    }


@app.route("/api/reporting/awaiting-invoicing")
@login_required
def reporting_awaiting_invoicing():
    return jsonify(build_awaiting_invoicing(get_db(), request.args))


@app.route("/api/reporting/awaiting-invoicing/export")
@login_required
def reporting_awaiting_invoicing_export():
    db = get_db()
    data = build_awaiting_invoicing(db, request.args)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Job", "Job Name", "Customer / Site", "Location", "Owner", "Contract Code", "Customer PO", "Line", "End Date", "Days Waiting", "Purchase Order Value"])
    for r in data["rows"]:
        writer.writerow([r["job_number"], r["job_name"], r["customer_name"], r["location"] or "", r["project_manager"] or "",
                         r["contract_code"] or "", r["customer_po"] or "", r["customer_po_line"] or "", r["end_date"] or "",
                         "" if r["days_waiting"] is None else r["days_waiting"], f"{r['po_value']:.2f}"])
    writer.writerow(["Total", "", "", "", "", "", "", "", "", "", f"{data['totals']['value']:.2f}"])
    log_document_access(db, "Reporting CSV export", f"Awaiting Invoicing, {data['totals']['jobs']} job(s)")
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=jobs_awaiting_invoicing.csv"})


# Unpaid customer invoices, aged from the invoice date. An invoice counts as unpaid when it is Overdue, or Sent
# and raised in the app (a Sent invoice that arrived from the Order Book is only a placeholder - see INVOICED_SQL).
def build_unpaid_invoices(db, args):
    customer = args.get("customer", "").strip()
    location = args.get("location", "").strip()
    owner = args.get("owner", "").strip()
    raw = globals.query_db(db, 
        f"""SELECT ci.*, wo.job_number, wo.job_name, wo.location, wo.project_manager, c.customer_name,
                   c.payment_terms_days, c.payment_terms_eow
            FROM customer_invoices ci
            JOIN works_orders wo ON wo.works_order_id = ci.works_order_id
            JOIN customers c ON c.customer_id = wo.customer_id
            WHERE (ci.status = 'Overdue' OR (ci.status = 'Sent' AND COALESCE(ci.created_by,'') != ''))""", one=False)
    today = date.today()
    items = []
    for r in raw:
        if customer and r["customer_name"] != customer: continue
        if location and (r["location"] or "") != location: continue
        if owner and (r["project_manager"] or "") != owner: continue
        try:
            age = (today - datetime.strptime((r["invoice_date"] or "")[:10], "%Y-%m-%d").date()).days
        except ValueError:
            age = None
        days, eow = customer_terms(r["customer_name"], r["payment_terms_days"], r["payment_terms_eow"])
        due = None
        if r["due_date"]:
            try:
                due = datetime.strptime(r["due_date"][:10], "%Y-%m-%d").date()
            except ValueError:
                due = None
        if due is None:
            due = compute_due_date(r["invoice_date"], days, eow)
        days_overdue = (today - due).days if due and today > due else 0
        amount = round(r["invoice_amount"] or 0, 2)
        items.append({"customer_invoice_id": r["customer_invoice_id"], "works_order_id": r["works_order_id"],
                      "invoice_number": r["invoice_number"], "job_number": r["job_number"], "job_name": r["job_name"],
                      "customer_name": r["customer_name"], "location": r["location"], "project_manager": r["project_manager"],
                      "invoice_date": r["invoice_date"], "due_date": due.isoformat() if due else None,
                      "terms": terms_text(days, eow), "terms_set": days is not None or bool(r["due_date"]),
                      "age_days": age, "days_overdue": days_overdue, "overdue": days_overdue > 0, "amount": amount,
                      "band": "unknown" if age is None else ("0-30" if age <= 30 else "31-60" if age <= 60 else "over-60")})
    items.sort(key=lambda x: (x["age_days"] is None, -(x["age_days"] or 0)))
    by = {}
    for x in items:
        c = by.setdefault(x["customer_name"], {"customer_name": x["customer_name"], "b0_30": 0.0, "b31_60": 0.0, "over_60": 0.0,
                                               "total": 0.0, "overdue": 0.0, "invoices": 0, "oldest_days": 0,
                                               "terms": x["terms"]})
        key = {"0-30": "b0_30", "31-60": "b31_60", "over-60": "over_60"}.get(x["band"])
        if key: c[key] += x["amount"]
        c["total"] += x["amount"]
        c["invoices"] += 1
        if x["overdue"]: c["overdue"] += x["amount"]
        c["oldest_days"] = max(c["oldest_days"], x["age_days"] or 0)
    customers = sorted(by.values(), key=lambda c: -c["total"])
    for c in customers:
        for k in ("b0_30", "b31_60", "over_60", "total", "overdue"): c[k] = round(c[k], 2)
    total = round(sum(x["amount"] for x in items), 2)
    overdue = round(sum(x["amount"] for x in items if x["overdue"]), 2)
    return {
        "invoices": items, "customers": customers,
        "totals": {"count": len(items), "total": total, "overdue": overdue, "not_yet_due": round(total - overdue, 2),
                   "over_60": round(sum(x["amount"] for x in items if x["band"] == "over-60"), 2),
                   "terms_not_set": sum(1 for x in items if not x["terms_set"])},
        "customer_list": sorted({r["customer_name"] for r in raw}),
        "locations": sorted({r["location"] for r in raw if r["location"]}),
        "owners": sorted({r["project_manager"] for r in raw if r["project_manager"]}),
    }


@app.route("/api/reporting/unpaid-invoices")
@login_required
def reporting_unpaid_invoices():
    return jsonify(build_unpaid_invoices(get_db(), request.args))


@app.route("/api/reporting/unpaid-invoices/export")
@login_required
def reporting_unpaid_invoices_export():
    db = get_db()
    data = build_unpaid_invoices(db, request.args)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Invoice", "Job", "Customer / Site", "Location", "Owner", "Invoice Date", "Terms", "Due Date",
                     "Age (days)", "Days Overdue", "Amount"])
    for x in data["invoices"]:
        writer.writerow([x["invoice_number"], x["job_number"], x["customer_name"], x["location"] or "", x["project_manager"] or "",
                         x["invoice_date"] or "", x["terms"], x["due_date"] or "", "" if x["age_days"] is None else x["age_days"],
                         x["days_overdue"], f"{x['amount']:.2f}"])
    writer.writerow(["Total", "", "", "", "", "", "", "", "", "", f"{data['totals']['total']:.2f}"])
    log_document_access(db, "Reporting CSV export", f"Unpaid Invoices, {data['totals']['count']} invoice(s)")
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=unpaid_invoices.csv"})


# Supplier deliveries. A live PO (Issued/Approved, not historic) with a Date Required in the past and no Full
# goods-in receipt is overdue; a Partial receipt still leaves it outstanding. Performance looks at POs that had a
# Date Required and have been received in full: on time if the Full receipt date is on or before Date Required.
def build_overdue_deliveries(db, args):
    supplier = args.get("supplier", "").strip()
    job = args.get("job", "").strip()
    raised_by = args.get("raised_by", "").strip()
    today = date.today()
    pos = globals.query_db(db, 
        """SELECT po.*, s.supplier_name, wo.job_number, wo.job_name,
                  (SELECT MIN(g.received_date) FROM goods_in g WHERE g.purchase_order_id = po.purchase_order_id
                     AND g.quantity_status = 'Full') AS full_date,
                  (SELECT MAX(g.received_date) FROM goods_in g WHERE g.purchase_order_id = po.purchase_order_id) AS last_date
           FROM purchase_orders po
           JOIN suppliers s ON s.supplier_id = po.supplier_id
           LEFT JOIN works_orders wo ON wo.works_order_id = po.works_order_id
           WHERE po.is_historic = 0 AND po.status != 'Cancelled' AND COALESCE(po.date_required,'') != ''""", one=False)

    def d(v):
        try:
            return datetime.strptime((v or "")[:10], "%Y-%m-%d").date()
        except ValueError:
            return None

    overdue, perf = [], {}
    for r in pos:
        req = d(r["date_required"])
        if req is None: continue
        if supplier and r["supplier_name"] != supplier: continue
        if job and (r["job_number"] or "") != job: continue
        if raised_by and (r["raised_by"] or "") != raised_by: continue
        if r["full_date"]:
            got = d(r["full_date"])
            p = perf.setdefault(r["supplier_name"], {"supplier_name": r["supplier_name"], "received": 0, "on_time": 0, "late_days": 0, "late": 0})
            p["received"] += 1
            late = (got - req).days if got else 0
            if late <= 0: p["on_time"] += 1
            else:
                p["late"] += 1
                p["late_days"] += late
        elif r["status"] in ("Issued", "Approved") and req < today:
            overdue.append({"purchase_order_id": r["purchase_order_id"], "po_number": r["po_number"], "supplier_name": r["supplier_name"],
                            "job_number": r["job_number"], "job_name": r["job_name"], "description": r["description"],
                            "raised_by": r["raised_by"], "date_required": r["date_required"],
                            "days_late": (today - req).days, "po_value": round(r["po_value"] or 0, 2),
                            "part_received": bool(r["last_date"])})
    overdue.sort(key=lambda x: -x["days_late"])
    perf_rows = []
    for p in perf.values():
        perf_rows.append({"supplier_name": p["supplier_name"], "received": p["received"], "on_time": p["on_time"],
                          "on_time_pct": round(100 * p["on_time"] / p["received"]),
                          "avg_days_late": round(p["late_days"] / p["late"], 1) if p["late"] else 0})
    perf_rows.sort(key=lambda x: (x["on_time_pct"], -x["received"]))
    all_names = lambda key: sorted({(r[key] or "") for r in pos if r[key]})
    return {
        "overdue": overdue, "performance": perf_rows,
        "totals": {"count": len(overdue), "value": round(sum(x["po_value"] for x in overdue), 2),
                   "over_14": sum(1 for x in overdue if x["days_late"] > 14),
                   "suppliers": len({x["supplier_name"] for x in overdue})},
        "suppliers": all_names("supplier_name"), "jobs": all_names("job_number"), "raised_by_list": all_names("raised_by"),
    }


@app.route("/api/reporting/overdue-deliveries")
@login_required
def reporting_overdue_deliveries():
    return jsonify(build_overdue_deliveries(get_db(), request.args))


@app.route("/api/reporting/overdue-deliveries/export")
@login_required
def reporting_overdue_deliveries_export():
    db = get_db()
    data = build_overdue_deliveries(db, request.args)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["PO", "Supplier", "Job", "Description", "Raised By", "Date Required", "Days Late", "Part Received", "PO Value"])
    for x in data["overdue"]:
        writer.writerow([x["po_number"], x["supplier_name"], x["job_number"] or "", x["description"] or "", x["raised_by"] or "",
                         x["date_required"], x["days_late"], "Yes" if x["part_received"] else "No", f"{x['po_value']:.2f}"])
    writer.writerow([])
    writer.writerow(["Supplier", "POs Received", "On Time", "On Time %", "Avg Days Late (when late)"])
    for p in data["performance"]:
        writer.writerow([p["supplier_name"], p["received"], p["on_time"], p["on_time_pct"], p["avg_days_late"]])
    log_document_access(db, "Reporting CSV export", f"Overdue Deliveries, {data['totals']['count']} PO(s)")
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=overdue_deliveries.csv"})


def _csv_response(rows_out, filename):
    output = io.StringIO()
    writer = csv.writer(output)
    for r in rows_out: writer.writerow(r)
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


def _parse_day(v):
    try:
        return datetime.strptime((v or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


# ---- Report 3: quote follow-up and win rate -------------------------------------------------------------
# Chase list = quotes still Sent (awaiting an answer) for more than 30 days. Win rate = Won out of every quote that
# has had an outcome (Won + Lost + Expired); Draft, Sent, On Hold and Cancelled are not outcomes. Quotes are
# grouped by the month/year of their quote date.
def build_quote_followup(db, args):
    customer = args.get("customer", "").strip()
    owner = args.get("owner", "").strip()
    year = args.get("year", "").strip()
    raw = globals.query_db(db, """SELECT q.*, c.customer_name FROM quotes q LEFT JOIN customers c ON c.customer_id = q.customer_id""", one=False)
    today = date.today()
    rows = []
    for r in raw:
        if customer and (r["customer_name"] or "") != customer: continue
        if owner and (r["owner"] or "") != owner: continue
        if year and (r["quote_date"] or "")[:4] != year: continue
        rows.append(r)

    def new_bucket():
        return {"won": 0, "lost": 0, "won_value": 0.0, "decided_value": 0.0, "open": 0, "open_value": 0.0}

    def add(b, r):
        v = r["estimated_value"] or 0
        if r["status"] == "Won": b["won"] += 1; b["won_value"] += v; b["decided_value"] += v
        elif r["status"] in ("Lost", "Expired"): b["lost"] += 1; b["decided_value"] += v
        elif r["status"] == "Sent": b["open"] += 1; b["open_value"] += v

    def finish(name, b):
        decided = b["won"] + b["lost"]
        return {"name": name, "open": b["open"], "open_value": round(b["open_value"], 2), "won": b["won"], "lost": b["lost"],
                "win_pct": round(100 * b["won"] / decided) if decided else None,
                "won_value": round(b["won_value"], 2),
                "win_value_pct": round(100 * b["won_value"] / b["decided_value"]) if b["decided_value"] else None}

    total, by_owner, by_cust, by_month = new_bucket(), {}, {}, {}
    chase = []
    for r in rows:
        add(total, r)
        add(by_owner.setdefault(r["owner"] or "(no owner)", new_bucket()), r)
        add(by_cust.setdefault(r["customer_name"] or "(no customer)", new_bucket()), r)
        qd = (r["quote_date"] or "")[:7]
        if qd: add(by_month.setdefault(qd, new_bucket()), r)
        if r["status"] == "Sent":
            d0 = _parse_day(r["quote_date"])
            age = (today - d0).days if d0 else None
            if age is not None and age > 30:
                chase.append({"quote_id": r["quote_id"], "quote_number": r["quote_number"], "customer_name": r["customer_name"],
                              "description": r["description"], "owner": r["owner"], "quote_date": r["quote_date"],
                              "days_waiting": age, "value": round(r["estimated_value"] or 0, 2)})
    chase.sort(key=lambda x: -x["days_waiting"])
    t = finish("All", total)
    cust_rows = sorted((finish(k, v) for k, v in by_cust.items()), key=lambda x: -(x["open_value"] + x["won_value"]))
    return {
        "totals": {"open": t["open"], "open_value": t["open_value"], "chase": len(chase),
                   "chase_value": round(sum(x["value"] for x in chase), 2), "won": t["won"], "lost": t["lost"],
                   "win_pct": t["win_pct"], "win_value_pct": t["win_value_pct"], "won_value": t["won_value"]},
        "chase": chase,
        "by_owner": sorted((finish(k, v) for k, v in by_owner.items()), key=lambda x: -(x["won"] + x["lost"] + x["open"])),
        "by_customer": cust_rows[:25],
        "by_month": [finish(k, by_month[k]) for k in sorted(by_month, reverse=True)[:24]],
        "customers": sorted({r["customer_name"] for r in raw if r["customer_name"]}),
        "owners": sorted({r["owner"] for r in raw if r["owner"]}),
        "years": sorted({(r["quote_date"] or "")[:4] for r in raw if (r["quote_date"] or "")[:4].isdigit()}, reverse=True),
    }


@app.route("/api/reporting/quote-followup")
@login_required
def reporting_quote_followup():
    return jsonify(build_quote_followup(get_db(), request.args))


@app.route("/api/reporting/quote-followup/export")
@login_required
def reporting_quote_followup_export():
    db = get_db()
    d = build_quote_followup(db, request.args)
    out = [["Quote", "Customer", "Description", "Owner", "Quote Date", "Days Waiting", "Value"]]
    out += [[x["quote_number"], x["customer_name"] or "", x["description"] or "", x["owner"] or "", x["quote_date"] or "",
             x["days_waiting"], f"{x['value']:.2f}"] for x in d["chase"]]
    out += [[], ["Owner", "Awaiting answer", "Won", "Lost/Expired", "Win % (count)", "Win % (value)"]]
    out += [[o["name"], o["open"], o["won"], o["lost"], "" if o["win_pct"] is None else o["win_pct"],
             "" if o["win_value_pct"] is None else o["win_value_pct"]] for o in d["by_owner"]]
    log_document_access(db, "Reporting CSV export", f"Quote Follow-up, {len(d['chase'])} quote(s) to chase")
    return _csv_response(out, "quote_followup.csv")


# ---- Report 4: workload by owner ------------------------------------------------------------------------
def build_workload(db, args):
    customer = args.get("customer", "").strip()
    location = args.get("location", "").strip()
    raw = globals.query_db(db, f"""SELECT wo.*, c.customer_name FROM works_orders wo JOIN customers c ON c.customer_id = wo.customer_id
                         WHERE {PLACEHOLDER_JOB_SQL} AND wo.status IN ('Open','In Progress','On Hold')""", one=False)
    today = date.today()
    owners, people = {}, set()
    for r in raw:
        people.add(r["project_manager"] or "")
        if customer and r["customer_name"] != customer: continue
        if location and (r["location"] or "") != location: continue
        o = owners.setdefault(r["project_manager"] or "(no owner)", {
            "owner": r["project_manager"] or "(no owner)", "open": 0, "in_progress": 0, "on_hold": 0, "late": 0,
            "active_value": 0.0, "late_value": 0.0, "on_hold_value": 0.0, "max_days_late": 0})
        v = r["contract_value"] or 0
        if r["status"] == "On Hold":
            o["on_hold"] += 1; o["on_hold_value"] += v
            continue
        o["open" if r["status"] == "Open" else "in_progress"] += 1
        o["active_value"] += v
        ed = _parse_day(r["end_date"])
        if ed and ed < today:
            o["late"] += 1; o["late_value"] += v
            o["max_days_late"] = max(o["max_days_late"], (today - ed).days)
    rows = sorted(owners.values(), key=lambda x: -(x["open"] + x["in_progress"] + x["on_hold"]))
    for o in rows:
        o["live"] = o["open"] + o["in_progress"]
        for k in ("active_value", "late_value", "on_hold_value"): o[k] = round(o[k], 2)
    keys = ("open", "in_progress", "on_hold", "late", "live")
    tot = {k: sum(o[k] for o in rows) for k in keys}
    tot.update({k: round(sum(o[k] for o in rows), 2) for k in ("active_value", "late_value", "on_hold_value")})
    return {"rows": rows, "totals": tot,
            "customers": sorted({r["customer_name"] for r in raw}),
            "locations": sorted({r["location"] for r in raw if r["location"]})}


@app.route("/api/reporting/workload")
@login_required
def reporting_workload():
    return jsonify(build_workload(get_db(), request.args))


@app.route("/api/reporting/workload/export")
@login_required
def reporting_workload_export():
    db = get_db()
    d = build_workload(db, request.args)
    out = [["Owner", "Open", "In Progress", "On Hold", "Late", "Live Value", "Late Value", "On Hold Value", "Longest Late (days)"]]
    out += [[o["owner"], o["open"], o["in_progress"], o["on_hold"], o["late"], f"{o['active_value']:.2f}",
             f"{o['late_value']:.2f}", f"{o['on_hold_value']:.2f}", o["max_days_late"]] for o in d["rows"]]
    log_document_access(db, "Reporting CSV export", f"Workload by Owner, {len(d['rows'])} owner(s)")
    return _csv_response(out, "workload_by_owner.csv")


# ---- Report 5: supplier invoices awaiting approval or payment, by age ----------------------------------
def build_supplier_ageing(db, args):
    supplier = args.get("supplier", "").strip()
    status = args.get("status", "").strip()
    raw = globals.query_db(db, """SELECT si.*, s.supplier_name, po.po_number, wo.job_number
                        FROM supplier_invoices si JOIN suppliers s ON s.supplier_id = si.supplier_id
                        LEFT JOIN purchase_orders po ON po.purchase_order_id = si.purchase_order_id
                        LEFT JOIN works_orders wo ON wo.works_order_id = si.works_order_id
                        WHERE si.status IN ('Pending','Approved','Disputed')""", one=False)
    today = date.today()
    items, by = [], {}
    for r in raw:
        if supplier and r["supplier_name"] != supplier: continue
        if status and r["status"] != status: continue
        d0 = _parse_day(r["invoice_date"])
        age = (today - d0).days if d0 else None
        amt = round(r["invoice_amount"] or 0, 2)
        band = "unknown" if age is None else ("b0_30" if age <= 30 else "b31_60" if age <= 60 else "over_60")
        items.append({"supplier_invoice_id": r["supplier_invoice_id"], "invoice_number": r["invoice_number"],
                      "supplier_name": r["supplier_name"], "po_number": r["po_number"], "job_number": r["job_number"],
                      "invoice_date": r["invoice_date"], "age_days": age, "status": r["status"], "amount": amt})
        b = by.setdefault(r["supplier_name"], {"supplier_name": r["supplier_name"], "b0_30": 0.0, "b31_60": 0.0, "over_60": 0.0,
                                               "unknown": 0.0, "total": 0.0, "invoices": 0})
        b[band] += amt; b["total"] += amt; b["invoices"] += 1
    items.sort(key=lambda x: (x["age_days"] is None, -(x["age_days"] or 0)))
    suppliers = sorted(by.values(), key=lambda x: -x["total"])
    for b in suppliers:
        for k in ("b0_30", "b31_60", "over_60", "unknown", "total"): b[k] = round(b[k], 2)
    amt_of = lambda pred: round(sum(x["amount"] for x in items if pred(x)), 2)
    return {"invoices": items, "suppliers": suppliers,
            "totals": {"count": len(items), "total": amt_of(lambda x: True),
                       "pending": amt_of(lambda x: x["status"] == "Pending"),
                       "approved": amt_of(lambda x: x["status"] == "Approved"),
                       "disputed": amt_of(lambda x: x["status"] == "Disputed"),
                       "over_60": amt_of(lambda x: x["age_days"] is not None and x["age_days"] > 60)},
            "supplier_list": sorted({r["supplier_name"] for r in raw})}


@app.route("/api/reporting/supplier-ageing")
@login_required
def reporting_supplier_ageing():
    return jsonify(build_supplier_ageing(get_db(), request.args))


@app.route("/api/reporting/supplier-ageing/export")
@login_required
def reporting_supplier_ageing_export():
    db = get_db()
    d = build_supplier_ageing(db, request.args)
    out = [["Invoice", "Supplier", "PO", "Job", "Invoice Date", "Age (days)", "Status", "Amount"]]
    out += [[x["invoice_number"], x["supplier_name"], x["po_number"] or "", x["job_number"] or "", x["invoice_date"] or "",
             "" if x["age_days"] is None else x["age_days"], x["status"], f"{x['amount']:.2f}"] for x in d["invoices"]]
    out.append(["Total", "", "", "", "", "", "", f"{d['totals']['total']:.2f}"])
    log_document_access(db, "Reporting CSV export", f"Supplier Invoices by Age, {d['totals']['count']} invoice(s)")
    return _csv_response(out, "supplier_invoices_by_age.csv")


# ---- Report 6: work in progress -------------------------------------------------------------------------
# Live jobs (Open, In Progress, On Hold): cost booked so far (supplier invoices + hours + stock) against what has
# actually been invoiced to the customer (same "really invoiced" rule as Awaiting Invoicing). Cost not yet billed is
# money tied up; a negative figure means billed ahead of cost. Open PO commitments are shown separately.
def build_wip(db, args):
    customer = args.get("customer", "").strip()
    location = args.get("location", "").strip()
    owner = args.get("owner", "").strip()
    raw = globals.query_db(db, f"""
        SELECT wo.works_order_id, wo.job_number, wo.job_name, wo.status, wo.location, wo.project_manager, wo.end_date,
               wo.contract_value, c.customer_name,
          COALESCE((SELECT SUM(invoice_amount) FROM supplier_invoices WHERE works_order_id = wo.works_order_id AND status != 'Cancelled'),0) AS purchases,
          COALESCE((SELECT SUM(labour_cost) FROM labour_entries WHERE works_order_id = wo.works_order_id),0) AS hours,
          COALESCE((SELECT SUM(sa.quantity * si.purchase_cost) FROM stock_allocations sa JOIN stock_items si ON si.stock_item_id = sa.stock_item_id
                    WHERE sa.works_order_id = wo.works_order_id),0) AS stock,
          COALESCE((SELECT SUM(po_value) FROM purchase_orders WHERE works_order_id = wo.works_order_id AND status != 'Cancelled' AND is_historic = 0),0) AS po_total,
          COALESCE((SELECT SUM(ci.invoice_amount) FROM customer_invoices ci WHERE ci.works_order_id = wo.works_order_id
                    AND ci.status NOT IN ('Cancelled','Draft') AND (ci.status IN ('Paid','Overdue') OR COALESCE(ci.created_by,'') != '')),0) AS invoiced
        FROM works_orders wo JOIN customers c ON c.customer_id = wo.customer_id
        WHERE {PLACEHOLDER_JOB_SQL} AND wo.status IN ('Open','In Progress','On Hold')""", one=False)
    rows = []
    for r in raw:
        if customer and r["customer_name"] != customer: continue
        if location and (r["location"] or "") != location: continue
        if owner and (r["project_manager"] or "") != owner: continue
        cost = r["purchases"] + r["hours"] + r["stock"]
        po_out = max(0.0, r["po_total"] - r["purchases"])
        rows.append({"works_order_id": r["works_order_id"], "job_number": r["job_number"], "job_name": r["job_name"],
                     "status": r["status"], "customer_name": r["customer_name"], "location": r["location"],
                     "project_manager": r["project_manager"], "end_date": r["end_date"],
                     "po_value": round(r["contract_value"] or 0, 2), "purchases": round(r["purchases"], 2),
                     "hours": round(r["hours"], 2), "stock": round(r["stock"], 2), "cost": round(cost, 2),
                     "committed": round(po_out, 2), "invoiced": round(r["invoiced"], 2),
                     "tied_up": round(cost - r["invoiced"], 2)})
    rows.sort(key=lambda x: -x["tied_up"])
    return {"rows": rows,
            "totals": {"jobs": len(rows), "cost": round(sum(x["cost"] for x in rows), 2),
                       "invoiced": round(sum(x["invoiced"] for x in rows), 2),
                       "committed": round(sum(x["committed"] for x in rows), 2),
                       "tied_up": round(sum(x["tied_up"] for x in rows if x["tied_up"] > 0), 2),
                       "billed_ahead": round(-sum(x["tied_up"] for x in rows if x["tied_up"] < 0), 2)},
            "customers": sorted({r["customer_name"] for r in raw}),
            "locations": sorted({r["location"] for r in raw if r["location"]}),
            "owners": sorted({r["project_manager"] for r in raw if r["project_manager"]})}


@app.route("/api/reporting/wip")
@login_required
def reporting_wip():
    return jsonify(build_wip(get_db(), request.args))


@app.route("/api/reporting/wip/export")
@login_required
def reporting_wip_export():
    db = get_db()
    d = build_wip(db, request.args)
    out = [["Job", "Job Name", "Status", "Customer / Site", "Location", "Owner", "PO Value", "Purchases", "Hours", "Stock",
            "Cost To Date", "Invoiced", "Cost Not Yet Invoiced", "Open PO Commitments"]]
    out += [[x["job_number"], x["job_name"], x["status"], x["customer_name"], x["location"] or "", x["project_manager"] or "",
             f"{x['po_value']:.2f}", f"{x['purchases']:.2f}", f"{x['hours']:.2f}", f"{x['stock']:.2f}", f"{x['cost']:.2f}",
             f"{x['invoiced']:.2f}", f"{x['tied_up']:.2f}", f"{x['committed']:.2f}"] for x in d["rows"]]
    log_document_access(db, "Reporting CSV export", f"Work in Progress, {d['totals']['jobs']} job(s)")
    return _csv_response(out, "work_in_progress.csv")


# ---- Report 7: on-time delivery to customers ------------------------------------------------------------
# Jobs with a Required By date. Delivered = the latest Despatched despatch note on the job. On time = delivered on or
# before Required By. Not delivered and past Required By = overdue (counted late). Draft and Cancelled jobs are left out.
def build_on_time_delivery(db, args):
    """Delivered = latest Despatched despatch note, otherwise the Order Book's 'Delivered On' date kept on the job."""
    customer = args.get("customer", "").strip()
    location = args.get("location", "").strip()
    owner = args.get("owner", "").strip()
    year = args.get("year", "").strip()
    raw = globals.query_db(db, f"""
        SELECT wo.works_order_id, wo.job_number, wo.job_name, wo.status, wo.location, wo.project_manager, wo.required_by,
               wo.contract_value, c.customer_name,
               COALESCE((SELECT MAX(dn.despatch_date) FROM despatch_notes dn WHERE dn.works_order_id = wo.works_order_id
                  AND dn.status = 'Despatched'), NULLIF(wo.delivered_date,'')) AS delivered
        FROM works_orders wo JOIN customers c ON c.customer_id = wo.customer_id
        WHERE {PLACEHOLDER_JOB_SQL} AND COALESCE(wo.required_by,'') != '' AND wo.status NOT IN ('Draft','Cancelled')""", one=False)
    today = date.today()
    def bucket(): return {"delivered": 0, "on_time": 0, "late": 0, "late_days": 0, "overdue": 0, "waiting": 0}
    total, by_c, by_o, by_m, listing = bucket(), {}, {}, {}, []
    suspect = no_date = 0
    for r in raw:
        req = _parse_day(r["required_by"])
        if req is None: continue
        if customer and r["customer_name"] != customer: continue
        if location and (r["location"] or "") != location: continue
        if owner and (r["project_manager"] or "") != owner: continue
        if year and req.isoformat()[:4] != year: continue
        got = _parse_day(r["delivered"])
        late_by = None
        if got and abs((got - req).days) > 365:
            suspect += 1          # a date typo (e.g. wrong year) - left out of the figures rather than distort them
            continue
        if got:
            late_by = (got - req).days
            state = "on_time" if late_by <= 0 else "late"
        elif r["status"] in ("Completed", "Closed"):
            no_date += 1          # finished, but no delivery date was ever recorded - can't say on time or late
            continue
        elif req < today:
            late_by = (today - req).days
            state = "overdue"
        else:
            state = "waiting"
        for b in (total, by_c.setdefault(r["customer_name"], bucket()), by_o.setdefault(r["project_manager"] or "(no owner)", bucket()),
                  by_m.setdefault(req.isoformat()[:7], bucket())):
            if state == "on_time": b["delivered"] += 1; b["on_time"] += 1
            elif state == "late": b["delivered"] += 1; b["late"] += 1; b["late_days"] += late_by
            elif state == "overdue": b["overdue"] += 1
            else: b["waiting"] += 1
        if state in ("late", "overdue"):
            listing.append({"works_order_id": r["works_order_id"], "job_number": r["job_number"], "job_name": r["job_name"],
                            "customer_name": r["customer_name"], "project_manager": r["project_manager"],
                            "required_by": r["required_by"], "delivered": r["delivered"] if got else None,
                            "days_late": late_by, "state": state, "po_value": round(r["contract_value"] or 0, 2)})
    listing.sort(key=lambda x: -x["days_late"])
    def fin(name, b):
        return {"name": name, "delivered": b["delivered"], "on_time": b["on_time"], "late": b["late"],
                "overdue": b["overdue"], "waiting": b["waiting"],
                "on_time_pct": round(100 * b["on_time"] / (b["delivered"] + b["overdue"])) if (b["delivered"] + b["overdue"]) else None,
                "avg_days_late": round(b["late_days"] / b["late"], 1) if b["late"] else 0}
    t = fin("All", total)
    return {"totals": t, "listing": listing, "suspect_dates": suspect, "no_delivery_date": no_date,
            "by_customer": sorted((fin(k, v) for k, v in by_c.items()), key=lambda x: -(x["delivered"] + x["overdue"])),
            "by_owner": sorted((fin(k, v) for k, v in by_o.items()), key=lambda x: -(x["delivered"] + x["overdue"])),
            "by_month": [fin(k, by_m[k]) for k in sorted(by_m, reverse=True)[:24]],
            "customers": sorted({r["customer_name"] for r in raw}),
            "locations": sorted({r["location"] for r in raw if r["location"]}),
            "owners": sorted({r["project_manager"] for r in raw if r["project_manager"]}),
            "years": sorted({(r["required_by"] or "")[:4] for r in raw if (r["required_by"] or "")[:4].isdigit()}, reverse=True)}


@app.route("/api/reporting/on-time-delivery")
@login_required
def reporting_on_time_delivery():
    return jsonify(build_on_time_delivery(get_db(), request.args))


@app.route("/api/reporting/on-time-delivery/export")
@login_required
def reporting_on_time_delivery_export():
    db = get_db()
    d = build_on_time_delivery(db, request.args)
    out = [["Job", "Job Name", "Customer / Site", "Owner", "Required By", "Delivered", "Days Late", "Status"]]
    out += [[x["job_number"], x["job_name"], x["customer_name"], x["project_manager"] or "", x["required_by"],
             x["delivered"] or "", x["days_late"], "Delivered late" if x["state"] == "late" else "Not delivered, overdue"] for x in d["listing"]]
    out += [[], ["Site / Customer", "Delivered", "On time", "Late", "Overdue, not delivered", "On time %", "Avg days late"]]
    out += [[c["name"], c["delivered"], c["on_time"], c["late"], c["overdue"], "" if c["on_time_pct"] is None else c["on_time_pct"],
             c["avg_days_late"]] for c in d["by_customer"]]
    log_document_access(db, "Reporting CSV export", "On-time Delivery")
    return _csv_response(out, "on_time_delivery.csv")


# ---- Report 9: jobs On Hold, and for how long -----------------------------------------------------------
# "On Hold since" is the date of the most recent change of a job's status to On Hold in the History log. Jobs put On
# Hold before that was recorded (e.g. arriving from the Order Book) have no start date and are shown as unknown.
def build_on_hold(db, args):
    customer = args.get("customer", "").strip()
    location = args.get("location", "").strip()
    owner = args.get("owner", "").strip()
    raw = globals.query_db(db, f"""
        SELECT wo.*, c.customer_name,
          (SELECT MAX(a.changed_date) FROM audit_log a WHERE a.table_name = 'works_orders' AND a.record_id = wo.works_order_id
             AND a.field_name = 'status' AND a.new_value = 'On Hold') AS held_since,
          (SELECT a.changed_by FROM audit_log a WHERE a.table_name = 'works_orders' AND a.record_id = wo.works_order_id
             AND a.field_name = 'status' AND a.new_value = 'On Hold' ORDER BY a.changed_date DESC, a.audit_id DESC LIMIT 1) AS held_by
        FROM works_orders wo JOIN customers c ON c.customer_id = wo.customer_id
        WHERE {PLACEHOLDER_JOB_SQL} AND wo.status = 'On Hold'""", one=False)
    today = date.today()
    rows = []
    for r in raw:
        if customer and r["customer_name"] != customer: continue
        if location and (r["location"] or "") != location: continue
        if owner and (r["project_manager"] or "") != owner: continue
        since = _parse_day(r["held_since"])
        rows.append({"works_order_id": r["works_order_id"], "job_number": r["job_number"], "job_name": r["job_name"],
                     "customer_name": r["customer_name"], "location": r["location"], "project_manager": r["project_manager"],
                     "held_since": since.isoformat() if since else None, "held_by": r["held_by"] if since else None,
                     "days_on_hold": (today - since).days if since else None,
                     "end_date": r["end_date"], "po_value": round(r["contract_value"] or 0, 2)})
    rows.sort(key=lambda x: (x["days_on_hold"] is None, -(x["days_on_hold"] or 0)))
    known = [x["days_on_hold"] for x in rows if x["days_on_hold"] is not None]
    return {"rows": rows,
            "totals": {"jobs": len(rows), "value": round(sum(x["po_value"] for x in rows), 2),
                       "over_30": sum(1 for d in known if d > 30), "longest": max(known) if known else 0,
                       "avg_days": round(sum(known) / len(known)) if known else 0, "unknown": len(rows) - len(known)},
            "customers": sorted({r["customer_name"] for r in raw}),
            "locations": sorted({r["location"] for r in raw if r["location"]}),
            "owners": sorted({r["project_manager"] for r in raw if r["project_manager"]})}


@app.route("/api/reporting/on-hold")
@login_required
def reporting_on_hold():
    return jsonify(build_on_hold(get_db(), request.args))


@app.route("/api/reporting/on-hold/export")
@login_required
def reporting_on_hold_export():
    db = get_db()
    d = build_on_hold(db, request.args)
    out = [["Job", "Job Name", "Customer / Site", "Location", "Owner", "On Hold Since", "Days On Hold", "Put On Hold By", "PO Value"]]
    out += [[x["job_number"], x["job_name"], x["customer_name"], x["location"] or "", x["project_manager"] or "",
             x["held_since"] or "Unknown", "" if x["days_on_hold"] is None else x["days_on_hold"], x["held_by"] or "",
             f"{x['po_value']:.2f}"] for x in d["rows"]]
    log_document_access(db, "Reporting CSV export", f"Jobs On Hold, {d['totals']['jobs']} job(s)")
    return _csv_response(out, "jobs_on_hold.csv")


# ---- Deliveries Due: live jobs due for delivery over the next few months, with income, cost and profit ----
# A job's delivery date is its Required By date, or its End Date when no Required By has been entered. Income is the job's
# Purchase Order Value. Forecast cost is cost booked so far plus purchase orders not yet invoiced, so a job that has not
# had its purchasing raised yet will look more profitable than it will turn out to be.
def _month_start(d, add=0):
    m = d.month - 1 + add
    return date(d.year + m // 12, m % 12 + 1, 1)


def build_deliveries_due(db, args):
    customer = args.get("customer", "").strip()
    location = args.get("location", "").strip()
    owner = args.get("owner", "").strip()
    months = min(12, max(1, safe_int(args.get("months"), 3) or 3))
    only = args.get("month", "").strip()
    today = date.today()
    starts = [_month_start(today, i) for i in range(months)]
    window_end = _month_start(today, months)
    labels = {st.strftime("%Y-%m"): st.strftime("%B %Y") for st in starts}
    raw = globals.query_db(db,
        f"""SELECT wo.*, c.customer_name FROM works_orders wo JOIN customers c ON c.customer_id = wo.customer_id
            WHERE {PLACEHOLDER_JOB_SQL} AND wo.status NOT IN ('Completed','Closed','Cancelled')
              AND COALESCE(wo.delivered_date,'') = ''""", one=False)

    # Filter matching jobs first, then compute metrics in bulk (7 queries total instead of 6 per job)
    eligible = []
    no_date = 0
    for r in raw:
        if customer and r["customer_name"] != customer:
            continue
        if location and (r["location"] or "") != location:
            continue
        if owner and (r["project_manager"] or "") != owner:
            continue
        due = _parse_day(r["required_by"]) or _parse_day(r["end_date"])
        if due is None:
            no_date += 1
            continue
        if due >= window_end:
            continue
        eligible.append((r, due))

    jobs_with_metrics = load_all_jobs_with_metrics(db, rows=[r for r, _ in eligible])
    metrics_by_id = {j["works_order_id"]: j for j in jobs_with_metrics}

    groups = {"overdue": [], **{k: [] for k in labels}}
    for r, due in eligible:
        m = metrics_by_id[r["works_order_id"]]
        income = round(r["contract_value"] or 0, 2)
        forecast = round(m["forecast_cost"], 2)
        profit = round(income - forecast, 2)
        row = {
            "works_order_id": r["works_order_id"],
            "job_number": r["job_number"],
            "job_name": r["job_name"],
            "customer_name": r["customer_name"],
            "location": r["location"],
            "project_manager": r["project_manager"],
            "status": r["status"],
            "due_date": due.isoformat(),
            "date_source": "Required By" if r["required_by"] else "End Date",
            "income": income,
            "actual_cost": round(m["actual_cost"], 2),
            "forecast_cost": forecast,
            "profit": profit,
            "margin_pct": round(profit / income * 100, 1) if income else None,
            "days_late": (today - due).days if due < today else 0,
        }
        key = "overdue" if due < today else due.strftime("%Y-%m")
        groups[key].append(row)

    def totals(rows):
        inc, act, fc = (round(sum(x[k] for x in rows), 2) for k in ("income", "actual_cost", "forecast_cost"))
        pr = round(inc - fc, 2)
        return {
            "jobs": len(rows),
            "income": inc,
            "actual_cost": act,
            "forecast_cost": fc,
            "profit": pr,
            "margin_pct": round(pr / inc * 100, 1) if inc else None,
        }

    out_groups = []
    for key in ["overdue", *labels]:
        rows = sorted(groups[key], key=lambda x: (x["due_date"], x["job_number"]))
        if only and only != key:
            continue
        out_groups.append({
            "key": key,
            "label": "Already overdue (still open)" if key == "overdue" else labels[key],
            "rows": rows,
            "totals": totals(rows),
        })
    window_rows = [x for k in labels for x in groups[k]]
    return {
        "groups": out_groups,
        "months": [{"key": k, "label": v, "totals": totals(groups[k])} for k, v in labels.items()],
        "overdue_totals": totals(groups["overdue"]),
        "window_totals": totals(window_rows),
        "no_date": no_date,
        "window_end": (window_end - timedelta(days=1)).isoformat(),
        "customers": sorted({r["customer_name"] for r in raw}),
        "locations": sorted({r["location"] for r in raw if r["location"]}),
        "owners": sorted({r["project_manager"] for r in raw if r["project_manager"]}),
    }


@app.route("/api/reporting/deliveries-due")
@login_required
def reporting_deliveries_due():
    return jsonify(build_deliveries_due(get_db(), request.args))


@app.route("/api/reporting/deliveries-due/export")
@login_required
def reporting_deliveries_due_export():
    db = get_db()
    d = build_deliveries_due(db, request.args)
    out = [["Due Month", "Job", "Job Name", "Customer / Site", "Location", "Owner", "Status", "Delivery Date", "Date From",
            "Days Late", "Income (PO Value)", "Actual Cost", "Forecast Cost", "Forecast Profit", "Margin %"]]
    for g in d["groups"]:
        for x in g["rows"]:
            out.append([g["label"], x["job_number"], x["job_name"], x["customer_name"], x["location"] or "", x["project_manager"] or "",
                        x["status"], x["due_date"], x["date_source"], x["days_late"] or "", f"{x['income']:.2f}",
                        f"{x['actual_cost']:.2f}", f"{x['forecast_cost']:.2f}", f"{x['profit']:.2f}",
                        "" if x["margin_pct"] is None else x["margin_pct"]])
        t = g["totals"]
        out.append([g["label"] + " total", "", f"{t['jobs']} job(s)", "", "", "", "", "", "", "", f"{t['income']:.2f}",
                    f"{t['actual_cost']:.2f}", f"{t['forecast_cost']:.2f}", f"{t['profit']:.2f}", "" if t["margin_pct"] is None else t["margin_pct"]])
    log_document_access(db, "Reporting CSV export", f"Deliveries Due, {d['window_totals']['jobs']} job(s) in window")
    return _csv_response(out, "deliveries_due.csv")


def build_report_rows(db):
    rows = globals.query_db(db,
        f"""SELECT wo.*, c.customer_name FROM works_orders wo
           JOIN customers c ON c.customer_id = wo.customer_id WHERE {PLACEHOLDER_JOB_SQL} ORDER BY wo.job_number""", one=False)
    jobs = load_all_jobs_with_metrics(db, rows=rows)

    # Fetch all invoiced works_order_ids in ONE query instead of 1 query per job
    invoiced_rows = globals.query_db(db,
        """SELECT DISTINCT ci.works_order_id FROM customer_invoices ci
           WHERE ci.status != 'Cancelled' AND ci.status != 'Draft'
             AND (ci.status IN ('Paid','Overdue') OR COALESCE(ci.created_by,'') != '')""", one=False)
    invoiced_ids = {r["works_order_id"] for r in invoiced_rows}

    report = []
    for j in jobs:
        report.append({
            "job_number": j["job_number"],
            "customer_name": j["customer_name"],
            "contract_value": j["contract_value"],
            "actual_cost": j["actual_cost"],
            "committed_cost": j["committed_cost"],
            "forecast_cost": j["forecast_cost"],
            "revenue": j["revenue"],
            "profit": j["profit"],
            "forecast_profit": j["forecast_profit"],
            "margin_pct": j["margin_pct"],
            "margin_target": j["margin_target"],
            "status": j["status"],
            "contract_code": j["contract_code"],
            "location": j.get("location"),
            "invoiced": j["works_order_id"] in invoiced_ids,
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
    rows = globals.query_db(db, 
        f"""SELECT s.supplier_id, s.supplier_name,
                   COUNT(si.supplier_invoice_id) AS invoice_count,
                   COALESCE(SUM(si.invoice_amount), 0) AS total_spend,
                   MAX(si.invoice_date) AS last_invoice_date
            FROM suppliers s
            LEFT JOIN supplier_invoices si
                   ON si.supplier_id = s.supplier_id AND si.status != 'Cancelled' {year_clause}
            GROUP BY s.supplier_id
            ORDER BY s.supplier_name""",
        params, one=False
    )
    years = globals.query_db(db, 
        "SELECT DISTINCT strftime('%Y', invoice_date) AS yr FROM supplier_invoices "
        "WHERE invoice_date IS NOT NULL AND status != 'Cancelled' ORDER BY yr DESC",
        one=False
    )
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
    years_rows = globals.query_db(db, 
        """SELECT DISTINCT strftime('%Y', d) AS yr FROM (
               SELECT invoice_date AS d FROM customer_invoices WHERE status != 'Cancelled'
               UNION ALL
               SELECT invoice_date AS d FROM supplier_invoices WHERE status != 'Cancelled'
               UNION ALL
               SELECT work_date AS d FROM labour_entries
           ) WHERE d IS NOT NULL""",
        one=False
    )
    current_year = date.today().year
    years = sorted({y["yr"] for y in years_rows if y["yr"]}, reverse=True)
    if str(current_year) not in years:
        years.insert(0, str(current_year))  # always offer the current year, even on a fresh install with no data yet
        years.sort(reverse=True)

    year = requested_year if requested_year in years else years[0]

    income_rows = globals.query_db(db, 
        """SELECT strftime('%Y-%m', invoice_date) AS ym, COALESCE(SUM(invoice_amount),0) AS total
           FROM customer_invoices WHERE status != 'Cancelled' AND strftime('%Y', invoice_date) = ?
           GROUP BY ym""",
        (year,), one=False
    )
    supplier_cost_rows = globals.query_db(db, 
        """SELECT strftime('%Y-%m', invoice_date) AS ym, COALESCE(SUM(invoice_amount),0) AS total
           FROM supplier_invoices WHERE status != 'Cancelled' AND strftime('%Y', invoice_date) = ?
           GROUP BY ym""",
        (year,), one=False
    )
    labour_cost_rows = globals.query_db(db, 
        """SELECT strftime('%Y-%m', work_date) AS ym, COALESCE(SUM(labour_cost),0) AS total
           FROM labour_entries WHERE strftime('%Y', work_date) = ?
           GROUP BY ym""",
        (year,), one=False
    )

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
    one = lambda sql, params=(): globals.query_db(db, sql, params, one=True)

    def money(v):
        return None if engineer else round(v or 0, 2)

    out = {"year": year}

    if not engineer:
        sent = one("SELECT COUNT(*) AS cnt, COALESCE(SUM(estimated_value),0) AS total FROM quotes WHERE status='Sent'")
        won = one("SELECT COUNT(*) AS cnt, COALESCE(SUM(estimated_value),0) AS total FROM quotes "
                  "WHERE status='Won' AND substr(COALESCE(quote_date, created_date),1,4)=?", (year,))
        out["quotes"] = {
            "awaiting_count": sent["cnt"],
            "awaiting_value": round(sent["total"], 2),
            "po_received_count": won["cnt"],
            "po_received_value": round(won["total"], 2),
        }

    wo_open = one(f"""SELECT COUNT(*) AS cnt, COALESCE(SUM(contract_value),0) AS total FROM works_orders wo
                      WHERE {PLACEHOLDER_JOB_SQL} AND status IN ('Open','In Progress')""")
    wo_done = one(f"""SELECT COUNT(*) AS cnt, COALESCE(SUM(contract_value),0) AS total FROM works_orders wo
                      WHERE {PLACEHOLDER_JOB_SQL} AND status='Completed' AND substr(end_date,1,4)=?""", (year,))
    out["works_orders"] = {
        "open_count": wo_open["cnt"],
        "open_value": money(wo_open["total"]),
        "completed_ytd_count": wo_done["cnt"],
        "completed_ytd_value": money(wo_done["total"]),
    }

    live_po = "po.is_historic=0 AND po.status IN ('Issued','Approved')"
    awaiting = one(f"""SELECT COUNT(*) AS cnt, COALESCE(SUM(po.po_value),0) AS total FROM purchase_orders po
                       WHERE {live_po} AND NOT EXISTS (SELECT 1 FROM goods_in g WHERE g.purchase_order_id=po.purchase_order_id)""")
    part = one(f"""SELECT COUNT(*) AS cnt, COALESCE(SUM(po.po_value),0) AS total FROM purchase_orders po
                   WHERE {live_po}
                     AND EXISTS (SELECT 1 FROM goods_in g WHERE g.purchase_order_id=po.purchase_order_id)
                     AND NOT EXISTS (SELECT 1 FROM goods_in g WHERE g.purchase_order_id=po.purchase_order_id
                                     AND g.quantity_status='Full')""")
    out["purchase_orders"] = {
        "awaiting_count": awaiting["cnt"],
        "awaiting_value": money(awaiting["total"]),
        "part_received_count": part["cnt"],
    }

    out["goods_in"] = {
        "total": one("SELECT COUNT(*) AS cnt FROM goods_in")["cnt"],
        "ytd": one("SELECT COUNT(*) AS cnt FROM goods_in WHERE substr(received_date,1,4)=?", (year,))["cnt"],
        "month": one("SELECT COUNT(*) AS cnt FROM goods_in WHERE substr(received_date,1,7)=?", (month,))["cnt"],
    }

    desp = "FROM despatch_notes WHERE status='Despatched'"
    out["despatch_notes"] = {
        "jobs_complete": one(f"SELECT COUNT(DISTINCT works_order_id) AS cnt {desp}")["cnt"],
        "ytd": one(f"SELECT COUNT(DISTINCT works_order_id) AS cnt {desp} AND substr(despatch_date,1,4)=?", (year,))["cnt"],
        "month": one(f"SELECT COUNT(DISTINCT works_order_id) AS cnt {desp} AND substr(despatch_date,1,7)=?", (month,))["cnt"],
    }

    if not engineer:
        out["customers"] = {"total": one("SELECT COUNT(*) AS cnt FROM customers")["cnt"]}
        out["suppliers"] = {"total": one("SELECT COUNT(*) AS cnt FROM suppliers")["cnt"]}
    return jsonify(out)


# Hours Booked report: hours from Labour Entries (the "Hours Quoted & Booked"
# screen), by month for one year, grouped by employee, job, customer or
# contract code. Filters narrow the entries before they are grouped.
HOURS_GROUPS = {"employee", "job", "customer", "contract_code"}


def build_hours_booked(db, args):
    year_rows = globals.query_db(db,
        "SELECT DISTINCT substr(work_date,1,4) AS yr FROM labour_entries WHERE work_date IS NOT NULL AND work_date != '' "
        "ORDER BY 1 DESC", one=False
    )
    years = [r["yr"] for r in year_rows if r["yr"]]
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
    entries = globals.query_db(db, sql, tuple(params), one=False)

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
    emp_rows = globals.query_db(db, "SELECT DISTINCT employee_name FROM labour_entries ORDER BY 1", one=False)
    employees = [r["employee_name"] for r in emp_rows]
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
    rows = globals.query_db(db, 
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
           ORDER BY wo.job_number""", one=False
    )
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
    rows = globals.query_db(db, 
        "SELECT user_id, username, full_name, role, email, created_date, must_change_password, totp_enabled "
        "FROM users ORDER BY full_name", one=False
    )
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
        user_id = globals.query_db(db, 
            "INSERT INTO users (username, password_hash, full_name, role, email, must_change_password) VALUES (?,?,?,?,?,1)",
            (data.get("username"), generate_password_hash(password), data.get("full_name"), data.get("role"), data.get("email")),
            commit=True
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    resp = {"user_id": user_id}
    if auto_generated:
        resp["temporary_password"] = password
    return jsonify(resp), 201


@app.route("/api/users/<int:uid>", methods=["PUT"])
@permission_required("all")
def update_user(uid):
    data = request.get_json(force=True) or {}
    db = get_db()
    target = globals.query_db(db, "SELECT * FROM users WHERE user_id=?", (uid,), one=True)
    if not target:
        return jsonify({"error": "User not found"}), 404

    new_role = data.get("role", target["role"])
    # Same safety net as delete: don't let the last Administrator demote
    # themselves (or be demoted) with no other admin left to undo it.
    if target["role"] == "Administrator" and new_role != "Administrator":
        row = globals.query_db(db, 
            "SELECT COUNT(*) AS cnt FROM users WHERE role='Administrator'", one=True)
        admin_count = row['cnt'] if row else 0
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
            globals.query_db(db,
                "UPDATE users SET role=?, email=?, password_hash=?, must_change_password=1 WHERE user_id=?",
                (new_role, new_email, generate_password_hash(new_password), uid),
                commit=True
            )
        else:
            globals.query_db(db,
                "UPDATE users SET role=?, email=? WHERE user_id=?",
                (new_role, new_email, uid),
                commit=True
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/users/<int:uid>", methods=["DELETE"])
@permission_required("all")
def delete_user(uid):
    db = get_db()
    target = globals.query_db(db, "SELECT * FROM users WHERE user_id=?", (uid,), one=True)
    if not target:
        return jsonify({"error": "User not found"}), 404
    if target["role"] == "Administrator":
        row = globals.query_db(db, 
            "SELECT COUNT(*) AS cnt FROM users WHERE role='Administrator'", one=True)
        admin_count = row['cnt'] if row else 0
        if admin_count <= 1:
            return jsonify({
                "error": "Can't delete the last remaining Administrator - "
                         "create another admin account first, or promote someone else to Administrator."
            }), 400
    globals.query_db(db, "DELETE FROM users WHERE user_id=?", (uid,), commit=True)
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
    target = globals.query_db(db, "SELECT * FROM users WHERE user_id=?", (uid,), one=True)
    if not target:
        return jsonify({"error": "User not found"}), 404
    globals.query_db(db, "UPDATE users SET totp_secret=NULL, totp_enabled=0 WHERE user_id=?", (uid,), commit=False)
    globals.query_db(db, "DELETE FROM totp_backup_codes WHERE user_id=?", (uid,), commit=False)
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
        table: globals.query_db(db, f"SELECT COUNT(*) AS n FROM {table}", one=True)["n"]
        for table in RESET_JOB_DATA_TABLES
    }
    counts["stock_allocations"] = globals.query_db(db,
        "SELECT COUNT(*) AS n FROM stock_allocations WHERE works_order_id IS NOT NULL", one=True)["n"]
    counts["stock_movements"] = globals.query_db(db,
        "SELECT COUNT(*) AS n FROM stock_movements "
        "WHERE from_works_order_id IS NOT NULL OR to_works_order_id IS NOT NULL", one=True)["n"]

    doc_files = [r["stored_filename"] for r in
                 globals.query_db(db, "SELECT stored_filename FROM goods_in_documents", one=False)]

    # works_orders is deleted last of all: stock_allocations/stock_movements
    # reference it without ON DELETE CASCADE (unlike the others, which
    # cascade or are deleted in dependency order above), so those rows
    # must be gone first or this would fail the foreign-key check.
    for table in RESET_JOB_DATA_TABLES:
        if table != "works_orders":
            globals.query_db(db, f"DELETE FROM {table}", commit=False)
    globals.query_db(db, "DELETE FROM stock_allocations WHERE works_order_id IS NOT NULL", commit=False)
    globals.query_db(db, 
        "DELETE FROM stock_movements "
        "WHERE from_works_order_id IS NOT NULL OR to_works_order_id IS NOT NULL", commit=False)
    globals.query_db(db, "DELETE FROM works_orders", commit=False)
    globals.query_db(db, "DELETE FROM alert_acceptances", commit=False)
    globals.query_db(db, 
        """INSERT INTO audit_log (table_name, record_id, field_name, old_value, new_value, changed_by)
           VALUES ('system', 0, 'reset_job_data', ?, 'reset for go-live', ?)""",
        (", ".join(f"{k}: {v}" for k, v in counts.items()), current_user_name()),
        commit=False
    )
    db.commit()

    for stored_name in doc_files:
        if stored_name and stored_name.startswith("https://"):
            try:
                blob.delete_blob(stored_name)
            except Exception:
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
        for row in globals.query_db(db,
            "SELECT contract_code, COUNT(*) AS n FROM works_orders WHERE contract_code IS NOT NULL GROUP BY contract_code",
            one=False
        )
    }
    pricing_rows = globals.query_db(db, "SELECT contract_code, standard_value, standard_cost FROM contract_code_targets", one=False)
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
    globals.query_db(db,
        "INSERT INTO settings (key, value) VALUES ('default_margin_target', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(target),), commit=True
    )
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
    globals.query_db(db, 
        "INSERT INTO contract_code_targets (contract_code, target_pct, standard_value, standard_cost) VALUES (?,?,?,?) "
        "ON CONFLICT(contract_code) DO UPDATE SET target_pct=excluded.target_pct, standard_value=excluded.standard_value, standard_cost=excluded.standard_cost",
        (code, target, standard_value, standard_cost), commit=True
    )
    return jsonify({"ok": True})


@app.route("/api/settings/margin-targets/<code>", methods=["DELETE"])
@permission_required("all")
def delete_contract_code_target(code):
    db = get_db()
    globals.query_db(db, "DELETE FROM contract_code_targets WHERE contract_code=?", (code.strip().upper(),), commit=True)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Settings - Ship To addresses (Administrator only to manage)
# --------------------------------------------------------------------------
@app.route("/api/settings/ship-to-addresses", methods=["GET"])
@login_required
def list_ship_to_addresses():
    db = get_db()
    rows = globals.query_db(db, "SELECT * FROM ship_to_addresses ORDER BY label", one=False)
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
        ship_id = globals.query_db(db, 
            """INSERT INTO ship_to_addresses (label, company_name, address_line1, address_line2, city, postcode)
               VALUES (?,?,?,?,?,?)""",
            (label, data.get("company_name"), data.get("address_line1"), data.get("address_line2"),
             data.get("city"), data.get("postcode")),
            commit=True
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ship_to_id": ship_id}), 201


@app.route("/api/settings/ship-to-addresses/<int:sid>", methods=["PUT"])
@permission_required("all")
def update_ship_to_address(sid):
    data = request.get_json(force=True) or {}
    label = (data.get("label") or "").strip()
    if not label:
        return jsonify({"error": "Label is required"}), 400
    db = get_db()
    try:
        globals.query_db(db,
            """UPDATE ship_to_addresses SET label=?, company_name=?, address_line1=?, address_line2=?,
               city=?, postcode=? WHERE ship_to_id=?""",
            (label, data.get("company_name"), data.get("address_line1"), data.get("address_line2"),
             data.get("city"), data.get("postcode"), sid),
            commit=True
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/settings/ship-to-addresses/<int:sid>", methods=["DELETE"])
@permission_required("all")
def delete_ship_to_address(sid):
    db = get_db()
    try:
        globals.query_db(db, "DELETE FROM ship_to_addresses WHERE ship_to_id=?", (sid,), commit=True)
    except Exception:
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
        "customers": rows_to_list(globals.query_db(db, "SELECT customer_id, customer_name, contact_name, address FROM customers ORDER BY customer_name", one=False)),
        "suppliers": rows_to_list(globals.query_db(db,
            """SELECT supplier_id, supplier_name, approval_status,
                      (SELECT MIN(expiry_date) FROM supplier_certificates c WHERE c.supplier_id = suppliers.supplier_id
                         AND COALESCE(c.expiry_date,'') != '') AS next_cert_expiry
               FROM suppliers ORDER BY supplier_name""", one=False)),
        # project_manager is included here (not just on the live /api/works-orders
        # list) because the Works Orders page's own Owner filter dropdown builds
        # its option list from this reference set, not from the page's current
        # (possibly already-filtered) rows - see renderWorksOrders() in app.js.
        "works_orders": rows_to_list(globals.query_db(db, "SELECT works_order_id, job_number, job_name, customer_id, contract_code, location, project_manager FROM works_orders wo WHERE " + PLACEHOLDER_JOB_SQL + " ORDER BY job_number", one=False)),
        # placeholder jobs the historic POs sit on: not offered in pick-lists, but the
        # Purchase Orders page still needs them to show the customer for those POs
        "placeholder_jobs": rows_to_list(globals.query_db(db, "SELECT works_order_id, job_number, job_name, customer_id, contract_code, location, project_manager FROM works_orders wo WHERE wo.job_number LIKE 'HIST-PO-%' ORDER BY job_number", one=False)),
        # Real people only - used to keep the PO "Raised by" filter free of initials,
        # combined entries and other non-names.
        "staff_names": sorted({n for n in __import__("tidy_raised_by").PEOPLE}
                              | {r['full_name'] for r in globals.query_db(db, "SELECT full_name FROM users WHERE full_name IS NOT NULL AND TRIM(full_name) != ''", one=False)}),
        "locations": [r['location'] for r in globals.query_db(db,
            "SELECT DISTINCT location FROM works_orders WHERE location IS NOT NULL AND TRIM(location) != '' ORDER BY location", one=False)],
        "purchase_orders": rows_to_list(globals.query_db(db,
            "SELECT purchase_order_id, po_number, works_order_id, po_value FROM purchase_orders "
            "WHERE is_historic=0 ORDER BY po_number", one=False)),
        "ship_to_addresses": rows_to_list(globals.query_db(db,
            "SELECT ship_to_id, label, company_name, address_line1, address_line2, city, postcode FROM ship_to_addresses ORDER BY label", one=False)),
        "contract_code_targets": [] if is_service_engineer() else rows_to_list(globals.query_db(db,
            "SELECT contract_code, target_pct, standard_value, standard_cost FROM contract_code_targets ORDER BY contract_code", one=False)),
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
        row = globals.query_db(conn, "SELECT value FROM settings WHERE key='app_version'", one=True)
        stored = row["value"] if row else None
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
            globals.query_db(conn, "INSERT OR REPLACE INTO settings (key, value) VALUES ('app_version', ?)",
                (app_version.APP_VERSION,), commit=False)
            globals.query_db(conn, 
                "INSERT INTO audit_log (table_name, record_id, field_name, old_value, new_value, changed_by) "
                "VALUES ('system', 0, 'app_version', ?, ?, 'startup')", (stored, app_version.APP_VERSION), commit=False)
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
