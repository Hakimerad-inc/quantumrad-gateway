"""TDD (S03-T5, RED): Retention & cleanup — only delivered studies purged.

Behaviors:
1. ``Spool.mark_delivered`` stamps ``retention_delivered_at`` on SENT studies
2. ``Spool.purge_delivered`` deletes files + rows for delivered studies older
   than ``retention_delivered_days``
3. Undelivered (FAILED/ERROR/QUEUED) studies are NEVER purged (US-04)
4. Delivered but not-yet-expired studies are kept
5. Purging only touches the delivered study, not unrelated ones
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mercure_gateway.config import default_config
from mercure_gateway.spool import Spool, StudyState
from mercure_gateway.spool.db import mem_database


def make_spool(tmp_path: Path, retention_days: int = 3) -> Spool:
    cfg = default_config()
    cfg.storage.spool_dir = str(tmp_path / "spool")
    cfg.storage.retention_delivered_days = retention_days
    return Spool(mem_database(), cfg)


def make_study_file(spool: Spool, study_uid: str, series_uid: str) -> Path:
    p = spool.spool_dir / study_uid / series_uid / "1.dcm"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"dicom")
    return p


def deliver_study(spool: Spool, study_uid: str) -> int:
    study_id = spool.receive(study_uid)
    spool._db.set_study_state(study_id, StudyState.SENT.value)
    return study_id


# ── Test 1: mark_delivered stamps retention_delivered_at ──────────────


def test_mark_delivered_stamps_timestamp(tmp_path: Path) -> None:
    spool = make_spool(tmp_path)
    study_id = deliver_study(spool, "1.2.3.4.1")

    spool.mark_delivered(study_id)

    row = spool._db.get_study(study_id)
    assert row is not None
    assert row["retention_delivered_at"] is not None


# ── Test 2: purge removes delivered + expired ─────────────────────────


def test_purge_removes_delivered_and_expired(tmp_path: Path) -> None:
    spool = make_spool(tmp_path, retention_days=3)
    study_id = deliver_study(spool, "1.2.3.4.2")
    make_study_file(spool, "1.2.3.4.2", "1.2.3.4.2.1")
    spool.mark_delivered(study_id)
    # Backdate the delivery beyond the retention window.
    spool._db.connection().execute(
        "UPDATE studies SET retention_delivered_at = datetime('now', '-10 days') WHERE id = ?",
        (study_id,),
    )
    spool._db.connection().commit()

    purged = spool.purge_delivered()

    assert purged == 1
    assert spool._db.get_study(study_id) is None
    assert not (spool.spool_dir / "1.2.3.4.2").exists()


# ── Test 3: undelivered studies never purged (US-04) ──────────────────


@pytest.mark.parametrize(
    "state",
    [StudyState.FAILED, StudyState.ERROR, StudyState.QUEUED, StudyState.RECEIVED],
)
def test_undelivered_never_purged(tmp_path: Path, state: StudyState) -> None:
    spool = make_spool(tmp_path, retention_days=0)  # 0 days = purge immediately
    study_id = spool.receive("1.2.3.4.3")
    spool._db.set_study_state(study_id, state.value)
    make_study_file(spool, "1.2.3.4.3", "1.2.3.4.3.1")
    # Even an ancient timestamp must not make an undelivered study purgable.
    spool._db.connection().execute(
        "UPDATE studies SET retention_delivered_at = datetime('now', '-10 days') WHERE id = ?",
        (study_id,),
    )
    spool._db.connection().commit()

    purged = spool.purge_delivered()

    assert purged == 0
    assert spool._db.get_study(study_id) is not None


# ── Test 4: delivered but not expired is kept ─────────────────────────


def test_delivered_not_expired_kept(tmp_path: Path) -> None:
    spool = make_spool(tmp_path, retention_days=3)
    study_id = deliver_study(spool, "1.2.3.4.4")
    make_study_file(spool, "1.2.3.4.4", "1.2.3.4.4.1")
    spool.mark_delivered(study_id)  # delivered just now → not expired

    purged = spool.purge_delivered()

    assert purged == 0
    assert spool._db.get_study(study_id) is not None
    assert (spool.spool_dir / "1.2.3.4.4").exists()


# ── Test 5: purge only touches eligible studies ───────────────────────


def test_usb_mode_uses_hour_granular_retention(tmp_path: Path) -> None:
    """With usb_mode.enabled the purge honors the aggressive 24 h window
    (usb-dongle-gateway-spec §6), not the day-granular default."""
    cfg = default_config()
    cfg.storage.spool_dir = str(tmp_path / "spool")
    cfg.storage.retention_delivered_days = 5  # would keep 30 h — must NOT apply
    cfg.usb_mode.enabled = True
    cfg.usb_mode.retention_delivered_hours = 24
    spool = Spool(mem_database(), cfg)

    stale = deliver_study(spool, "1.2.3.4.7")
    make_study_file(spool, "1.2.3.4.7", "1.2.3.4.7.1")
    spool.mark_delivered(stale)
    spool._db.connection().execute(
        "UPDATE studies SET retention_delivered_at = datetime('now', '-30 hours') WHERE id = ?",
        (stale,),
    )
    fresh = deliver_study(spool, "1.2.3.4.8")
    make_study_file(spool, "1.2.3.4.8", "1.2.3.4.8.1")
    spool.mark_delivered(fresh)
    spool._db.connection().execute(
        "UPDATE studies SET retention_delivered_at = datetime('now', '-12 hours') WHERE id = ?",
        (fresh,),
    )
    spool._db.connection().commit()

    purged = spool.purge_delivered()

    assert purged == 1
    assert spool._db.get_study(stale) is None
    assert spool._db.get_study(fresh) is not None
    assert not (spool.spool_dir / "1.2.3.4.7").exists()


def test_usb_mode_disabled_keeps_day_retention(tmp_path: Path) -> None:
    """Without usb_mode the day-granular window still wins (S10-T5)."""
    spool = make_spool(tmp_path, retention_days=1)
    study_id = deliver_study(spool, "1.2.3.4.9")
    make_study_file(spool, "1.2.3.4.9", "1.2.3.4.9.1")
    spool.mark_delivered(study_id)
    # 30 h ago = just past the 1-day window → purged.
    spool._db.connection().execute(
        "UPDATE studies SET retention_delivered_at = datetime('now', '-30 hours') WHERE id = ?",
        (study_id,),
    )
    spool._db.connection().commit()

    assert spool.purge_delivered() == 1
    assert spool._db.get_study(study_id) is None


def test_purge_does_not_touch_other_studies(tmp_path: Path) -> None:
    spool = make_spool(tmp_path, retention_days=3)
    expired = deliver_study(spool, "1.2.3.4.5")
    make_study_file(spool, "1.2.3.4.5", "1.2.3.4.5.1")
    spool.mark_delivered(expired)
    spool._db.connection().execute(
        "UPDATE studies SET retention_delivered_at = datetime('now', '-10 days') WHERE id = ?",
        (expired,),
    )
    fresh = deliver_study(spool, "1.2.3.4.6")
    make_study_file(spool, "1.2.3.4.6", "1.2.3.4.6.1")
    spool.mark_delivered(fresh)
    spool._db.connection().commit()

    purged = spool.purge_delivered()

    assert purged == 1
    assert spool._db.get_study(expired) is None
    assert spool._db.get_study(fresh) is not None