"""Desktop shell wrapper (ADR-0002).

Per ADR-0002 (Accepted), the shell is **Tauri wrapping the FastAPI web admin
panel** — the web layer (``mercure_gateway.web``) is the UI; Tauri provides
the native tray, window and auto-start. This module remains a thin launcher
so the package stays importable independent of the Rust/Tauri side.
"""

from __future__ import annotations

__all__ = ["run_desktop"]


def run_desktop() -> int:
    """Launch the gateway with the web admin panel (the Tauri shell target).

    The Tauri shell (see ``docs/adr/ADR-0002-desktop-shell-architecture.md``)
    spawns this process and loads ``http://127.0.0.1:8080``.
    """
    from mercure_gateway.main import main

    return main(["--web"])
