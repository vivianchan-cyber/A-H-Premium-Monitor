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

Local development: when AHMON_USERS is unset AND no DATABASE_URL is
configured, the app runs open as an implicit local admin (with a visible
notice). When DATABASE_URL is set, AHMON_USERS is mandatory — a hosted
instance can never silently run unauthenticated.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import time

ROLES = ("admin", "viewer")
_ITERATIONS = 600_000


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


# --------------------------------------------------------------- streamlit

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
        return user

    st.title("A–H Premium Monitor")
    st.markdown("Please sign in. Access is limited to approved users.")
    with st.form("login"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")
    if submitted:
        user = authenticate(email, password)
        if user is None:
            time.sleep(1.0)          # slow down brute-force attempts
            st.error("Unknown email or wrong password.")
        else:
            st.session_state["auth_user"] = user
            st.rerun()
    st.stop()


def logout():
    import streamlit as st
    st.session_state.pop("auth_user", None)


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
