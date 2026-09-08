#!/usr/bin/env python
"""S09-T2 perf gates — forwarding latency + concurrent throughput (K8).

Measures, against a scripted fake handler, the time from claim to the first
delivery attempt (US-03 ≤2 s "begin") and the concurrent forwarding
throughput (K8).  These gates run in CI and fail the build on regression.

Usage:
    python scripts/check_perf_gates.py

Returns exit code 0 (pass) or 1 (fail) and prints results.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

LATENCY_BUDGET_SEC = 2.0  # US-03: forwarding begins within 2 s of claim
CONCURRENT_TARGET_ITEMS_PER_SEC = 5  # K8: sanity throughput floor (CI-safe)

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
    """Return items delivered per second across a batch."""
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
    while handler.deliveries < count:
        forwarder.process_once(limit=10)
    elapsed = time.perf_counter() - start
    forwarder.stop()

    return count / max(elapsed, 1e-9)


def main() -> int:
    ok = True

    latency = measure_latency()
    status = "PASS" if latency <= LATENCY_BUDGET_SEC else "FAIL"
    ok = ok and status == "PASS"
    print(
        f"{status}: forwarding begins in {latency*1000:.0f} ms "
        f"(budget {LATENCY_BUDGET_SEC*1000:.0f} ms)"
    )

    throughput = measure_throughput()
    status = "PASS" if throughput >= CONCURRENT_TARGET_ITEMS_PER_SEC else "FAIL"
    ok = ok and status == "PASS"
    print(
        f"{status}: concurrent throughput {throughput:.1f} items/s "
        f"(floor {CONCURRENT_TARGET_ITEMS_PER_SEC:.0f})"
    )

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
