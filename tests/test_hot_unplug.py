"""TDD (S10-T4, RED): hot-unplug shutdown sequence — receiver.stop → flush → fsync → marker.

usb-dongle-gateway-spec §7.2 / sprint DoD:
1. Removal triggers the ordered shutdown sequence: the receiver stops
   accepting associations first, then the in-flight flush runs, then the
   shutdown marker is written as the last filesystem act.
2. The flush is bounded by a timeout (10 s per K10): a hung flush must not
   block removal past the deadline — the sequence proceeds (forced removal)
   and the marker is still written.
3. A flush that raises must not skip the marker write — every step after
   the first is best-effort once the device is being yanked.
4. The marker write is durable: fsync on the file (and best-effort on the
   directory) before removal is allowed, so a subsequent power loss cannot
   leave an ambiguous marker.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest

from mercure_gateway.hotplug import (
    has_shutdown_marker,
    run_shutdown_sequence,
)


def test_sequence_runs_in_order(tmp_path: Path) -> None:
    """receiver.stop → flush → shutdown marker, in that order."""
    calls: list[str] = []

    run_shutdown_sequence(
        stop_receiver=lambda: calls.append("receiver"),
        flush=lambda: calls.append("flush"),
        spool_dir=tmp_path,
    )

    assert calls == ["receiver", "flush"]
    assert has_shutdown_marker(tmp_path)


def test_flush_within_timeout_reports_completed(tmp_path: Path) -> None:
    result = run_shutdown_sequence(
        stop_receiver=lambda: None,
        flush=lambda: None,
        spool_dir=tmp_path,
        flush_timeout_sec=1.0,
    )
    assert result is True
    assert has_shutdown_marker(tmp_path)


def test_flush_timeout_forces_removal(tmp_path: Path) -> None:
    """A flush hung past the timeout must not block removal: the sequence
    completes (forced removal), the marker is still written, and the
    return value reports the flush did not finish (K10)."""
    release = threading.Event()

    def hung_flush() -> None:
        release.wait(timeout=5.0)

    try:
        result = run_shutdown_sequence(
            stop_receiver=lambda: None,
            flush=hung_flush,
            spool_dir=tmp_path,
            flush_timeout_sec=0.2,
        )
        assert result is False
        assert has_shutdown_marker(tmp_path)
    finally:
        release.set()  # unwind the flush thread


def test_raising_flush_still_writes_marker(tmp_path: Path) -> None:
    """Once the device is being yanked any step may raise OSError; the
    marker write must still happen so the next boot can classify the
    shutdown."""

    def failing_flush() -> None:
        raise OSError("device gone")

    result = run_shutdown_sequence(
        stop_receiver=lambda: None,
        flush=failing_flush,
        spool_dir=tmp_path,
    )

    assert result is True  # flush "completed" (raised, but did not hang)
    assert has_shutdown_marker(tmp_path)


def test_marker_write_is_fsynced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The marker must hit the platter before removal is allowed — fsync on
    the file (review F10: the device may vanish at any instant)."""
    fsynced: list[int] = []
    real_fsync = __import__("os").fsync
    monkeypatch.setattr(
        "mercure_gateway.hotplug.os.fsync",
        lambda fd: (fsynced.append(fd), real_fsync(fd))[1],
    )

    run_shutdown_sequence(
        stop_receiver=lambda: None,
        flush=lambda: None,
        spool_dir=tmp_path,
    )

    assert fsynced, "marker written without fsync — not durable against power loss"


def test_sequence_logs_start(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="mercure_gateway.hotplug"):
        run_shutdown_sequence(
            stop_receiver=lambda: None,
            flush=lambda: None,
            spool_dir=tmp_path,
        )
    assert any("shutdown sequence" in r.message for r in caplog.records)
