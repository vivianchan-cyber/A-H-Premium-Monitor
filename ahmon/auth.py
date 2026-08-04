"""Authentication and roles for the hosted dashboard.

Users are configured entirely through the AHMON_USERS environment
variable (never in source code): a JSON list of objects

    [{"email": "you@example.com", "role": "admin",
      "password_hash": "pbkdf2_sha256$600000$<salt>$<hash>"}, …]

- Only e-mail addresses present in this list can log in (the approved
  list *is* the user list).
- Roles: "admin" (refresh data, run backfills, manage the focus list,
  see system actions) and "viewer" (read-only).
- Password hashes are salted PBKDF2-SHA256 (stdlib only). Generate one
  with:  python -m ahmon.auth hash
- No credential, hash or e-mail ever appears in code, logs or the page
  source; login state lives in the server-side Streamlit session.

Remember me: the login form offers a "keep me signed in" option that
stores an HMAC-SHA256-signed token (e-mail + expiry, no password
material) in a browser cookie for REMEMBER_DAYS days. The signature is
keyed on the signing secret *and* the user's current password hash, so
changing a password or removing the user invalidates their remembered
sessions immediately. The signing secret is AHMON_COOKIE_SECRET when
set; otherwise it is derived from the AHMON_USERS value itself, which
also works but logs every remembered device out whenever the user list
changes at all. The cookie is set by a tiny script (Streamlit cannot
set cookies server-side), so it cannot be HttpOnly — the token it
carries grants login only while its user still exists unchanged, and
it never contains a password or hash.

Local development: when AHMON_USERS is unset AND no DATABASE_URL is
configured, the app runs open as an implicit local admin (with a visible
notice). When DATABASE_URL is set, AHMON_USERS is mandatory — a hosted
instance can never silently run unauthenticated.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time

ROLES = ("admin", "viewer")
_ITERATIONS = 600_000

COOKIE_NAME = "ahmon_remember"
REMEMBER_DAYS = 30


# ------------------------------------------------------------------ hashing

def hash_password(password: str, iterations: int = _ITERATIONS) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 salt.encode(), iterations).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                        salt.encode(), int(iterations)).hex()
        return hmac.compare_digest(candidate, digest)
    except (ValueError, AttributeError):
        return False


# ------------------------------------------------------------------- users

def load_users() -> dict[str, dict]:
    """{email(lower): {"role": ..., "password_hash": ...}} from env."""
    raw = os.environ.get("AHMON_USERS", "").strip()
    if not raw:
        return {}
    users = {}
    for u in json.loads(raw):
        email = u["email"].strip().lower()
        role = u.get("role", "viewer")
        if role not in ROLES:
            raise ValueError(f"unknown role for {email!r}: {role!r}")
        users[email] = {"role": role, "password_hash": u["password_hash"]}
    return users


def authenticate(email: str, password: str) -> dict | None:
    """Returns {"email", "role"} on success, None otherwise. Constant-ish
    time for unknown users (a dummy verify runs either way)."""
    users = load_users()
    email = (email or "").strip().lower()
    record = users.get(email)
    stored = record["password_hash"] if record else hash_password("x")
    ok = verify_password(password or "", stored)
    if record and ok:
        return {"email": email, "role": record["role"]}
    return None


# -------------------------------------------------------- remember-me token

def _cookie_secret() -> str | None:
    """Signing key for remember-me tokens: AHMON_COOKIE_SECRET when set,
    else derived from the AHMON_USERS value (any user-list edit then
    invalidates all remembered sessions). None → feature unavailable."""
    explicit = os.environ.get("AHMON_COOKIE_SECRET", "").strip()
    if explicit:
        return explicit
    raw = os.environ.get("AHMON_USERS", "").strip()
    if raw:
        return hashlib.sha256(b"ahmon-remember|" + raw.encode()).hexdigest()
    return None


def _signature(email: str, expires: int, password_hash: str,
               secret: str) -> str:
    key = (secret + "|" + password_hash).encode()
    return hmac.new(key, f"{email}|{expires}".encode(),
                    hashlib.sha256).hexdigest()


def make_remember_token(email: str, days: int = REMEMBER_DAYS,
                        _now: float | None = None) -> str | None:
    """Signed token 'v1.<email-b64url>.<expiry>.<sig>' for a configured
    user, or None when the user or the signing secret is unavailable."""
    email = (email or "").strip().lower()
    record = load_users().get(email)
    secret = _cookie_secret()
    if not record or not secret:
        return None
    now = _now if _now is not None else time.time()
    expires = int(now + days * 86400)
    email_b64 = base64.urlsafe_b64encode(
        email.encode()).decode().rstrip("=")
    sig = _signature(email, expires, record["password_hash"], secret)
    return f"v1.{email_b64}.{expires}.{sig}"


def verify_remember_token(token: str | None,
                          _now: float | None = None) -> dict | None:
    """{"email", "role"} when the token is well-formed, unexpired and its
    user still exists with an unchanged password; None otherwise."""
    try:
        version, email_b64, expires_s, sig = token.split(".")
        if version != "v1":
            return None
        email = base64.urlsafe_b64decode(
            email_b64 + "=" * (-len(email_b64) % 4)).decode()
        expires = int(expires_s)
    except (AttributeError, ValueError):
        return None
    now = _now if _now is not None else time.time()
    if now > expires:
        return None
    record = load_users().get(email)
    secret = _cookie_secret()
    if not record or not secret:
        return None
    expected = _signature(email, expires, record["password_hash"], secret)
    if not hmac.compare_digest(expected, sig):
        return None
    return {"email": email, "role": record["role"]}


# --------------------------------------------------------------- streamlit

def _write_cookie(value: str, max_age: int):
    """Set/clear the remember-me cookie from the browser: Streamlit has
    no server-side Set-Cookie, so a zero-height component script writes
    it on the app's own origin. Token characters are [A-Za-z0-9._-],
    safe to interpolate. Secure keeps it HTTPS-only (browsers still
    allow it on http://localhost for development)."""
    import streamlit.components.v1 as components
    components.html(
        f"<script>window.parent.document.cookie = "
        f'"{COOKIE_NAME}={value}; Max-Age={max_age}; Path=/; '
        f'SameSite=Lax; Secure";</script>',
        height=0, width=0)


def require_login():
    """Gate the Streamlit app. Returns the logged-in user dict, or stops
    the script at a login form. Import streamlit lazily so this module
    stays usable from CLI tools and tests."""
    import streamlit as st

    from . import config

    users = load_users()
    if not users:
        if config.DATABASE_URL:
            st.error("AHMON_USERS is not configured. A hosted instance "
                     "must define the approved-user list — see "
                     "DEPLOYMENT.md.")
            st.stop()
        return {"email": "local@dev", "role": "admin", "local_dev": True}

    user = st.session_state.get("auth_user")
    if user:
        token = st.session_state.pop("_remember_pending", None)
        if token:
            _write_cookie(token, REMEMBER_DAYS * 86400)
        return user

    if st.session_state.pop("_forget_cookie", False):
        _write_cookie("", 0)                 # logged out: drop the cookie
    else:
        user = verify_remember_token(st.context.cookies.get(COOKIE_NAME))
        if user:
            st.session_state["auth_user"] = user
            return user

    st.title("A–H Premium Monitor")
    st.markdown("Please sign in. Access is limited to approved users.")
    with st.form("login"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        remember = st.checkbox(
            f"Keep me signed in on this device for {REMEMBER_DAYS} days")
        submitted = st.form_submit_button("Sign in", type="primary")
    if submitted:
        user = authenticate(email, password)
        if user is None:
            time.sleep(1.0)          # slow down brute-force attempts
            st.error("Unknown email or wrong password.")
        else:
            st.session_state["auth_user"] = user
            if remember:
                st.session_state["_remember_pending"] = \
                    make_remember_token(user["email"])
            st.rerun()
    st.stop()


def logout():
    import streamlit as st
    st.session_state.pop("auth_user", None)
    # the cookie outlives the session — without this flag the very next
    # rerun would silently sign the user straight back in from it
    st.session_state["_forget_cookie"] = True


# --------------------------------------------------------------------- CLI

def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if argv[:1] == ["hash"]:
        import getpass
        pw = getpass.getpass("Password to hash: ")
        if len(pw) < 8:
            print("Use at least 8 characters.", file=sys.stderr)
            return 1
        if pw != getpass.getpass("Repeat: "):
            print("Passwords do not match.", file=sys.stderr)
            return 1
        print(hash_password(pw))
        print("\nPut this hash into the AHMON_USERS env var, e.g.:\n"
              '[{"email": "you@example.com", "role": "admin", '
              '"password_hash": "<the hash above>"}]', file=sys.stderr)
        return 0
    print("usage: python -m ahmon.auth hash", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
