"""Disk-full monitoring + auto-purge of delivered studies (US-04, S03).

Behaviors:
1. ``Spool.purge_oldest_delivered`` removes the oldest fully-delivered study only.
2. Undelivered / FAILED studies are never auto-purged.
3. ``DiskMonitor.check_once`` warns when capacity crosses ``disk_full_warning_pct``.
4. With ``purge_on_disk_full`` it purges delivered studies oldest-first until
   usage drops back under the threshold.
5. Below the threshold nothing is purged.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mercure_gateway.config import default_config
from mercure_gateway.disk import DiskMonitor
from mercure_gateway.spool import Spool, StudyState
from mercure_gateway.spool.db import mem_database

# Valid DICOM UIDs are digits + dots only (max 64 chars).
UID_1 = "1.2.826.0.1.3680043.10.150.1"
UID_2 = "1.2.826.0.1.3680043.10.150.2"
UID_3 = "1.2.826.0.1.3680043.10.150.3"


def make_spool(tmp_path: Path) -> Spool:
    cfg = default_config()
    cfg.storage.spool_dir = str(tmp_path / "spool")
    return Spool(mem_database(), cfg)


def deliver(spool: Spool, study_uid: str, *, age_hours: int = 0) -> int:
    """Receive + mark delivered a study (optionally aged for ordering)."""
    study_id = spool.receive(study_uid)
    spool._db.set_study_state(study_id, StudyState.SENT.value)
    spool.mark_delivered(study_id)
    (spool.spool_dir / study_uid / "series" / "inst.dcm").parent.mkdir(parents=True, exist_ok=True)
    (spool.spool_dir / study_uid / "series" / "inst.dcm").write_bytes(b"dicom")
    if age_hours:
        spool._db.connection().execute(
            "UPDATE studies SET retention_delivered_at = datetime('now', ?) WHERE id = ?",
            (f"-{age_hours} hours", study_id),
        )
        spool._db.connection().commit()
    return study_id


# ── DiskMonitor measurement helper ────────────────────────────────────────


def usage(pct: float) -> SimpleNamespace:
    return SimpleNamespace(total=100_000_000_000, used=pct * 1_000_000_000, free=0)


# ── Spool.purge_oldest_delivered ──────────────────────────────────────────


def test_purge_oldest_delivered_removes_oldest_first(tmp_path: Path) -> None:
    spool = make_spool(tmp_path)
    old_id = deliver(spool, UID_1, age_hours=48)
    new_id = deliver(spool, UID_2, age_hours=1)

    assert spool.purge_oldest_delivered() is True
    assert spool._db.get_study(old_id) is None
    assert not (spool.spool_dir / UID_1).exists()
    assert spool._db.get_study(new_id) is not None

    assert spool.purge_oldest_delivered() is True
    assert spool._db.get_study(new_id) is None

    assert spool.purge_oldest_delivered() is False


def test_purge_oldest_delivered_never_touches_undelivered(tmp_path: Path) -> None:
    spool = make_spool(tmp_path)
    failed_id = spool.receive(UID_3)
    spool._db.set_study_state(failed_id, StudyState.FAILED.value)
    spool._db.connection().execute(
        "UPDATE studies SET retention_delivered_at = datetime('now', '-10 days') WHERE id = ?",
        (failed_id,),
    )
    spool._db.connection().commit()

    assert spool.purge_oldest_delivered() is False
    assert spool._db.get_study(failed_id) is not None


# ── DiskMonitor warning / purge behaviour ────────────────────────────────


def test_monitor_warns_when_over_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    spool = make_spool(tmp_path)
    study_id = deliver(spool, UID_1)
    monkeypatch.setattr("mercure_gateway.disk.shutil.disk_usage", lambda _p: usage(95.0))
    monitor = DiskMonitor(spool, warning_pct=90, purge_on_full=False)

    with caplog.at_level(logging.WARNING):
        pct = monitor.check_once()

    assert pct >= 90.0
    assert any("disk usage at" in r.message for r in caplog.records)
    assert spool._db.get_study(study_id) is not None  # warning only — nothing purged


def test_monitor_purges_delivered_until_below_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = make_spool(tmp_path)
    old_id = deliver(spool, UID_1, age_hours=48)
    new_id = deliver(spool, UID_2, age_hours=1)
    # First measurement over threshold, second (after one purge) under.
    measurements = iter([usage(95.0), usage(60.0)])
    monkeypatch.setattr("mercure_gateway.disk.shutil.disk_usage", lambda _p: next(measurements))
    monitor = DiskMonitor(spool, warning_pct=90, purge_on_full=True)

    pct = monitor.check_once()

    assert pct == 60.0
    assert spool._db.get_study(old_id) is None  # oldest purged
    assert spool._db.get_study(new_id) is not None  # newest kept
    assert not (spool.spool_dir / UID_1).exists()


def test_monitor_stops_purging_when_no_delivered_studies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = make_spool(tmp_path)
    monkeypatch.setattr(
        "mercure_gateway.disk.shutil.disk_usage",
        lambda _p: usage(95.0),  # stays over — but nothing left to purge
    )
    monitor = DiskMonitor(spool, warning_pct=90, purge_on_full=True)

    pct = monitor.check_once()

    assert pct >= 90.0  # no exception, loop terminates safely


def test_monitor_below_threshold_does_not_purge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = make_spool(tmp_path)
    study_id = deliver(spool, UID_1)
    monkeypatch.setattr("mercure_gateway.disk.shutil.disk_usage", lambda _p: usage(50.0))
    monitor = DiskMonitor(spool, warning_pct=90, purge_on_full=True)

    pct = monitor.check_once()

    assert pct == 50.0
    assert spool._db.get_study(study_id) is not None


# ── Spool-size cap (storage.max_spool_gb — review M3) ────────────────────


def store_instance_bytes(spool: Spool, study_uid: str, num_bytes: int) -> None:
    """Record a delivered study carrying *num_bytes* of instance data."""
    study_id = deliver(spool, study_uid)
    spool._db.insert_instance_meta(
        study_uid=study_uid,
        series_uid=f"{study_uid}.2",
        instance_uid=f"{study_uid}.3",
        file_path=str(spool.spool_dir / study_uid / "series" / "inst.dcm"),
        received_syntax="1.2.840.10008.1.2.1",
        stored_syntax="1.2.840.10008.1.2.1",
        num_bytes=num_bytes,
    )
    _ = study_id


def test_spool_cap_enforced_purges_oldest_delivered(tmp_path: Path) -> None:
    spool = make_spool(tmp_path)
    store_instance_bytes(spool, UID_1, num_bytes=2 * 1024**3)  # 2 GiB, older
    store_instance_bytes(spool, UID_2, num_bytes=1 * 1024**3)  # 1 GiB, newer
    monitor = DiskMonitor(spool, warning_pct=90, purge_on_full=False, max_spool_gb=1)

    pct = monitor.check_once()

    assert spool._db.spool_num_bytes() <= 1 * 1024**3
    assert spool._db.get_study(spool._db.get_study_by_uid(UID_2)["id"]) is not None
    assert not (spool.spool_dir / UID_1).exists()
    _ = pct


def test_spool_cap_not_triggered_under_limit(tmp_path: Path) -> None:
    spool = make_spool(tmp_path)
    store_instance_bytes(spool, UID_1, num_bytes=512 * 1024**2)
    monitor = DiskMonitor(spool, warning_pct=90, purge_on_full=False, max_spool_gb=1)

    monitor.check_once()

    assert spool._db.get_study(spool._db.get_study_by_uid(UID_1)["id"]) is not None


def test_spool_cap_never_purges_undelivered(tmp_path: Path) -> None:
    spool = make_spool(tmp_path)
    # A *delivered* study pushing over the cap, and an undelivered one.
    # Neither study has instance_meta rows — the cap loop must run, find no
    # eligible (delivered) study via spool_num_bytes accounting, and stop.
    delivered_id = deliver(spool, UID_1)
    undelivered_id = spool.receive(UID_3)
    monitor = DiskMonitor(spool, warning_pct=90, purge_on_full=False, max_spool_gb=1)

    monitor.check_once()

    assert spool._db.get_study(undelivered_id) is not None  # US-04: never removed
    assert spool._db.get_study(delivered_id) is not None  # still counted toward cap


def test_spool_cap_disabled_when_unset(tmp_path: Path) -> None:
    spool = make_spool(tmp_path)
    store_instance_bytes(spool, UID_1, num_bytes=10 * 1024**3)
    monitor = DiskMonitor(spool, warning_pct=90, purge_on_full=False, max_spool_gb=None)

    monitor.check_once()

    assert spool._db.get_study(spool._db.get_study_by_uid(UID_1)["id"]) is not None


def test_monitor_loop_runs_until_stopped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spool = make_spool(tmp_path)
    monkeypatch.setattr("mercure_gateway.disk.shutil.disk_usage", lambda _p: usage(50.0))
    monitor = DiskMonitor(spool, warning_pct=90, purge_on_full=False)
    monitor.start()
    assert monitor.is_running
    time.sleep(0.05)  # let one tick run (poll clamped to >= 1s)
    monitor.stop()
    assert not monitor.is_running


def test_monitor_start_is_idempotent(tmp_path: Path) -> None:
    spool = make_spool(tmp_path)
    monitor = DiskMonitor(spool, warning_pct=90, purge_on_full=False)
    monitor.start()
    first_thread = monitor._thread
    monitor.start()  # second start must be a no-op
    assert monitor._thread is first_thread
    monitor.stop()
