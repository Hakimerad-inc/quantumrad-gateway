"""Authentication for the web admin panel (product refinement §7).

Implements the ``web_ui.auth_enabled`` / ``web_ui.auth_password_hash``
configuration surface: a single shared-password session scheme with a
signed-token cookie.  When ``auth_enabled`` is false the API is open —
which is only safe because :meth:`main` refuses to bind the panel to a
non-loopback address in that mode.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, Request, Response

from mercure_gateway.config import GatewayConfig

__all__ = [
    "COOKIE_NAME",
    "create_session_token",
    "login",
    "logout",
    "verify_password",
    "require_auth",
]

_COOKIE_NAME = "mercure_session"
COOKIE_NAME = _COOKIE_NAME
_SESSION_TTL_SEC = 12 * 3600  # 12 h — a working day


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def create_session_token(secret: str) -> str:
    """Return ``<expiry>.<signature>`` for a fresh admin session."""
    expiry = int(time.time()) + _SESSION_TTL_SEC
    payload = str(expiry)
    return f"{payload}.{_sign(payload, secret)}"


def _verify_token(token: str | None, secret: str) -> bool:
    if not token:
        return False
    payload, _, signature = token.rpartition(".")
    if not payload or not signature:
        return False
    if not hmac.compare_digest(signature, _sign(payload, secret)):
        return False
    try:
        expiry = int(payload)
    except ValueError:
        return False
    return time.time() < expiry


def verify_password(password: str, password_hash: str) -> bool:
    """Verify *password* against a stored hash.

    Supports bcrypt (``$2b$...``) when ``bcrypt`` is installed and falls back
    to salted SHA-256 (``sha256$salt$hex``) otherwise, so the default install
    (no bcrypt dependency) still avoids storing the plaintext password.
    """
    if password_hash.startswith("$2"):
        try:
            import bcrypt

            return bool(bcrypt.checkpw(password.encode(), password_hash.encode()))
        except ImportError:
            return False
    parts = password_hash.split("$", 2)
    if len(parts) == 3 and parts[0] == "sha256":
        _algo, salt, expected = parts
        computed = hashlib.sha256((salt + password).encode()).hexdigest()
        return bool(hmac.compare_digest(computed, expected))
    return False


def _session_secret(config: GatewayConfig) -> str:
    """Per-process session signing secret (derived from the password hash)."""
    return config.web_ui.auth_password_hash or f"ephemeral-{secrets.token_hex(16)}"


def require_auth(request: Request) -> None:
    """FastAPI dependency enforcing web-UI authentication when enabled.

    When ``web_ui.auth_enabled`` is false this is a no-op (loopback-only
    binding is enforced by the composition root instead).
    """
    config: GatewayConfig = request.app.state.config
    if not config.web_ui.auth_enabled:
        return
    secret = _session_secret(config)
    token = request.cookies.get(_COOKIE_NAME)
    if _verify_token(token, secret):
        return
    # Allow Authorization: Bearer <token> (Tauri shell / scripting).
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer ") and _verify_token(header[7:], secret):
        return
    raise HTTPException(status_code=401, detail="authentication required")


def login(request: Request, response: Response, password: str | None) -> None:
    """Verify *password* and set the session cookie (review F5).

    When ``auth_enabled`` is false any password is accepted (the API is open
    anyway) but the cookie is still issued so the SPA login flow works
    uniformly. With auth enabled a wrong/missing password raises 401.
    """
    config: GatewayConfig = request.app.state.config
    if config.web_ui.auth_enabled:
        stored = config.web_ui.auth_password_hash
        if not password or not stored or not verify_password(password, stored):
            raise HTTPException(status_code=401, detail="invalid credentials")
    token = create_session_token(_session_secret(config))
    # ``Secure`` is set only when the panel is actually served over TLS, so
    # plain-HTTP localhost (and the starlette TestClient, which speaks HTTP)
    # still receive and replay the cookie. Under web_ui.tls_* the browser will
    # never send it over a cleartext hop.
    response.set_cookie(
        _COOKIE_NAME,
        token,
        max_age=_SESSION_TTL_SEC,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )


def logout(response: Response, secure: bool = False) -> None:
    """Clear the session cookie.

    *secure* must match the flag the cookie was set with or the browser will
    not match it for deletion — callers pass ``request.url.scheme == "https"``.
    """
    response.delete_cookie(_COOKIE_NAME, secure=secure)
