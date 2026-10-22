"""
Two-factor authentication (TOTP, RFC 6238) - implemented directly against
Python's own standard library (hmac, hashlib, struct, base64) rather than
pulling in a third-party TOTP package, so this feature adds zero new
dependencies to install, pin, or keep patched. Verified against the
official RFC 4226 HOTP test vectors (the counter-based primitive TOTP is
built on) during development.

Setup offers both a QR code to scan and a manual entry key to type - the
manual key (look for "Enter a setup key" / "Enter code manually" on the
add-account screen) is guaranteed to work with any TOTP app, so it's
always shown even though the QR image (see qr_png.py and app.py's
get_2fa_setup_qr) is usually the quicker option. Both come from the exact
same provisioning_uri() below - one is typed from the text, the other
rendered into an image - so there is only ever one secret to transcribe
correctly, not two different setup paths that could drift apart.

Used by the 2FA setup/verify routes in app.py.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse

TOTP_PERIOD = 30     # seconds per code - the standard interval every authenticator app assumes
TOTP_DIGITS = 6
ISSUER = "TEAMworks360"


def generate_secret():
    """A random 160-bit secret, base32-encoded without padding - the
    standard size and format every TOTP authenticator app expects."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def format_secret_for_display(secret):
    """The raw secret, grouped into 4-character blocks (XXXX XXXX XXXX...)
    so it's easier to read and type correctly by hand into an authenticator
    app's "enter a setup key manually" screen."""
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


def provisioning_uri(secret, account_name, issuer=ISSUER):
    """A standard otpauth:// URI for this secret - included alongside the
    manual key as a convenience for the handful of authenticator apps that
    can add an account by pasting a link instead of typing the key."""
    label = urllib.parse.quote(f"{issuer}:{account_name}")
    params = urllib.parse.urlencode({
        "secret": secret, "issuer": issuer, "algorithm": "SHA1",
        "digits": TOTP_DIGITS, "period": TOTP_PERIOD,
    })
    return f"otpauth://totp/{label}?{params}"


def _hotp(secret, counter):
    """RFC 4226 HOTP - the counter-based code TOTP (RFC 6238) is built on
    by simply using floor(unix_time / period) as the counter."""
    padded = secret.upper()
    padded += "=" * ((8 - len(padded) % 8) % 8)
    key = base64.b32decode(padded)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** TOTP_DIGITS)
    return str(code_int).zfill(TOTP_DIGITS)


def current_code(secret, for_time=None, step=0):
    """The code for the TOTP_PERIOD-second window at for_time (default:
    now), optionally shifted by `step` whole windows - used by verify()
    below to tolerate a little clock drift between this server and the
    user's phone."""
    t = for_time if for_time is not None else time.time()
    counter = int(t // TOTP_PERIOD) + step
    return _hotp(secret, counter)


def verify(secret, code, valid_window=1):
    """Accepts a code from the current window, or from `valid_window`
    windows immediately before/after it (default: +/- 30 seconds), to
    tolerate clock drift between this server and the user's phone -
    checked with a constant-time comparison so the response can't be
    timed to narrow down which digit is wrong."""
    code = (code or "").strip().replace(" ", "")
    if not code or not code.isdigit() or len(code) != TOTP_DIGITS:
        return False
    if not secret:
        return False
    now = time.time()
    try:
        for step in range(-valid_window, valid_window + 1):
            if hmac.compare_digest(current_code(secret, now, step), code):
                return True
    except Exception:
        return False
    return False


def generate_backup_code():
    """One recovery code, formatted like TEAM's existing temporary
    passwords (generate_temporary_password() in app.py) - uppercase with a
    dash in the middle for readability, e.g. "A1B2C-D3E4F"."""
    raw = secrets.token_hex(5).upper()
    return f"{raw[:5]}-{raw[5:]}"
