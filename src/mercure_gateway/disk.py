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
    """Daemon polling the spool filesystem capacity and auto-purging delivery."""

    def __init__(
        self,
        spool: Spool,
        *,
        warning_pct: int,
        purge_on_full: bool,
        poll_sec: float = _DEFAULT_POLL_SEC,
    ) -> None:
        self._spool = spool
        self._warning_pct = warning_pct
        self._purge_on_full = purge_on_full
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
        return pct
