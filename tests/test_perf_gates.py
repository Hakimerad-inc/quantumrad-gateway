"""S09-T2 (RED): Perf gates (§5.6, K8).

Verifies the gate script measures within budget: forwarding begins within
US-03's 2 s window, and concurrent throughput clears the K8 floor.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _run_gate(*extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPTS / "check_perf_gates.py"), *extra],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_perf_gate_exits_zero() -> None:
    """The perf gate passes (exit 0) within the latency/throughput budgets."""
    result = _run_gate()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout


def test_perf_gate_reports_latency() -> None:
    """The gate prints the forwarding latency measurement."""
    result = _run_gate()
    assert "forwarding begins in" in result.stdout


def test_perf_gate_reports_throughput() -> None:
    """The gate prints the concurrent throughput measurement (K8)."""
    result = _run_gate()
    assert "concurrent throughput" in result.stdout


@pytest.mark.integration
def test_real_io_mode_passes() -> None:
    """P1-7: the ``--real-io`` mode runs the gate against real handler I/O.

    The synthetic default is what CI runs per push; this is the integration
    path. It is marked so a constrained runner can deselect it, but it is
    fast enough (a loopback ACK server, a tmpdir payload) to run anywhere.
    """
    result = _run_gate("--real-io")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "(real-I/O)" in result.stdout


def test_real_io_handler_performs_real_disk_and_socket_io() -> None:
    """P1-7: the real handler must actually touch disk and a socket.

    This is the core assertion of the fix — the synthetic handler increments
    a counter and does no I/O, so it cannot see a regression in handler cost.
    Proving the real handler writes a file and ships bytes over a real socket
    is what makes the mode worth having. Asserted via artifacts on disk and
    the server's byte counter, not via the handler's own counter.
    """
    sys.path.insert(0, str(_SCRIPTS))
    try:
        import check_perf_gates
    finally:
        sys.path.pop(0)

    with tempfile.TemporaryDirectory(prefix="perf-gate-test-") as raw_dir:
        out_dir = Path(raw_dir)
        server = check_perf_gates._AckServer()
        try:
            server.start()
            handler = check_perf_gates._RealIoHandler(out_dir=out_dir, server=server)

            result = handler.deliver(task=None, spool_dir=out_dir)

            assert result.ok, result.error
            # Disk: exactly one payload file, of the documented size.
            written = list(out_dir.glob("perf-*.dicom"))
            assert len(written) == 1
            assert written[0].stat().st_size == check_perf_gates._RealIoHandler.PAYLOAD_BYTES
            # Socket: the server received what was sent, over a real TCP
            # connection (an unconnected handler would leave this at 0).
            assert server.bytes_received == check_perf_gates._RealIoHandler.PAYLOAD_BYTES
        finally:
            server.stop()


def test_real_io_handler_reports_io_failures_as_delivery_failures() -> None:
    """A dead socket must surface as ``ok=False``, not raise.

    The point of wiring real I/O in is to see its failures; a handler that
    blew up on a broken peer would take the forwarder's worker with it
    instead of the study retrying.
    """
    sys.path.insert(0, str(_SCRIPTS))
    try:
        import check_perf_gates
    finally:
        sys.path.pop(0)

    # A server that accepts the connection and immediately closes never sends
    # the ACK, so the handler sees an empty recv — a real failure shape.
    import socket
    import threading

    reject = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    reject.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    reject.bind(("127.0.0.1", 0))
    reject.listen(1)
    port = reject.getsockname()[1]
    stop = threading.Event()

    def _hang_up() -> None:
        reject.settimeout(0.5)
        while not stop.is_set():
            try:
                conn, _ = reject.accept()
            except TimeoutError:
                continue
            with conn:
                pass  # close immediately: no ACK

    thread = threading.Thread(target=_hang_up, daemon=True)
    thread.start()

    class _Port:
        """The handler only reads ``.port`` off its server."""

        def __init__(self, port: int) -> None:
            self.port = port

    with tempfile.TemporaryDirectory(prefix="perf-gate-test-") as raw_dir:
        out_dir = Path(raw_dir)
        try:
            handler = check_perf_gates._RealIoHandler(out_dir=out_dir, server=_Port(port))
            result = handler.deliver(task=None, spool_dir=out_dir)
        finally:
            stop.set()
            thread.join(timeout=5.0)
            reject.close()

    assert not result.ok
    assert result.error
