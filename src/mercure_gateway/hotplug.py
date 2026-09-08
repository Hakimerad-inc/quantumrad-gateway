"""Hot-unplug detection for the USB dongle gateway variant.

Monitors the spool directory's filesystem and triggers a graceful shutdown
sequence when the underlying storage device is removed.  The shutdown
sequence follows the protocol from ``usb-dongle-gateway-spec §7.2``:

    1. Stop accepting new DICOM associations
    2. Wait for in-flight studies to complete (timeout 10 s)
    3. Flush pending audit events
    4. fsync all open file handles
    5. Write shutdown marker to data partition
    6. Allow USB removal

Detection methods (platform-specific):

- **Linux**: polls ``/sys/block/<device>/device/remove`` or checks
  ``os.path.ismount()`` on the spool directory.
- **Windows**: polls ``win32api.GetVolumeInformation()`` or checks
  ``os.path.ismount()``.
- **Cross-platform fallback**: polls ``os.stat()`` on the spool directory;
  an ``OSError`` indicates the device is gone.

The detector runs as a daemon thread and calls a caller-supplied shutdown
callback when removal is detected.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

__all__ = [
    "HotplugDetector",
    "write_shutdown_marker",
    "has_shutdown_marker",
    "clear_shutdown_marker",
    "run_shutdown_sequence",
]

logger = logging.getLogger(__name__)

_SHUTDOWN_MARKER = ".shutdown"

# Default poll interval and flush timeout per usb-dongle-gateway-spec §7.2
_DEFAULT_POLL_INTERVAL_SEC = 2.0
_FLUSH_TIMEOUT_SEC = 10.0

# Consecutive failed probes required before declaring removal (debounces
# transient stat errors such as AV scans).
_REMOVAL_THRESHOLD = 2


def write_shutdown_marker(spool_dir: Path) -> None:
    """Write a shutdown marker file to *spool_dir*.

    The marker signals that a graceful shutdown was initiated; the recovery
    scanner (``recovery.recover``) reads it on next boot.
    """
    marker = spool_dir / _SHUTDOWN_MARKER
    marker.write_text("shutdown", encoding="utf-8")
    logger.info("Shutdown marker written to %s", marker)


def has_shutdown_marker(spool_dir: Path) -> bool:
    """Return ``True`` if a shutdown marker exists in *spool_dir*."""
    return (spool_dir / _SHUTDOWN_MARKER).exists()


def clear_shutdown_marker(spool_dir: Path) -> None:
    """Remove the shutdown marker if present."""
    marker = spool_dir / _SHUTDOWN_MARKER
    if marker.exists():
        marker.unlink()
        logger.info("Shutdown marker cleared")


def run_shutdown_sequence(
    *,
    stop_receiver: Callable[[], None],
    flush: Callable[[], None],
    spool_dir: Path,
    flush_timeout_sec: float = _FLUSH_TIMEOUT_SEC,
) -> bool:
    """Execute the §7.2 graceful shutdown sequence and return flush outcome.

    Order (each step after the first is best-effort — the device is being
    yanked, so any step may raise ``OSError`` at any moment):

        1. ``stop_receiver`` — stop accepting new DICOM associations
        2. ``flush`` — drain in-flight studies / audit events, bounded by
           ``flush_timeout_sec`` (K10: ≤10 s allows forced removal)
        3. Write the shutdown marker as the *last filesystem act*, fsynced
           so the classification survives a subsequent power loss

    Returns ``True`` when the flush completed (or raised-and-unwound —
    indistinguishable from done on a dying device), ``False`` when it hung
    past ``flush_timeout_sec`` and removal was forced.
    """
    logger.warning("USB removal — shutdown sequence started")
    stop_receiver()

    # The flush runs on a helper thread so a hung drain cannot exceed the
    # deadline: past the timeout removal is forced (K10) and the marker is
    # written anyway. A raising flush counts as "completed" — the OSError
    # is expected on a device that is already vanishing.
    completed = threading.Event()

    def _flush_and_signal() -> None:
        try:
            flush()
        finally:
            # Signal even on exception — a raising flush has unwound, so it
            # is not hung; only a flush stuck *inside* must hit the timeout.
            completed.set()

    flush_thread = threading.Thread(
        target=_flush_and_signal, name="hotplug-flush", daemon=True
    )
    flush_thread.start()
    finished = completed.wait(timeout=flush_timeout_sec)

    if not finished:
        logger.warning(
            "flush did not complete within %.1fs — forcing removal", flush_timeout_sec
        )
    try:
        _write_shutdown_marker_fsynced(spool_dir)
    except OSError:
        # Device already gone — nothing further to persist, nothing to
        # recover on next boot (the recovery scan will run regardless).
        logger.warning("could not write shutdown marker (device already removed)")
    return finished


def _write_shutdown_marker_fsynced(spool_dir: Path) -> None:
    """Write the shutdown marker and fsync it before returning."""
    marker = spool_dir / _SHUTDOWN_MARKER
    with open(marker, "w", encoding="utf-8") as f:
        f.write("shutdown")
        f.flush()
        os.fsync(f.fileno())
    logger.info("Shutdown marker written (fsynced) to %s", marker)


class HotplugDetector:
    """Daemon thread that monitors a spool directory for device removal.

    Parameters
    ----------
    spool_dir:
        The directory whose filesystem is monitored.
    on_removal:
        Callable invoked (in the detector thread) when removal is detected.
        Typically stops the receiver, flushes, and writes the shutdown marker.
    poll_interval_sec:
        Seconds between filesystem checks (default 2.0).
    enabled:
        When ``False`` the detector thread is never started (no-op).

    Example::

        def shutdown():
            receiver.stop()
            write_shutdown_marker(spool.spool_dir)

        detector = HotplugDetector(spool.spool_dir, shutdown, enabled=True)
        detector.start()
        # ... gateway runs ...
        detector.stop()
    """

    def __init__(
        self,
        spool_dir: Path,
        on_removal: Callable[[], None],
        *,
        poll_interval_sec: float = _DEFAULT_POLL_INTERVAL_SEC,
        enabled: bool = True,
    ) -> None:
        self._spool_dir = spool_dir
        self._on_removal = on_removal
        self._poll_interval = poll_interval_sec
        self._enabled = enabled
        self._running = False
        self._thread: threading.Thread | None = None
        self._failures = 0
        self._device_path = self._resolve_device_path()

    # -- public API -------------------------------------------------------

    def start(self) -> None:
        """Begin monitoring in a daemon thread.  No-op if disabled."""
        if not self._enabled:
            logger.debug("Hotplug detection disabled; not starting")
            return
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="hotplug")
        self._thread.start()
        logger.info("Hotplug detection started (poll every %.1fs)", self._poll_interval)

    def stop(self) -> None:
        """Stop monitoring."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=self._poll_interval + 1)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._running

    # -- internals --------------------------------------------------------

    def _resolve_device_path(self) -> Path | None:
        """Return the sysfs remove path for the spool's block device, or None."""
        if sys.platform != "linux":
            return None
        try:
            # Resolve the real path and find its mount point
            resolved = os.path.realpath(str(self._spool_dir))
            # On Linux, removable devices are typically /dev/sdX mounted somewhere
            stat = os.stat(resolved)
            dev_num = stat.st_dev
            # Read /proc/mounts to find the device
            with open("/proc/mounts") as f:
                for line in f:
                    fields = line.split()
                    if len(fields) < 2:
                        continue
                    mount_point = fields[1]
                    device = fields[0]
                    mount_stat = os.stat(mount_point)
                    if mount_stat.st_dev == dev_num and device.startswith("/dev/"):
                        # Found the device — construct sysfs path
                        dev_name = os.path.basename(device)
                        sysfs_path = Path(f"/sys/block/{dev_name}/device/remove")
                        if sysfs_path.exists():
                            return sysfs_path
                        return None
        except (OSError, IndexError):
            return None
        return None

    def _is_device_removed(self) -> bool:
        """Return ``True`` if the storage device has been removed.

        Requires ``_REMOVAL_THRESHOLD`` consecutive failures so a transient
        ``os.stat`` error (AV scan, permission hiccup) doesn't trigger a full
        shutdown.
        """
        removed = self._probe_device_removed()
        if removed:
            self._failures += 1
        else:
            self._failures = 0
        return self._failures >= _REMOVAL_THRESHOLD

    def _probe_device_removed(self) -> bool:
        """Single removal probe (no debouncing).

        The spool FILESYSTEM is the source of truth: ``os.stat`` on the
        directory fails only when the underlying device is gone.

        The sysfs ``remove`` attribute is never read (review F9): it is
        write-only (root reads ``''``, non-root gets PermissionError), so a
        read can only ever false-positive. Its mere existence does not prove
        the device is alive either — sysfs nodes can vanish during a re-scan
        while the mount is fine — so only the stat probe decides.
        """
        try:
            os.stat(str(self._spool_dir))
            return False
        except OSError:
            return True

    def _run(self) -> None:
        """Background loop that polls for device removal."""
        while self._running:
            try:
                if self._is_device_removed():
                    logger.warning("USB device removal detected for %s", self._spool_dir)
                    self._running = False
                    self._on_removal()
                    return
            except Exception:
                logger.exception("Error checking device status")
            time.sleep(self._poll_interval)
