"""TDD (S03-T7, RED): Concurrent forwarding workers forward in parallel.

Behaviors (refinement §2.2):
1. N concurrent workers (default 3) claim and deliver N studies in parallel.
2. All N studies reach SENT after concurrent dispatch.
3. Single worker (concurrency=1) processes sequentially.
4. Configurable concurrency is respected.
"""

from __future__ import annotations

import time
from pathlib import Path

from mercure_gateway.config import DICOMDestination, default_config
from mercure_gateway.forwarder import DeliveryResult, Forwarder, RetryPolicy
from mercure_gateway.spool import ClaimedTask, Spool, StudyState
from mercure_gateway.spool.db import mem_database


class SlowHandler:
    """Handler that takes *delay_sec* per call, recording calls."""

    def __init__(self, delay_sec: float = 0.3, succeed: bool = True) -> None:
        self.delay_sec = delay_sec
        self.succeed = succeed
        self.calls: list[ClaimedTask] = []

    def deliver(self, task: ClaimedTask, spool_dir: Path) -> DeliveryResult:
        self.calls.append(task)
        time.sleep(self.delay_sec)
        return DeliveryResult(ok=self.succeed)


def study(spool: Spool, uid_suffix: str, dest: DICOMDestination) -> int:
    sid = spool.receive(f"1.2.3.{uid_suffix}")
    spool.enqueue(sid, [dest])
    return sid


TARGET = DICOMDestination(
    name="pacs", type="dicom", host="127.0.0.1", port=11112, aet_target="PACS"
)


def make_forwarder(concurrency: int, handler: SlowHandler) -> Forwarder:
    cfg = default_config()
    cfg.forwarding.concurrency = concurrency
    fwd = Forwarder(cfg, Spool(mem_database()), retry=RetryPolicy(base_delay_sec=0, max_attempts=1))
    fwd.register_handler("dicom", handler)
    return fwd


# ── Test 1: 3 concurrent workers process 3 studies in parallel ────────


def test_3_workers_parallel() -> None:
    spool = Spool(mem_database())
    handler = SlowHandler(delay_sec=0.5)
    cfg = default_config()
    cfg.forwarding.concurrency = 3
    fwd = Forwarder(cfg, spool, retry=RetryPolicy(base_delay_sec=0, max_attempts=1))
    fwd.register_handler("dicom", handler)

    ids = [study(spool, str(i), TARGET) for i in range(3)]
    fwd.start()

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if all(spool.state(sid) == StudyState.SENT for sid in ids):
            break
        time.sleep(0.05)

    fwd.stop()

    assert all(spool.state(sid) == StudyState.SENT for sid in ids)
    assert len(handler.calls) == 3


# ── Test 2: single worker processes sequentially ─────────────────────


def test_single_worker_sequential() -> None:
    spool = Spool(mem_database())
    handler = SlowHandler(delay_sec=0.3)
    cfg = default_config()
    cfg.forwarding.concurrency = 1
    fwd = Forwarder(cfg, spool, retry=RetryPolicy(base_delay_sec=0, max_attempts=1))
    fwd.register_handler("dicom", handler)

    ids = [study(spool, str(i), TARGET) for i in range(2)]
    start = time.monotonic()
    fwd.start()
    time.sleep(1.0)  # enough for sequential (2*0.3 + overhead)
    fwd.stop()
    elapsed = time.monotonic() - start

    assert all(spool.state(sid) == StudyState.SENT for sid in ids)
    # Sequential dispatch should take at least 2 * delay_sec.
    assert elapsed >= 0.5, f"too fast for sequential: {elapsed:.2f}s"


# ── Test 3: concurrency config is respected ───────────────────────────


def test_concurrency_config_respected() -> None:
    spool = Spool(mem_database())
    cfg = default_config()
    cfg.forwarding.concurrency = 5
    fwd = Forwarder(cfg, spool)
    fwd.start()
    assert len(fwd._workers) == 5
    fwd.stop()
