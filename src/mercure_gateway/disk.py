"""Disk-capacity monitor with optional auto-purge of delivered studies.

Watches the spool filesystem and warns when used capacity crosses
``storage.disk_full_warning_pct``.  When ``storage.purge_on_disk_full`` is set,
delivered (``SENT``) studies are purged *oldest-first* until usage drops back
under the threshold.  Undelivered / FAILED studies are never auto-removed
(US-04) — capacity is always recovered from fully-delivered copies only.

The monitor is best-effort: measurement and purge failures are logged, never
raised, and it runs on its own daemon thread so a stuck filesystem can never
block receive/forward (US-10 isolation invariant).

The purge loops are bounded (review P0-11). Each iteration deletes files and
takes the process-wide database write lock, so an unbounded loop under
sustained incoming load makes the capacity watcher itself the load: the thread
that is supposed to protect the spool competes for the lock the receiver
needs. The cap is not a correctness limit — it is a throttle — and hitting it
is reported rather than silently succeeding (see :attr:`purge_iterations_capped`).
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from collections.abc import Callable

from mercure_gateway.spool import Spool

__all__ = ["DiskMonitor"]

logger = logging.getLogger(__name__)

_DEFAULT_POLL_SEC = 30.0

# A single check purges at most this many studies. 100 × the median delivered
# study is far more than one poll interval should recover; the next poll picks
# up where this one stopped. Without a cap, a spool that grows faster than it
# can purge pins the DB write lock for the whole interval.
_MAX_PURGE_ITERATIONS = 100

# Pause between purge iterations so a long purge yields the database write lock
# to the receiver between studies instead of holding it for the whole run.
_PURGE_PAUSE_SEC = 0.05


class DiskMonitor:
    """Daemon polling the spool filesystem capacity and auto-purging delivery.

    Also enforces the configured spool-size cap (``storage.max_spool_gb``,
    review M3: previously defined but never enforced). The cap counts only
    persisted DICOM instance bytes (the same data the retention purger
    manages); when exceeded, delivered studies are purged oldest-first until
    usage drops back under the cap. Undelivered studies are never removed.
    """

    _GB = 1024**3

    def __init__(
        self,
        spool: Spool,
        *,
        warning_pct: int,
        purge_on_full: bool,
        max_spool_gb: int | None = None,
        max_spool_bytes: int | None = None,
        poll_sec: float = _DEFAULT_POLL_SEC,
        # Test seam: a clock that does not actually sleep. Production passes
        # time.sleep, tests pass a no-op so a bounded loop does not take
        # 100 × 50 ms of wall clock.
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._spool = spool
        self._warning_pct = warning_pct
        self._purge_on_full = purge_on_full
        if max_spool_bytes is not None:
            # Exact-byte knob for tests/drills; the GiB config field is too
            # coarse to express a sub-GiB cap (review: drills reached through
            # _max_spool_bytes, which silently no-ops if the attr is renamed).
            self._max_spool_bytes: int | None = max_spool_bytes
        else:
            self._max_spool_bytes = None if max_spool_gb is None else max_spool_gb * self._GB
        self._poll_sec = max(1.0, poll_sec)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._sleep = sleep if sleep is not None else time.sleep
        # Observability (review P0-11): the loops are bounded by design, so
        # hitting the bound is the signal that the spool is growing faster
        # than purge can recover — the one symptom of a capacity-loss cascade
        # that is otherwise invisible while "disk monitor: running" stays green.
        self.purge_iterations_total = 0
        self.purge_budget_hits = 0
        # Per-check budget; reset in check_once() so the bound is a throttle,
        # not a lifetime quota (a lifetime cap would stop purging forever after
        # the first 100).
        self._purges_this_check = 0

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        """Start the background polling thread (idempotent)."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="disk-monitor")
        self._thread.start()
        logger.info(
            "disk monitor started (warn at %d%%, auto-purge=%s)",
            self._warning_pct,
            self._purge_on_full,
        )

    def stop(self, *, join_timeout: float = 5.0) -> None:
        """Stop the polling thread."""
        self._running = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)
            self._thread = None

    def _loop(self) -> None:
        while self._running:
            try:
                self.check_once()
            except Exception:  # noqa: BLE001 — boundary: monitor must survive
                logger.exception("disk monitor check failed")
            self._stop_event.wait(self._poll_sec)

    def check_once(self) -> float:
        """Measure usage and purge delivered studies if over threshold.

        Returns the used-capacity percentage after any purge.  Raises when the
        filesystem cannot be measured (callers log/ignore).
        """
        disk = shutil.disk_usage(self._spool.spool_dir)
        pct = disk.used * 100.0 / max(1, disk.total)
        # The budget is per check: the next poll continues the purge.
        self._purges_this_check = 0
        if pct >= self._warning_pct:
            logger.warning(
                "spool disk usage at %.1f%% (warning threshold %d%%)",
                pct,
                self._warning_pct,
            )
            if self._purge_on_full:
                while pct >= self._warning_pct:
                    if not self._purge_once(f"disk still over {self._warning_pct}%"):
                        break
                    disk = shutil.disk_usage(self._spool.spool_dir)
                    pct = disk.used * 100.0 / max(1, disk.total)
        self._enforce_spool_cap()
        return pct

    def _enforce_spool_cap(self) -> None:
        """Purge delivered studies when the spool byte total exceeds the cap.

        The cap (``storage.max_spool_gb``, review M3) is enforced against the
        DB-tracked instance bytes, not the whole filesystem: on a shared disk
        the filesystem percentage says nothing about how much spool is being
        used. Purge order is oldest-delivered-first, exactly like the
        disk-full path, and undelivered studies are never touched (US-04).
        """
        if self._max_spool_bytes is None:
            return
        total = self._spool.spool_num_bytes()
        if total <= self._max_spool_bytes:
            return
        over_gb = (total - self._max_spool_bytes) / self._GB
        logger.warning(
            "spool at %.2f GiB exceeds max_spool_gb cap — purging delivered studies",
            total / self._GB,
        )
        while self._spool.spool_num_bytes() > self._max_spool_bytes:
            if not self._purge_once("spool still over the byte cap"):
                break
        logger.info("spool cap enforcement done (was %.2f GiB over)", over_gb)

    def _purge_once(self, still_over_context: str) -> bool:
        """Purge one delivered study, honouring the iteration budget.

        Returns False when there is nothing more to purge — the caller stops
        either way. ``True`` means one study was purged and the caller may
        continue, subject to :data:`_MAX_PURGE_ITERATIONS` *per check*.

        Both purge loops funnel through here so the bound, the yield to the
        receiver, and the "hit the cap" reporting are one implementation
        (review P0-11).
        """
        if self._purges_this_check >= _MAX_PURGE_ITERATIONS:
            # Not a failure — the next poll continues the work. Reported
            # because a spool that needs 100+ purges per interval is losing
            # the capacity race, and "disk monitor: running" says nothing
            # about it.
            if self.purge_budget_hits == 0 or self.purge_budget_hits % 10 == 0:
                logger.warning(
                    "purge loop hit the %d-iteration budget in one check; the "
                    "spool is growing faster than purge can recover — remaining "
                    "purges deferred to the next poll (budget hit %d time(s))",
                    _MAX_PURGE_ITERATIONS,
                    self.purge_budget_hits + 1,
                )
            self.purge_budget_hits += 1
            return False
        if not self._spool.purge_oldest_delivered():
            logger.warning("%s but no delivered studies to purge", still_over_context)
            return False
        self._purges_this_check += 1
        self.purge_iterations_total += 1
        # Yield the DB write lock between studies so a long purge does not
        # starve the receiver for the whole run.
        self._sleep(_PURGE_PAUSE_SEC)
        return True
