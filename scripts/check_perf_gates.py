#!/usr/bin/env python
"""S09-T2 perf gates — forwarding latency + concurrent throughput (K8).

Measures the time from claim to the first delivery attempt (US-03 ≤2 s
"begin") and the concurrent forwarding throughput (K8).  These gates run in
CI and fail the build on regression.

Two modes (review P1-7):

- **synthetic** (default) — a scripted handler that records the claim time.
  Fast; this is what CI runs on every push. It measures the forwarder's
  queue/claim/route machinery only: the handler itself does *no* I/O, so a
  regression in handler cost is invisible to it.
- **real** (``--real-io``) — the handler performs actual disk and socket I/O
  per delivery: a real ``fsync`` on a written file plus a real TCP round-trip
  to a loopback server. This is the mode that can see handler regressions.
  It is the integration path (``just perf-gates-real``), not the per-push CI
  gate, because it is slower and its floor is machine-dependent.

The K8 floor in the synthetic mode is deliberately a *sanity* floor, not a
performance target. Measured on real ext4 (review P1-4): 25 concurrent
associations yield 0.69 inst/s each at 1338 ms median latency — 25× the
offered load buys 1.24× throughput. The receiver, not the forwarder, is the
binding constraint; this gate guards the forwarder's own bookkeeping.

Usage:
    python scripts/check_perf_gates.py            # synthetic (CI default)
    python scripts/check_perf_gates.py --real-io  # real handler I/O

Returns exit code 0 (pass) or 1 (fail) and prints results.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

LATENCY_BUDGET_SEC = 2.0  # US-03: forwarding begins within 2 s of claim
CONCURRENT_TARGET_ITEMS_PER_SEC = 5  # K8: sanity throughput floor (CI-safe)
# The real-I/O floor is lower because a delivery now pays for an fsync plus a
# TCP round-trip; the synthetic floor would be a lie about what is possible.
REAL_IO_TARGET_ITEMS_PER_SEC = 2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _StopwatchHandler:
    """Destination handler that records when delivery first began.

    In synthetic mode this is the whole handler: a counter and a timestamp.
    That is enough to gate the queue/claim/route path and nothing more —
    review P1-7's point is that it must not be mistaken for a delivery
    measurement.
    """

    def __init__(self) -> None:
        self.first_begin_sec: float | None = None
        self.deliveries = 0
        self._start = time.perf_counter()

    def deliver(self, task: Any, spool_dir: Path) -> Any:
        from mercure_gateway.forwarder import DeliveryResult

        if self.first_begin_sec is None:
            self.first_begin_sec = time.perf_counter() - self._start
        self.deliveries += 1
        return DeliveryResult(ok=True)


class _AckServer:
    """A loopback TCP server that reads a payload and ACKs it.

    This is the "actual socket" half of the real-I/O mode. Real delivery
    transports (DIMSE, SFTP, HTTPS) all pay a connect + send + wait-for-ACK
    round trip; a loopback server reproduces that cost shape without an
    external PACS, so the mode stays deterministic and dependency-free.
    """

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self.bytes_received = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="perf-gate-ack", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _serve(self) -> None:
        self._sock.settimeout(0.25)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            with conn:
                try:
                    total = 0
                    while True:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                    conn.sendall(b"ok")
                except OSError:
                    pass
                self.bytes_received += total

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)
        self._sock.close()


class _RealIoHandler(_StopwatchHandler):
    """A handler that performs real disk + socket I/O per delivery.

    Every delivery writes a payload file, ``fsync``s it, ships the bytes over
    a real TCP connection, and waits for the ACK. If this handler's cost
    regresses, the real-I/O gate sees it; the synthetic one cannot.
    """

    PAYLOAD_BYTES = 4096

    def __init__(self, *, out_dir: Path, server: _AckServer) -> None:
        super().__init__()
        self._out_dir = out_dir
        self._server = server

    def deliver(self, task: Any, spool_dir: Path) -> Any:
        from mercure_gateway.forwarder import DeliveryResult

        # US-03 is "forwarding *begins*" — the attempt, not its completion —
        # so the timestamp is taken before the I/O, as in the base class.
        if self.first_begin_sec is None:
            self.first_begin_sec = time.perf_counter() - self._start

        # Disk: a real write plus a real fsync, as every durable transport
        # does. mkstemp keeps the path unique without trusting task identity.
        fd, tmp_path = tempfile.mkstemp(prefix="perf-", suffix=".dicom", dir=str(self._out_dir))
        payload = b"x" * self.PAYLOAD_BYTES
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            return DeliveryResult(ok=False, error=str(exc))

        # Network: a real connect/send/recv round trip to the loopback ACK
        # server. connect() is where a hung peer would block, so a socket
        # timeout bounds it exactly as the real transports do (review P1-12).
        try:
            with socket.create_connection(("127.0.0.1", self._server.port), timeout=10) as sock:
                sock.sendall(payload)
                # Shutting down the write side is what tells the server the
                # payload is complete; recv then waits for its ACK.
                sock.shutdown(socket.SHUT_WR)
                ack = sock.recv(2)
            if ack != b"ok":
                return DeliveryResult(ok=False, error=f"unexpected ACK {ack!r}")
        except OSError as exc:
            return DeliveryResult(ok=False, error=str(exc))

        self.deliveries += 1
        return DeliveryResult(ok=True)


def _seed_studies(spool: Any, count: int) -> None:
    from mercure_gateway.config import DICOMDestination

    target = DICOMDestination(
        name="folder",
        host="pacs.local",
        port=104,
        aet_target="PACS",
    )
    for i in range(count):
        study_id = spool.receive(f"1.2.840.1.{i}", accession=f"ACC-{i:05d}", modality="CT")
        spool.enqueue(study_id, [target])


def _make_handler(real_io: bool, *, out_dir: Any) -> tuple[_StopwatchHandler, _AckServer | None]:
    """The handler for this run, and the server to stop afterwards.

    Returns ``(handler, ack_server)`` where ``ack_server`` is ``None`` in
    synthetic mode — the caller stops it in a ``finally``.
    """
    if not real_io:
        return _StopwatchHandler(), None
    server = _AckServer()
    server.start()
    return _RealIoHandler(out_dir=out_dir, server=server), server


def measure_latency(real_io: bool = False, *, out_dir: Any = None) -> float:
    """Return seconds from queueing a study to first delivery attempt."""
    from mercure_gateway.config import default_config
    from mercure_gateway.forwarder import Forwarder
    from mercure_gateway.spool import Spool
    from mercure_gateway.spool.db import mem_database

    db = mem_database()
    spool = Spool(db)
    cfg = default_config()
    cfg.forwarding.queue_poll_interval_ms = 5
    handler, server = _make_handler(real_io, out_dir=out_dir)

    try:
        forwarder = Forwarder(cfg, spool)
        forwarder.register_handler("dicom", handler)
        _seed_studies(spool, 1)
        forwarder.process_once()
        forwarder.stop()
    finally:
        if server is not None:
            server.stop()

    assert handler.first_begin_sec is not None, "no delivery attempted"
    return handler.first_begin_sec


def measure_throughput(real_io: bool = False, *, out_dir: Any = None, count: int = 100) -> float:
    """Return items delivered per second across a batch."""
    from mercure_gateway.config import default_config
    from mercure_gateway.forwarder import Forwarder
    from mercure_gateway.spool import Spool
    from mercure_gateway.spool.db import mem_database

    db = mem_database()
    spool = Spool(db)
    cfg = default_config()
    handler, server = _make_handler(real_io, out_dir=out_dir)

    try:
        forwarder = Forwarder(cfg, spool)
        forwarder.register_handler("dicom", handler)
        _seed_studies(spool, count)

        start = time.perf_counter()
        while handler.deliveries < count:
            forwarder.process_once(limit=10)
        elapsed = time.perf_counter() - start
        forwarder.stop()
    finally:
        if server is not None:
            server.stop()

    return count / max(elapsed, 1e-9)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # P1-7: the real-I/O mode is opt-in. It is slower and its floor depends on
    # the host's disk and loopback, so per-push CI keeps the synthetic gate
    # and the integration path runs this one.
    parser.add_argument(
        "--real-io",
        action="store_true",
        help="deliver through a handler that performs real fsync + socket I/O",
    )
    args = parser.parse_args(argv)

    ok = True
    out_dir: Any = None
    if args.real_io:
        out_dir = Path(tempfile.mkdtemp(prefix="perf-gate-"))

    try:
        label = "real-I/O" if args.real_io else "synthetic"
        floor = REAL_IO_TARGET_ITEMS_PER_SEC if args.real_io else CONCURRENT_TARGET_ITEMS_PER_SEC

        latency = measure_latency(args.real_io, out_dir=out_dir)
        status = "PASS" if latency <= LATENCY_BUDGET_SEC else "FAIL"
        ok = ok and status == "PASS"
        print(
            f"{status} ({label}): forwarding begins in {latency*1000:.0f} ms "
            f"(budget {LATENCY_BUDGET_SEC*1000:.0f} ms)"
        )

        throughput = measure_throughput(args.real_io, out_dir=out_dir)
        status = "PASS" if throughput >= floor else "FAIL"
        ok = ok and status == "PASS"
        print(
            f"{status} ({label}): concurrent throughput {throughput:.1f} items/s "
            f"(floor {floor:.0f})"
        )
    finally:
        if out_dir is not None:
            shutil.rmtree(out_dir, ignore_errors=True)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
