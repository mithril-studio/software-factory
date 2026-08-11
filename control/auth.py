"""Session-cookie auth for the control plane.

A single shared login gates the whole platform. Credentials live in the environment (with a
default baked in for the studio), and the password is only ever checked server-side — it never
reaches the browser. A successful login gets a signed, expiring cookie; every `/api` route
except the auth endpoints requires it.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

COOKIE_NAME = "factory_session"
SESSION_TTL = 7 * 24 * 3600  # a week

AUTH_EMAIL = os.environ.get("FACTORY_AUTH_EMAIL", "info@mithril-studio.com").strip().lower()
AUTH_PASSWORD = os.environ.get("FACTORY_AUTH_PASSWORD", "theageofmenisover")

# Signing key for the session cookie. A stable value keeps sessions alive across restarts;
# when unset we derive one from the password so it is stable yet rotates if the password changes.
_SECRET = (
    os.environ.get("FACTORY_SECRET_KEY", "").strip()
    or hashlib.sha256(f"factory-session:{AUTH_PASSWORD}".encode()).hexdigest()
).encode()

# Endpoints reachable without a session (plus everything outside /api, i.e. the SPA + assets).
PUBLIC_PATHS = {"/api/login", "/api/logout", "/api/me", "/healthz"}


def check_credentials(email: str, password: str) -> bool:
    """Constant-time comparison of both fields so neither leaks via timing."""
    email_ok = hmac.compare_digest(email.strip().lower(), AUTH_EMAIL)
    password_ok = hmac.compare_digest(password, AUTH_PASSWORD)
    return email_ok and password_ok


def _sign(expiry: int) -> str:
    return hmac.new(_SECRET, str(expiry).encode(), hashlib.sha256).hexdigest()


def issue_token() -> str:
    """A fresh session token: an expiry stamp plus its signature."""
    expiry = int(time.time()) + SESSION_TTL
    return f"{expiry}.{_sign(expiry)}"


def valid_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    stamp, _, sig = token.partition(".")
    if not stamp.isdigit():
        return False
    expiry = int(stamp)
    if expiry < time.time():
        return False
    return hmac.compare_digest(sig, _sign(expiry))


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or not path.startswith("/api")
