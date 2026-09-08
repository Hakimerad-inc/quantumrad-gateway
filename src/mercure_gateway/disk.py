"""Disk-capacity monitor with optional auto-purge of delivered studies.

Watches the spool filesystem and warns when used capacity crosses
``storage.disk_full_warning_pct``.  When ``storage.purge_on_disk_full`` is set,
delivered (``SENT``) studies are purged *oldest-first* until usage drops back
under the threshold.  Undelivered / FAILED studies are never auto-removed
(US-04) — capacity is always recovered from fully-delivered copies only.

The monitor is best-effort: measurement and purge failures are logged, never
raised, and it runs on its own daemon thread so a stuck filesystem can never
block receive/forward (US-10 isolation invariant).
"""

from __future__ import annotations

import logging
import shutil
import threading

from mercure_gateway.spool import Spool

__all__ = ["DiskMonitor"]

logger = logging.getLogger(__name__)

_DEFAULT_POLL_SEC = 30.0


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
        poll_sec: float = _DEFAULT_POLL_SEC,
    ) -> None:
        self._spool = spool
        self._warning_pct = warning_pct
        self._purge_on_full = purge_on_full
        self._max_spool_bytes = None if max_spool_gb is None else max_spool_gb * self._GB
        self._poll_sec = max(1.0, poll_sec)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False

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
        if pct >= self._warning_pct:
            logger.warning(
                "spool disk usage at %.1f%% (warning threshold %d%%)",
                pct,
                self._warning_pct,
            )
            if self._purge_on_full:
                while pct >= self._warning_pct:
                    if not self._spool.purge_oldest_delivered():
                        logger.warning(
                            "disk still over %d%% but no delivered studies to purge",
                            self._warning_pct,
                        )
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
            if not self._spool.purge_oldest_delivered():
                logger.warning(
                    "spool over cap by %.2f GiB but no delivered studies to purge",
                    (self._spool.spool_num_bytes() - self._max_spool_bytes) / self._GB,
                )
                break
        logger.info("spool cap enforcement done (was %.2f GiB over)", over_gb)
