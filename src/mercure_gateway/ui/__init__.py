"""Desktop shell placeholder (PRD §5.1).

The PRD specifies a Qt (PySide6) or Tauri shell wrapping the Python core;
the exact choice is TBD pending the Phase 0 prototype (PRD §13 Q1).  This
module is deliberately a stub so the package remains importable and the
console entry point works before any UI framework is introduced.
"""

from __future__ import annotations

__all__ = ["run_desktop"]


def run_desktop() -> int:
    """Launch the desktop shell.

    Placeholder only. Once the shell framework is chosen (PySide6 baseline,
    PRD §13 Q1) this will build the tray icon, main window (queue/status/logs/
    settings/reports) and the guided first-run wizard (PRD §2.2 Flow A).
    """
    return 0
