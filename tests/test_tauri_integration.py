"""S06-T1 (RED): Tauri tray state derivation (ADR-0002, PRD §3.2 / §2.2 Flow B/C).

The Tauri shell's native tray icon shows idle/sending/error per Flow B/C.  The
tray state is derived from the gateway's queue + system status so the Rust
shell only needs to poll one endpoint — the derivation logic lives in Python
where it is RED-testable.

Tray states (Flow B/C):
- ``idle``     — components running, queue empty
- ``sending``  — queue has active SENDING/QUEUED work or components starting
- ``error``    — component stopped or a study failed
- ``removable``— USB mode: spool idle + on removable media (safe to unplug)
"""

from __future__ import annotations

from mercure_gateway.tray import derive_tray_state

# ══════════════════════════════════════════════════════════════════════
# Idle
# ══════════════════════════════════════════════════════════════════════

def test_idle_when_running_and_empty() -> None:
    state = derive_tray_state(
        receiver="running",
        forwarder="running",
        report_retriever="running",
        queued=0,
        sending=0,
        sent=0,
        error=0,
        failed=0,
    )
    assert state == "idle"


# ══════════════════════════════════════════════════════════════════════
# Sending
# ══════════════════════════════════════════════════════════════════════

def test_sending_when_queue_active() -> None:
    state = derive_tray_state(
        receiver="running",
        forwarder="running",
        report_retriever="running",
        queued=5,
        sending=2,
        sent=0,
        error=0,
        failed=0,
    )
    assert state == "sending"


def test_sending_when_queued_pending() -> None:
    state = derive_tray_state(
        receiver="running",
        forwarder="running",
        report_retriever="running",
        queued=3,
        sending=0,
        sent=0,
        error=0,
        failed=0,
    )
    assert state == "sending"


# ══════════════════════════════════════════════════════════════════════
# Error
# ══════════════════════════════════════════════════════════════════════

def test_error_when_forwarder_stopped() -> None:
    state = derive_tray_state(
        receiver="running",
        forwarder="stopped",
        report_retriever="running",
        queued=0,
        sending=0,
        sent=0,
        error=0,
        failed=0,
    )
    assert state == "error"


def test_error_when_receiver_stopped() -> None:
    state = derive_tray_state(
        receiver="stopped",
        forwarder="running",
        report_retriever="running",
        queued=0,
        sending=0,
        sent=0,
        error=0,
        failed=0,
    )
    assert state == "error"


def test_error_when_study_failed() -> None:
    state = derive_tray_state(
        receiver="running",
        forwarder="running",
        report_retriever="running",
        queued=0,
        sending=0,
        sent=0,
        error=1,
        failed=1,
    )
    assert state == "error"


# ══════════════════════════════════════════════════════════════════════
# Removable (USB mode)
# ══════════════════════════════════════════════════════════════════════

def test_removable_when_usb_idle() -> None:
    state = derive_tray_state(
        receiver="running",
        forwarder="running",
        report_retriever="running",
        queued=0,
        sending=0,
        sent=0,
        error=0,
        failed=0,
        usb_mode=True,
    )
    assert state == "removable"


def test_sending_beats_removable_when_active() -> None:
    """Active queue takes priority over the safe-to-remove hint."""
    state = derive_tray_state(
        receiver="running",
        forwarder="running",
        report_retriever="running",
        queued=1,
        sending=0,
        sent=0,
        error=0,
        failed=0,
        usb_mode=True,
    )
    assert state == "sending"


# ══════════════════════════════════════════════════════════════════════
# Error beats everything
# ══════════════════════════════════════════════════════════════════════

def test_error_beats_sending_and_removable() -> None:
    state = derive_tray_state(
        receiver="running",
        forwarder="running",
        report_retriever="stopped",
        queued=5,
        sending=2,
        sent=0,
        error=0,
        failed=1,
        usb_mode=True,
    )
    assert state == "error"
