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

import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from mercure_gateway.web.auth import require_auth

if TYPE_CHECKING:
    from mercure_gateway.config import GatewayConfig, WebUIConfig
    from mercure_gateway.disk import DiskMonitor
    from mercure_gateway.spool import Spool

__all__ = ["create_app"]

_STATIC_DIR = Path(__file__).parent / "static"

# Loopback hostnames the panel answers on regardless of the configured bind
# address: the Tauri webview and a browser both address it as 127.0.0.1 or
# localhost, and a config bound to 0.0.0.0/:: still serves those (off-loopback
# binding requires auth — main._enforce_bind_security refuses otherwise).
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost")

# Tauri's custom schemes for the desktop shell (ADR-0002) — added verbatim
# because they are not http(s) URLs and so cannot be built from the config.
_TAURI_ORIGINS = ("tauri://localhost", "https://tauri.localhost")

# Non-loopback binding is only allowed when auth is enabled (main.py enforces
# this); loopback is single-user by definition, so the CSRF origin check is a
# defense-in-depth layer, not the primary boundary.

# Headers sent on every response regardless of transport. HSTS is NOT here:
# it is emitted conditionally, only when the request arrived over TLS —
# browsers ignore it on http://, and claiming it unconditionally over plain
# HTTP is a documented-control-not-implemented finding (ADR-0007).
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
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
        # Report PDFs render in an iframe as a ``data:`` URL (the report
        # viewer builds one from the fetched bytes rather than serving a
        # navigable same-origin document).  Without an explicit ``frame-src``
        # the iframe falls back to ``default-src 'self'``, which blocks
        # ``data:`` — and ``object-src 'none'`` removes the <embed> fallback —
        # so the PDF path silently renders blank.
        "frame-src 'self' data:; "
        "base-uri 'self'; "
        "frame-ancestors 'none'"
    ),
}

_HSTS_VALUE = "max-age=31536000; includeSubDomains"


def _bracket_host(host: str) -> str:
    """Bracket an IPv6 literal for use in an origin (``::1`` → ``[::1]``)."""
    return f"[{host}]" if ":" in host else host


def _allowed_origins(web_ui: WebUIConfig) -> tuple[str, ...]:
    """Origins a same-app request legitimately carries, for CORS *and* CSRF.

    Built from ``web_ui``: the hostnames the panel is served on, at the port
    it is actually bound to, under the scheme it is served over.  An earlier
    version hardcoded ``:8080`` *and* accepted any port on a loopback host, so
    any other localhost service (or a cross-origin one on a free port) counted
    as same-app.  The port is now pinned to ``web_ui.port`` — the tests and
    E2E set it in the config, so an isolated web port stays allowed — and the
    Tauri schemes are appended verbatim.
    """
    schemes: tuple[str, ...] = ("http", "https") if web_ui.tls_cert_file else ("http",)
    origins = {
        f"{scheme}://{_bracket_host(host)}:{web_ui.port}": None
        for scheme in schemes
        for host in (web_ui.host, *_LOOPBACK_HOSTS)
    }
    return (*origins, *_TAURI_ORIGINS)


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
        # The allow-list is exact (host *and* port, built from web_ui) — a
        # cross-site form/post cannot forge it, and a *different* service on
        # the same loopback host is cross-origin, not same-app.
        if scope["method"] in self._STATE_CHANGING and scope["path"].startswith("/api/"):
            headers = dict(scope.get("headers", []))
            origin = headers.get(b"origin")
            if origin is not None and origin.decode() not in self._allowed_origins:
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
                # HSTS only over TLS — browsers ignore it on http://, and
                # claiming it unconditionally over plain HTTP is the
                # documented-control-not-implemented finding (ADR-0007).
                if scope.get("scheme") == "https":
                    headers.append(
                        (b"strict-transport-security", _HSTS_VALUE.encode())
                    )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


def create_app(
    config: GatewayConfig,
    spool: Spool,
    *,
    config_path: str | Path | None = None,
    disk_monitor: DiskMonitor | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    The *config* and *spool* instances are shared with the gateway core;
    the web layer reads them but does not own their lifecycle.  *disk_monitor*
    is optional because the web app is also built standalone in tests and by
    ``--write-default-config`` paths; when present its purge counters reach the
    metrics route, which is the only place a bounded-but-starving purge loop is
    visible (review P0-11).
    """
    from mercure_gateway import __version__
    from mercure_gateway.web.routes import auth_router, router

    # The version an API consumer reads (and the codegen input) is the
    # product's, not a literal left over from the scaffold — a hardcoded
    # "0.1.0" here is the one mirror sync_version.py could not see, and it
    # was the one that shipped (review P1-8).
    # The schema and the interactive docs are an unauthenticated map of every
    # admin endpoint — PHI, credentials, start/stop controls.  When the panel
    # requires a login they are removed entirely: /openapi.json, /docs and
    # /redoc would otherwise hand the whole surface to anyone who can reach
    # the port.  With auth off the panel is loopback-only (main.py enforces
    # it), so the docs stay reachable for local development.
    _docs_off = config.web_ui.auth_enabled
    app = FastAPI(
        title="QuantumRAD Gateway API",
        version=__version__,
        description="REST API for the QuantumRAD Gateway web admin panel",
        docs_url=None if _docs_off else "/docs",
        redoc_url=None if _docs_off else "/redoc",
        openapi_url=None if _docs_off else "/openapi.json",
    )

    # Store shared references on app state for route access
    app.state.config = config
    app.state.spool = spool
    app.state.config_path = str(config_path) if config_path else None
    app.state.disk_monitor = disk_monitor
    # Session signing secret, minted per process and deliberately independent of
    # the password hash: a config save that round-trips the hash must not log
    # out every live operator (review P0-8). Single-worker uvicorn is the
    # documented deployment; a second worker would mint its own secret and
    # invalidate the other's cookies.
    app.state.session_secret = secrets.token_hex(32)
    # Snapshot the config the running components were constructed with. The
    # receiver/forwarder hold their own config refs captured at construction,
    # so a saved change only takes effect after a process restart; comparing
    # this snapshot to the current config is the *server-side* truth for "is a
    # restart pending", which survives a page reload (the client-side flag does
    # not — see SystemStatus.config_pending_restart).
    app.state.startup_config_json = config.model_dump_json()

    # Login attempt tracker for the rate limiter (review P1-17). Per-app state
    # so every TestClient gets a fresh bucket.
    from mercure_gateway.web.ratelimit import attach_tracker

    attach_tracker(app)

    origins = _allowed_origins(config.web_ui)

    # Middleware order: Starlette runs user middleware in REVERSE registration
    # order, so the middleware added LAST is the OUTERMOST.  CORS is therefore
    # registered first and the security middleware second — putting the
    # security middleware outside CORS, where it sees every response including
    # the preflight replies CORSMiddleware answers without calling the app
    # (registering it first, as an earlier version did, made it the *innermost*
    # layer, so an OPTIONS preflight went back to the browser with no CSP).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["*"],
    )

    # Security middleware LAST (runs outermost): headers on every response, and
    # the CSRF origin check before any route is reached.
    app.add_middleware(_SecurityMiddleware, allowed_origins=origins)

    # API routes. require_auth is applied ONLY to the admin router — the
    # auth router (login/logout) must be reachable before a session exists.
    # (App-level dependencies would also wrap login and lock it behind 401.)
    app.include_router(auth_router, prefix="/api")
    app.include_router(router, prefix="/api", dependencies=[Depends(require_auth)])

    # Serve SPA static files (catch-all must be last)
    if _STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="spa")
    else:
        _docs_reachable = not _docs_off

        @app.get("/{path:path}")
        async def _spa_fallback(path: str) -> dict[str, str]:
            # The SPA sources are web/ (vite.config.ts writes the bundle into
            # src/mercure_gateway/web/static/), not web/static/ — the old
            # message pointed at a directory that does not exist.
            return {
                "message": (
                    "SPA not built. Run 'npm run build' in the web/ directory "
                    "(the Vite build writes src/mercure_gateway/web/static/)."
                ),
                "docs": "/docs" if _docs_reachable else "/api/system/health",
            }

    return app
