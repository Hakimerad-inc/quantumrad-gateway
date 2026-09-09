"""Web admin panel — FastAPI backend serving REST API and SPA (product refinement §7).

The web admin provides a browser-based interface for configuration, queue management,
report viewing, and audit log access.  It runs on ``localhost:8080`` and is designed to
be wrapped by a Tauri shell (ADR-0002) or accessed directly via browser.

Communication architecture: Tauri webview loads ``http://127.0.0.1:8080`` (method 1
per ADR-0002); the SPA makes standard ``fetch()`` calls to the REST API.

Security: the API carries PHI and admin controls. All routes depend on
:func:`mercure_gateway.web.auth.require_auth` (no-op while ``auth_enabled``
is false — the composition root restricts binding to loopback in that mode).
CORS is restricted to the loopback origins the Tauri shell actually uses;
credentials are never combined with wildcard origins.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from mercure_gateway.web.auth import require_auth

if TYPE_CHECKING:
    from mercure_gateway.config import GatewayConfig
    from mercure_gateway.spool import Spool

__all__ = ["create_app"]

_STATIC_DIR = Path(__file__).parent / "static"

# Loopback origins the Tauri webview / localhost SPA actually use — the same
# set the CORS middleware allows.  A state-changing request whose Origin is
# *not* in this set is CSRF (a cross-site form/post cannot spoof loopback).
# The allow-list covers any port on loopback hosts: the SPA is served by the
# gateway itself, so a request Origin that already matches the host on any
# port is same-app, not cross-site (tests bind isolated web ports, e.g. E2E).
_ALLOWED_ORIGINS = (
    "http://127.0.0.1:8080",
    "http://localhost:8080",
    "tauri://localhost",
    "https://tauri.localhost",
)

# Hosts (any port) whose origins are accepted in addition to the exact list.
_ALLOWED_ORIGIN_HOSTS = {"127.0.0.1", "localhost"}

# Non-loopback binding is only allowed when auth is enabled (main.py enforces
# this); loopback is single-user by definition, so the CSRF origin check is a
# defense-in-depth layer, not the primary boundary.

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    # The SPA loads its JS/CSS from the same origin only.  React inline
    # ``style`` attributes need 'unsafe-inline' for styles; scripts stay
    # 'self' (no inline scripts, no eval — the Vite build emits external
    # module files only).
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'"
    ),
}


class _SecurityMiddleware:
    """Security headers + origin-based CSRF check (ASGI middleware).

    - Adds security headers (nosniff, frame deny, HSTS, CSP) to every response.
    - Rejects state-changing requests (POST/PUT/DELETE) whose ``Origin`` header
      is present but not in the loopback/tauri allow-list (CSRF).
    """

    _STATE_CHANGING = {"POST", "PUT", "DELETE", "PATCH"}

    def __init__(self, app: Any, allowed_origins: tuple[str, ...]) -> None:
        self.app = app
        self._allowed_origins = set(allowed_origins)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # CSRF: only meaningful for browser-like requests that send an Origin.
        if scope["method"] in self._STATE_CHANGING and scope["path"].startswith("/api/"):
            headers = dict(scope.get("headers", []))
            origin = headers.get(b"origin")
            if origin is not None:
                origin_str = origin.decode()
                if origin_str in self._allowed_origins:
                    origin_allowed = True
                else:
                    # Same-app loopback on a non-default port (e.g. tests on an
                    # isolated web port): host must be loopback and the scheme
                    # plain http — tauri:// and remote hosts stay rejected.
                    from urllib.parse import urlsplit

                    parts = urlsplit(origin_str)
                    origin_allowed = (
                        parts.scheme == "http"
                        and parts.hostname in _ALLOWED_ORIGIN_HOSTS
                        and parts.username is None
                        and parts.password is None
                    )
                if not origin_allowed:
                    response = JSONResponse(
                        status_code=403, content={"detail": "origin not allowed"}
                    )
                    await response(scope, receive, send)
                    return

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                for name, value in _SECURITY_HEADERS.items():
                    headers.append((name.lower().encode(), value.encode()))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


def create_app(
    config: GatewayConfig,
    spool: Spool,
    *,
    config_path: str | Path | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    The *config* and *spool* instances are shared with the gateway core;
    the web layer reads them but does not own their lifecycle.
    """
    from mercure_gateway.web.routes import auth_router, router

    app = FastAPI(
        title="QuantumRAD Gateway API",
        version="0.1.0",
        description="REST API for the QuantumRAD Gateway web admin panel",
    )

    # Store shared references on app state for route access
    app.state.config = config
    app.state.spool = spool
    app.state.config_path = str(config_path) if config_path else None

    # Security middleware FIRST (runs outermost): headers on every response,
    # CSRF origin check before the CORS handling.
    app.add_middleware(_SecurityMiddleware, allowed_origins=_ALLOWED_ORIGINS)

    # CORS: only the origins the Tauri webview / localhost SPA actually use.
    # Never combine allow_credentials=True with wildcard origins.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(_ALLOWED_ORIGINS),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["*"],
    )

    # API routes. require_auth is applied ONLY to the admin router — the
    # auth router (login/logout) must be reachable before a session exists.
    # (App-level dependencies would also wrap login and lock it behind 401.)
    app.include_router(auth_router, prefix="/api")
    app.include_router(router, prefix="/api", dependencies=[Depends(require_auth)])

    # Serve SPA static files (catch-all must be last)
    if _STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="spa")
    else:
        @app.get("/{path:path}")
        async def _spa_fallback(path: str) -> dict[str, str]:
            return {
                "message": "SPA not built. Run 'npm run build' in web/static/.",
                "docs": "/docs",
            }

    return app
