"""Tauri tray state derivation (ADR-0002, PRD §3.2 / §2.2 Flow B/C, S06-T1).

The Tauri shell's native tray icon reflects gateway health.  The Rust side only
polls ``GET /api/system/status`` + ``GET /api/queue/stats`` and feeds the raw
counts here; this module maps them to a single tray state:

- ``idle``      — components running, queue empty
- ``sending``   — queue has SENDING/QUEUED work
- ``error``     — a component is stopped or a study failed (highest priority)
- ``removable`` — USB mode + idle (safe-to-remove hint)
"""

from __future__ import annotations

__all__ = ["derive_tray_state"]


def _as_int(value: object) -> int:
    try:
        return int(value) if value is not None else 0  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


def derive_tray_state(**kwargs: object) -> str:
    """Derive the tray icon state from gateway status + queue counts.

    Priority: error > sending > removable > idle.  A stopped component or any
    failed/error study forces ``error``; active queue work forces ``sending``;
    otherwise USB mode reports ``removable`` and everything else ``idle``.
    """
    receiver = str(kwargs.get("receiver", "running"))
    forwarder = str(kwargs.get("forwarder", "running"))
    retriever = str(kwargs.get("report_retriever", "running"))
    queued = _as_int(kwargs.get("queued"))
    sending = _as_int(kwargs.get("sending"))
    error = _as_int(kwargs.get("error"))
    failed = _as_int(kwargs.get("failed"))
    usb_mode = bool(kwargs.get("usb_mode", False))

    components_stopped = any(
        c != "running" for c in (receiver, forwarder, retriever)
    )
    if components_stopped or error > 0 or failed > 0:
        return "error"

    if sending > 0 or queued > 0:
        return "sending"

    if usb_mode:
        return "removable"

    return "idle"
