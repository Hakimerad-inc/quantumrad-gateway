"""TDD (review F8): exponential backoff must be enforced across workers.

The retry loop used to re-queue the route BEFORE sleeping the backoff, so
with more than one worker another poller re-claimed the waiting route
immediately and the backoff schedule was never applied — the whole retry
budget burned at the poll rate (~2.5 s for 5 attempts).

Behaviors:
1. Single worker, zero delay: retry-until-budget behavior is unchanged
   (regression guard for the existing FAILED path).
2. With a real backoff delay, the route stays in ``sending`` (locked by the
   sleeping worker) so a second ``process_once`` from another worker cannot
   re-claim it mid-backoff.
3. After the backoff elapses, the SAME worker re-claims the route by id and
   retries — attempts advance one per backoff window, not one per poll.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from mercure_gateway.config import DICOMDestination, default_config
from mercure_gateway.forwarder import DeliveryResult, Forwarder, RetryPolicy
from mercure_gateway.spool import ClaimedTask, Spool, StudyState
from mercure_gateway.spool.db import mem_database


class SlowFailingHandler:
    """Fails immediately; records wall-clock timestamps of each attempt."""

    def __init__(self) -> None:
        self.attempts: list[float] = []

    def deliver(self, task: ClaimedTask, spool_dir: Path) -> DeliveryResult:
        self.attempts.append(time.monotonic())
        return DeliveryResult(ok=False, error="destination down")


def make_forwarder(
    spool: Spool, handler: object, *, base_delay: float, max_attempts: int = 5
) -> Forwarder:
    fwd = Forwarder(
        default_config(),
        spool,
        retry=RetryPolicy(base_delay_sec=base_delay, max_attempts=max_attempts),
    )
    fwd.register_handler("dicom", handler)  # type: ignore[arg-type]
    return fwd


def test_zero_delay_retry_exhausts_to_failed(target_hub: DICOMDestination) -> None:
    """Regression: base_delay 0 keeps the old behavior (immediate retries)."""
    spool = Spool(mem_database())
    handler = SlowFailingHandler()
    fwd = make_forwarder(spool, handler, base_delay=0, max_attempts=3)

    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])
    fwd.process_once()

    assert spool.state(study_id) == StudyState.FAILED
    assert len(handler.attempts) == 3


@pytest.mark.slow
def test_backoff_holds_route_against_other_workers(
    target_hub: DICOMDestination,
) -> None:
    """While worker A sleeps the backoff, the route must NOT be claimable by
    another worker's poll (review F8)."""
    spool = Spool(mem_database())
    handler = SlowFailingHandler()
    fwd = make_forwarder(spool, handler, base_delay=1.0, max_attempts=5)

    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])

    # Simulate worker A: claim + fail + enter backoff (run the retry in a
    # thread because _dispatch blocks for the full backoff window).
    dispatch_thread = threading.Thread(target=fwd.process_once, daemon=True)
    dispatch_thread.start()
    # Poll until worker A has claimed and failed the first attempt (review M14:
    # replace fixed sleep with event synchronisation to avoid flaking under CI).
    for _ in range(60):
        if len(handler.attempts) >= 1:
            break
        time.sleep(0.005)
    assert len(handler.attempts) >= 1, "worker A never claimed the route"

    # Worker B polls while A is sleeping the backoff.
    other = fwd.process_once()
    assert other == 0, (
        "route was re-claimable during backoff — exponential backoff defeated"
    )

    dispatch_thread.join(timeout=20)
    # The same worker's retries continue on schedule: 5 attempts spaced ≥1 s.
    assert len(handler.attempts) == 5
    for first, second in zip(handler.attempts, handler.attempts[1:], strict=False):
        gap = second - first
        assert gap >= 0.9, f"retry gap {gap:.2f}s < backoff — schedule not enforced"


def test_backoff_retries_continue_on_same_worker(
    target_hub: DICOMDestination,
) -> None:
    """Attempts advance one per backoff window; the study only reaches FAILED
    after the full budget has been spent through the schedule."""
    spool = Spool(mem_database())
    handler = SlowFailingHandler()
    fwd = make_forwarder(spool, handler, base_delay=0.3, max_attempts=3)

    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])

    start = time.monotonic()
    fwd.process_once()
    elapsed = time.monotonic() - start

    assert spool.state(study_id) == StudyState.FAILED
    assert len(handler.attempts) == 3
    # Two backoff windows of 0.3 s (0.3 + 0.6) must have elapsed.
    assert elapsed >= 0.85, f"retries finished in {elapsed:.2f}s — backoff skipped"
