#!/usr/bin/env python
"""S09-T2 perf gates — forwarding latency + concurrent throughput (K8).

Measures the time from claim to the first delivery attempt (US-03 ≤2 s
"begin") and the concurrent forwarding throughput (K8).  These gates run in
CI and fail the build on regression.

Two modes (review P1-7 — the throughput gate used to measure a fake):

* default (``python scripts/check_perf_gates.py``) — the scripted fake
  handler increments a counter and returns ``ok=True`` with **no I/O**.
  Fast and stable enough for every push, and it still catches a regression
  in claim latency or queue drain — but it proves nothing about real
  throughput: 5 items/s of counter increments is a statement about a while
  loop, not about the forwarder.
* ``--real`` — the same batch delivered by the real :class:`DICOMHandler`
  through an actual TCP socket to a local C-STORE SCP, reading real DICOM
  files off a real (temp) spool.  This is the gate that means something;
  it is marked ``slow``/``integration`` in ``tests/test_perf_gates.py`` and
  runs on the integration path, not on every push.

The CI default is deliberately the fast mode: on a loaded free-tier runner
a socket-bound measurement has enough variance to flap, and a flaky gate is
worse than a narrow one.  ``--real`` is the number to watch on a quiet
machine and in the nightly / integration run.

Measured 2026-09-19 (dev ext4, quiet box, 3 runs): synthetic 2442 items/s
vs real 8.1–8.6 items/s — roughly **300×** apart.  The fake measurement was
not a loose approximation of throughput; it was measuring something else
entirely.  That gap is the whole point of P1-7.

The real number is also why the shared floor of 5 items/s stays: it is a
sanity floor, not a target, and even on a quiet box the real measurement
clears it by under 2×.  A regression there is a real one; a flap on a
loaded runner is a CI-timing artifact — which is why ``--real`` is off the
per-push path.

Usage:
    python scripts/check_perf_gates.py          # fast synthetic (CI default)
    python scripts/check_perf_gates.py --real   # real socket + disk I/O

Returns exit code 0 (pass) or 1 (fail) and prints results.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

LATENCY_BUDGET_SEC = 2.0  # US-03: forwarding begins within 2 s of claim
CONCURRENT_TARGET_ITEMS_PER_SEC = 5  # K8: sanity throughput floor (CI-safe)

# Real-mode batch size: each study opens its own association, so this is also
# the number of real TCP connections. Kept equal to the synthetic batch so
# the two numbers are directly comparable.
_REAL_COUNT = 100

# A delivery that never completes must fail the gate, not hang it. Bounds the
# drain loop in both modes (the synthetic loop had no bound either).
_DRAIN_TIMEOUT_SEC = 60.0

# Test-only UID root for the real-mode batch (kept well under VR UI's 64
# bytes; see _seed_real_studies).
_TEST_UID_ROOT = "1.2.840.1.999.42"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _StopwatchHandler:
    """Destination handler that records when delivery first began."""

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


class _CountingScp:
    """Live C-STORE SCP counting instances that actually arrived on the wire.

    The honest end-to-end metric for the real gate: a delivery counts only
    once its bytes have crossed a real socket, not when the handler is
    invoked — the fake handler's counter and the SCP's counter are what
    separates "the queue drains" from "the data moves".
    """

    def __init__(self) -> None:
        self.received = 0
        self.first_received_sec: float | None = None
        self._lock = threading.Lock()
        # perf_counter() is process-wide and monotonic, so the caller can
        # timestamp the enqueue against the same origin this uses.
        self._start = time.perf_counter()
        self._server: Any = None

    def handle_store(self, event: object) -> int:
        with self._lock:
            if self.first_received_sec is None:
                self.first_received_sec = time.perf_counter() - self._start
            self.received += 1
        return 0x0000

    def start(self) -> int:
        """Bind the SCP to an ephemeral loopback port; return the port."""
        from pydicom.uid import AllTransferSyntaxes
        from pydicom.uid import CTImageStorage as CTContext
        from pynetdicom import AE, evt

        ae = AE(ae_title="PERFSCP")
        ae.add_supported_context(CTContext, AllTransferSyntaxes)
        self._server = ae.start_server(
            ("127.0.0.1", 0),
            evt_handlers=[(evt.EVT_C_STORE, self.handle_store)],
            block=False,
        )
        return int(self._server.server_address[1])

    def shutdown(self) -> None:
        if self._server is not None:
            self._server.shutdown()


def _seed_studies(spool: Any, count: int) -> None:
    """Enqueue ``count`` studies against the fake handler (no files on disk)."""
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


def _write_dicom_file(path: Path, study_uid: str, instance_uid: str) -> None:
    """Write a minimal valid CT DICOM file — the smallest unit a handler reads."""
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import UID, CTImageStorage, ExplicitVRLittleEndian

    path.parent.mkdir(parents=True, exist_ok=True)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CTImageStorage
    # pydicom types these as UID, not str — wrap so mypy stays clean.
    meta.MediaStorageSOPInstanceUID = UID(instance_uid)
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(path, {}, file_meta=meta, preamble=b"\x00" * 128)
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = f"{study_uid}.1"
    ds.SOPInstanceUID = instance_uid
    ds.SOPClassUID = CTImageStorage
    ds.PatientName = "PERF^GATE"
    ds.Modality = "CT"
    ds.save_as(path, enforce_file_format=True)


def _seed_real_studies(spool: Any, count: int, target: Any) -> None:
    """Write real DICOM files to the spool and enqueue them for ``target``.

    Every study lands on disk as an actual ``.dcm`` file, so delivery reads
    real bytes and the handler's presentation-context negotiation runs
    against real file headers — the path the production forwarder takes.
    """
    for i in range(count):
        # Short, deterministic UIDs under one test-only root: pydicom's
        # generate_uid() can emit values longer than VR UI's 64-byte ceiling.
        study_uid = f"{_TEST_UID_ROOT}.1.{i}"
        instance_uid = f"{_TEST_UID_ROOT}.2.{i}"
        _write_dicom_file(
            spool.spool_dir / study_uid / f"{study_uid}.1" / f"{instance_uid}.dcm",
            study_uid,
            instance_uid,
        )
        study_id = spool.receive(study_uid)
        spool.enqueue(study_id, [target])


def _drain(forwarder: Any, count: int, delivered: Any, limit: int = 10) -> bool:
    """Pump the forwarder until ``delivered`` reaches ``count`` or the budget ends.

    Returns whether the batch completed — a stuck delivery fails the gate
    instead of hanging it.
    """
    deadline = time.perf_counter() + _DRAIN_TIMEOUT_SEC
    while delivered() < count:
        if time.perf_counter() > deadline:
            return False
        forwarder.process_once(limit=limit)
    return True


@contextlib.contextmanager
def _real_harness() -> Iterator[tuple[Any, Any, Any, _CountingScp]]:
    """A temp on-disk spool + real DICOMHandler + live SCP, torn down on exit.

    Yields ``(spool, forwarder, target, scp)`` so callers can seed studies
    against the real destination without reaching into forwarder internals.
    """
    from mercure_gateway.config import DICOMDestination, default_config
    from mercure_gateway.forwarder import Forwarder, RetryPolicy
    from mercure_gateway.forwarder.handlers.dicom import DICOMHandler
    from mercure_gateway.spool import Spool
    from mercure_gateway.spool.db import mem_database

    scp = _CountingScp()
    port = scp.start()
    try:
        with tempfile.TemporaryDirectory(prefix="perf-gate-") as tmp:
            cfg = default_config()
            cfg.storage.spool_dir = str(Path(tmp) / "spool")
            spool = Spool(mem_database(), cfg)
            target = DICOMDestination(
                name="pacs",
                host="127.0.0.1",
                port=port,
                aet_target="PERFSCP",
            )
            forwarder = Forwarder(cfg, spool, retry=RetryPolicy(base_delay_sec=0))
            forwarder.register_handler("dicom", DICOMHandler(target, spool))
            try:
                yield spool, forwarder, target, scp
            finally:
                forwarder.stop()
    finally:
        scp.shutdown()


def measure_latency() -> float:
    """Return seconds from queueing a study to first delivery attempt."""
    from mercure_gateway.config import default_config
    from mercure_gateway.forwarder import Forwarder
    from mercure_gateway.spool import Spool
    from mercure_gateway.spool.db import mem_database

    db = mem_database()
    spool = Spool(db)
    cfg = default_config()
    cfg.forwarding.queue_poll_interval_ms = 5
    handler = _StopwatchHandler()

    forwarder = Forwarder(cfg, spool)
    forwarder.register_handler("dicom", handler)
    _seed_studies(spool, 1)
    forwarder.process_once()
    forwarder.stop()

    assert handler.first_begin_sec is not None, "no delivery attempted"
    return handler.first_begin_sec


def measure_throughput() -> float:
    """Return items delivered per second across a batch (synthetic handler)."""
    from mercure_gateway.config import default_config
    from mercure_gateway.forwarder import Forwarder
    from mercure_gateway.spool import Spool
    from mercure_gateway.spool.db import mem_database

    db = mem_database()
    spool = Spool(db)
    cfg = default_config()
    handler = _StopwatchHandler()

    forwarder = Forwarder(cfg, spool)
    forwarder.register_handler("dicom", handler)
    count = 100
    _seed_studies(spool, count)

    start = time.perf_counter()
    _drain(forwarder, count, lambda: handler.deliveries)
    elapsed = time.perf_counter() - start
    forwarder.stop()

    return count / max(elapsed, 1e-9)


def measure_real_latency() -> float:
    """Seconds from enqueue to the first byte arriving at a real SCP (P1-7)."""
    with _real_harness() as (spool, forwarder, target, scp):
        # Timestamp the enqueue against the SCP's own clock origin.
        enqueue_at = time.perf_counter() - scp._start
        _seed_real_studies(spool, 1, target)
        forwarder.process_once()

    assert scp.first_received_sec is not None, "no instance reached the SCP"
    return scp.first_received_sec - enqueue_at


def measure_real_throughput() -> float:
    """Instances/s delivered through a real socket to a real SCP (P1-7)."""
    with _real_harness() as (spool, forwarder, target, scp):
        _seed_real_studies(spool, _REAL_COUNT, target)
        start = time.perf_counter()
        drained = _drain(forwarder, _REAL_COUNT, lambda: scp.received)
        elapsed = time.perf_counter() - start

    assert drained, f"only {scp.received}/{_REAL_COUNT} instances reached the SCP"
    return scp.received / max(elapsed, 1e-9)


def _report(
    label: str, value: float, budget: float, unit: str, *, at_most: bool = True
) -> bool:
    """Print one gate line; return whether it passed.

    ``at_most`` selects the sense: latency must stay under its budget,
    throughput must clear its floor.
    """
    ok = value <= budget if at_most else value >= budget
    print(f"{'PASS' if ok else 'FAIL'}: {label} {value:.1f} {unit} (budget {budget:g} {unit})")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--real",
        action="store_true",
        help="measure through a real socket to a local C-STORE SCP (slow/integration)",
    )
    args = parser.parse_args(argv)

    if args.real:
        ok = _report(
            "real forwarding begins in",
            measure_real_latency() * 1000,
            LATENCY_BUDGET_SEC * 1000,
            "ms",
        )
        ok &= _report(
            "real concurrent throughput",
            measure_real_throughput(),
            CONCURRENT_TARGET_ITEMS_PER_SEC,
            "items/s",
            at_most=False,
        )
        return 0 if ok else 1

    ok = _report(
        "forwarding begins in", measure_latency() * 1000, LATENCY_BUDGET_SEC * 1000, "ms"
    )
    ok &= _report(
        "concurrent throughput",
        measure_throughput(),
        CONCURRENT_TARGET_ITEMS_PER_SEC,
        "items/s",
        at_most=False,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
