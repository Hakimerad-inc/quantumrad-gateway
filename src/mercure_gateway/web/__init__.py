"""Web admin panel — FastAPI backend serving REST API and SPA (product refinement §7).

The web admin provides a browser-based interface for configuration, queue management,
report viewing, and audit log access.  It runs on ``localhost:8080`` and is designed to
be wrapped by a Tauri shell (ADR-0002) or accessed directly via browser.

Communication architecture: Tauri webview loads ``http://127.0.0.1:8080`` (method 1
per ADR-0002); the SPA makes standard ``fetch()`` calls to the REST API.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

if TYPE_CHECKING:
    from mercure_gateway.config import GatewayConfig
    from mercure_gateway.spool import Spool

__all__ = ["create_app"]

_STATIC_DIR = Path(__file__).parent / "static"


def create_app(config: GatewayConfig, spool: Spool) -> FastAPI:
    """Create and configure the FastAPI application.

    The *config* and *spool* instances are shared with the gateway core;
    the web layer reads them but does not own their lifecycle.
    """
    from mercure_gateway.web.routes import router

    app = FastAPI(
        title="mercure-gateway API",
        version="0.1.0",
        description="REST API for mercure-gateway web admin panel",
    )

    # Store shared references on app state for route access
    app.state.config = config
    app.state.spool = spool

    # CORS: allow Tauri webview and any local origin
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # API routes
    app.include_router(router, prefix="/api")

    # Serve SPA static files (catch-all must be last)
    if _STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="spa")
    else:
        @app.get("/{path:path}")
        async def _spa_fallback(path: str) -> dict[str, str]:
            return {"message": "SPA not built. Run 'npm run build' in web/static/.", "docs": "/docs"}

    return app
