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
    "hash_password",
    "login",
    "logout",
    "verify_password",
    "require_auth",
]

_COOKIE_NAME = "mercure_session"
COOKIE_NAME = _COOKIE_NAME
_SESSION_TTL_SEC = 12 * 3600  # 12 h — a working day

# PBKDF2 parameters. 200k iterations of SHA-256 is ~60 ms on commodity
# hardware — deliberate: a shared-password scheme has no per-user cost
# budget to hide behind, and the panel is a single-user surface, so the
# latency is imperceptible while the offline cost is not.
_PBKDF2_ITERATIONS = 200_000
_PBKDF2_HASH = "sha256"
_SALT_BYTES = 16


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


def hash_password(password: str, *, iterations: int = _PBKDF2_ITERATIONS) -> str:
    """Return a stdlib PBKDF2 hash of *password* (``pbkdf2$<iters>$<salt>$<dk>``).

    This is the scheme new hashes are created with. It deliberately uses only
    the standard library: the previous bcrypt branch (``$2b$...``) depended on
    a module that is not a declared dependency of this project, so a hash
    created where bcrypt was importable could become **unverifiable** later —
    the failure mode is a permanent lockout of the admin panel (review P0-8).
    PBKDF2 cannot be missing from the interpreter that created the hash.
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(
        _PBKDF2_HASH, password.encode(), salt, iterations
    )
    return f"pbkdf2${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    """Verify *password* against a stored hash.

    Three formats are accepted:

    * ``pbkdf2$<iters>$<salt hex>$<dk hex>`` — created by :func:`hash_password`;
      iterations are read from the stored string so an old hash stays
      verifiable after the default is raised.
    * ``sha256$salt$hex`` — the only format that exists in deployed configs
      today. Kept so the change does not lock out any operator.
    * ``$2b$...`` (bcrypt) — verified when the module happens to be installed,
      so a hash created under the old code path still works. Never created
      here, and its ``ImportError`` branch returns False rather than raising:
      an unverifiable hash must not become an unhandled 500 on the login path.
    """
    if password_hash.startswith("$2"):
        try:
            import bcrypt

            return bool(bcrypt.checkpw(password.encode(), password_hash.encode()))
        except ImportError:
            return False
    parts = password_hash.split("$", 3)
    if len(parts) == 4 and parts[0] == "pbkdf2":
        _algo, iters, salt_hex, expected = parts
        try:
            iterations = int(iters)
            salt_bytes = bytes.fromhex(salt_hex)
        except ValueError:
            return False
        dk = hashlib.pbkdf2_hmac(
            _PBKDF2_HASH, password.encode(), salt_bytes, iterations
        )
        return bool(hmac.compare_digest(dk.hex(), expected))
    parts = password_hash.split("$", 2)
    if len(parts) == 3 and parts[0] == "sha256":
        _algo, salt, expected = parts
        computed = hashlib.sha256((salt + password).encode()).hexdigest()
        return bool(hmac.compare_digest(computed, expected))
    return False


def _session_secret(request: Request) -> str:
    """Per-process session signing secret.

    Deliberately **not** derived from the password hash: any config save that
    round-trips ``web_ui.auth_password_hash`` would otherwise invalidate every
    live session (including the operator's own, mid-edit). The secret is minted
    once per process in :func:`mercure_gateway.web.create_app` and lives on
    ``app.state``. The hash-derived fallback only covers an app assembled
    without ``create_app`` (tests that mount the router directly).
    """
    secret: str | None = getattr(request.app.state, "session_secret", None)
    if secret:
        return secret
    config: GatewayConfig = request.app.state.config
    return config.web_ui.auth_password_hash or f"ephemeral-{secrets.token_hex(16)}"


def require_auth(request: Request) -> None:
    """FastAPI dependency enforcing web-UI authentication when enabled.

    When ``web_ui.auth_enabled`` is false this is a no-op (loopback-only
    binding is enforced by the composition root instead).
    """
    config: GatewayConfig = request.app.state.config
    if not config.web_ui.auth_enabled:
        return
    secret = _session_secret(request)
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
    token = create_session_token(_session_secret(request))
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
