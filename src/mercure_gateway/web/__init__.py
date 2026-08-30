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
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from mercure_gateway.web.auth import require_auth

if TYPE_CHECKING:
    from mercure_gateway.config import GatewayConfig
    from mercure_gateway.spool import Spool

__all__ = ["create_app"]

_STATIC_DIR = Path(__file__).parent / "static"


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
        title="mercure-gateway API",
        version="0.1.0",
        description="REST API for mercure-gateway web admin panel",
    )

    # Store shared references on app state for route access
    app.state.config = config
    app.state.spool = spool
    app.state.config_path = str(config_path) if config_path else None

    # CORS: only the origins the Tauri webview / localhost SPA actually use.
    # Never combine allow_credentials=True with wildcard origins.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:8080",
            "http://localhost:8080",
            "tauri://localhost",
            "https://tauri.localhost",
        ],
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
