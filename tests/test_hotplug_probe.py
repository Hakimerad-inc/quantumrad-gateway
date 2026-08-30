"""TDD (review F9): sysfs probe must never false-positive on a live device.

The Linux probe used to READ ``/sys/block/<dev>/device/remove`` — a
write-only attribute (root reads ``''``, non-root gets PermissionError) —
and treated any OSError as device removal. On a whole-disk USB mount the
probe therefore reported "removed" on every poll and the gateway shut
itself down ~4 s after start.

Behaviors:
1. A probe on a live spool directory (sysfs path present, device alive)
   must NOT report removal — even though reading the remove file raises.
2. A disappearing spool directory still reports removal (stat fallback).
3. A sysfs path that vanishes while the spool is still alive must NOT trip
   removal (sysfs can be re-scanned; the filesystem is the truth).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from mercure_gateway.hotplug import HotplugDetector


def test_probe_not_removed_on_live_spool(tmp_path: Path) -> None:
    """Live spool dir: probe reports NOT removed (the write-only sysfs
    remove file must never be read — review F9)."""
    spool = tmp_path / "spool"
    spool.mkdir()
    detector = HotplugDetector(spool, lambda: None, enabled=False)
    # Point the "device path" at a write-only-looking sysfs file so the old
    # implementation would raise PermissionError → false "removed".
    sysfs_like = tmp_path / "sys" / "block" / "sdb" / "device" / "remove"
    sysfs_like.parent.mkdir(parents=True)
    sysfs_like.write_text("")
    sysfs_like.chmod(0o200)  # write-only for everyone
    detector._device_path = sysfs_like

    assert detector._probe_device_removed() is False


def test_probe_removed_when_spool_gone(tmp_path: Path) -> None:
    """Spool directory vanished → removal detected (stat fallback)."""
    spool = tmp_path / "spool"
    spool.mkdir()
    detector = HotplugDetector(spool, lambda: None, enabled=False)
    detector._device_path = None  # force stat fallback
    spool.rmdir()

    assert detector._probe_device_removed() is True


def test_probe_not_removed_when_only_sysfs_path_vanishes(tmp_path: Path) -> None:
    """sysfs re-scan or udev hiccup removes the sysfs node while the spool
    filesystem is alive → NOT a removal (filesystem is the truth)."""
    spool = tmp_path / "spool"
    spool.mkdir()
    detector = HotplugDetector(spool, lambda: None, enabled=False)
    sysfs_like = tmp_path / "sys" / "block" / "sdb" / "device" / "remove"
    sysfs_like.parent.mkdir(parents=True)
    sysfs_like.write_text("")
    detector._device_path = sysfs_like
    sysfs_like.unlink()

    assert detector._probe_device_removed() is False


def test_detector_does_not_shut_down_healthy_gateway(tmp_path: Path) -> None:
    """End-to-end regression (review F9): with a sysfs device path present,
    the detector must keep running (no shutdown callback) for several poll
    cycles — the old code fired the callback within 2 polls (~4 s)."""
    spool = tmp_path / "spool"
    spool.mkdir()
    sysfs_like = tmp_path / "sys" / "block" / "sdb" / "device" / "remove"
    sysfs_like.parent.mkdir(parents=True)
    sysfs_like.write_text("")
    sysfs_like.chmod(0o200)  # write-only

    removal_event = threading.Event()
    detector = HotplugDetector(spool, removal_event.set, poll_interval_sec=0.05)
    detector._device_path = sysfs_like
    detector.start()
    time.sleep(0.4)  # ~8 poll cycles
    detector.stop()

    assert not removal_event.is_set(), "healthy gateway shut down by false positive"
    assert detector._probe_device_removed() is False or not spool.exists()
