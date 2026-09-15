"""TDD (review F1): received studies must be auto-enqueued for forwarding.

The production receive path must route studies to the forwarder without
manual/demo-code ``enqueue`` calls (US-03: auto-forward). After the last
instance of a study arrives, the spool schedules it to the enabled
destinations once the receiver has been idle for
``receiver.auto_enqueue_delay_sec`` — the delay debounces multi-instance
studies so the forwarder never delivers a half-received study.

Behaviors:
1. Single instance → RECEIVED immediately; QUEUED with routes after the delay.
2. Multi-instance study stays RECEIVED while instances keep arriving and is
   enqueued only after the last one (+ delay), with the full instance count.
3. No configured destinations → study stays RECEIVED (nothing to route to).
4. A study already QUEUED (or beyond) is never demoted by a late timer.
5. Works through the real Receiver C-STORE path, not just Spool directly.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian

from mercure_gateway.config import DICOMDestination, ReceiverConfig, default_config
from mercure_gateway.spool import Spool, StudyState
from mercure_gateway.spool.db import mem_database

# Delay long enough that "still RECEIVED" assertions right after a store are
# deterministic, short enough that the waits stay fast.
_DELAY_SEC = 1.0


def make_dataset(
    study_uid: str, series_uid: str, instance_uid: str, modality: str = "CT"
) -> Dataset:
    ds = Dataset()
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.SOPInstanceUID = instance_uid
    ds.SOPClassUID = CTImageStorage
    ds.Modality = modality
    ds.PatientName = "TEST^P"
    ds.AccessionNumber = "ACC-1"
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = CTImageStorage
    ds.file_meta.MediaStorageSOPInstanceUID = instance_uid
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    return ds


def auto_spool(
    tmp_path: Path, destinations: list[DICOMDestination], *, delay: float = _DELAY_SEC
) -> Spool:
    cfg = default_config()
    cfg.storage.spool_dir = str(tmp_path / "spool")
    cfg.receiver.auto_enqueue_delay_sec = delay
    cfg.destinations = destinations
    return Spool(mem_database(), cfg)


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture()
def hub() -> DICOMDestination:
    return DICOMDestination(
        name="hub", type="dicom", host="hub.local", port=11112, aet_target="MERCURE"
    )


# ── 1. single instance is auto-enqueued after the idle delay ──────────


def test_single_instance_auto_enqueued(tmp_path: Path, hub: DICOMDestination) -> None:
    spool = auto_spool(tmp_path, [hub])
    study_id = spool.store_instance(make_dataset("1.2.3.1", "1.2.3.1.1", "1.2.3.1.1.1"))

    assert spool.state(study_id) == StudyState.RECEIVED

    assert wait_until(lambda: spool.state(study_id) == StudyState.QUEUED)
    routes = spool._db.get_routes(study_id)
    assert [r["target_name"] for r in routes] == ["hub"]
    assert routes[0]["status"] == "waiting"


# ── 2. multi-instance study enqueued once, after the last instance ────


def test_multi_instance_enqueued_after_last_instance(
    tmp_path: Path, hub: DICOMDestination
) -> None:
    spool = auto_spool(tmp_path, [hub])
    study_uid = "1.2.3.2"
    for i in ("1", "2", "3"):
        spool.store_instance(make_dataset(study_uid, f"{study_uid}.1", f"{study_uid}.1.{i}"))

    study_id = int(spool._db.get_study_by_uid(study_uid)["id"])
    # The timer was rescheduled by every instance: still RECEIVED right after
    # the last store (deterministic — the delay has not elapsed).
    assert spool.state(study_id) == StudyState.RECEIVED

    assert wait_until(lambda: spool.state(study_id) == StudyState.QUEUED)
    row = spool._db.get_study(study_id)
    assert row["num_instances"] == 3, "forwarded before the study was fully received"
    assert row["num_series"] == 1


# ── 2b. distinct studies arriving within one delay window all enqueue ──
#
# E1 dry-run found the shipped behavior: one Spool-wide timer that each store
# replaced, so study N's arrival cancelled study N-1's pending auto-enqueue —
# a burst of small studies (stat batch from a modality) left all but the last
# stranded in RECEIVED forever, routes and all. Debounce must be per-study:
# re-arming study X replaces *X's* timer only, never a different study's.


def test_burst_of_distinct_studies_all_auto_enqueue(tmp_path: Path, hub: DICOMDestination) -> None:
    spool = auto_spool(tmp_path, [hub], delay=1.0)
    ids = [
        spool.store_instance(make_dataset(f"1.2.3.2{i[-1]}", f"1.2.3.2{i}.1", f"1.2.3.2{i}.1.1"))
        for i in ("11", "12", "13")
    ]  # 3 stores, comfortably inside one 1 s delay window

    assert wait_until(lambda: all(spool.state(i) == StudyState.QUEUED for i in ids), timeout=6.0)
    for study_id in ids:
        assert [r["target_name"] for r in spool._db.get_routes(study_id)] == ["hub"]


def test_stop_cancels_all_pending_timers(tmp_path: Path, hub: DICOMDestination) -> None:
    spool = auto_spool(tmp_path, [hub], delay=1.0)
    ids = [
        spool.store_instance(make_dataset(f"1.2.3.9{x}", f"1.2.3.9{x}.1", f"1.2.3.9{x}.1.1"))
        for x in (11, 12)
    ]
    spool.stop()
    time.sleep(1.4)  # past the delay: cancelled timers must not fire
    assert all(spool.state(i) == StudyState.RECEIVED for i in ids)


# ── 3. no destinations configured → stays RECEIVED ────────────────────


def test_no_destinations_stays_received(tmp_path: Path) -> None:
    spool = auto_spool(tmp_path, [], delay=0.05)
    study_id = spool.store_instance(make_dataset("1.2.3.3", "1.2.3.3.1", "1.2.3.3.1.1"))

    time.sleep(0.3)
    assert spool.state(study_id) == StudyState.RECEIVED
    assert spool._db.get_routes(study_id) == []


# ── 4. a late timer never demotes a study past RECEIVED ───────────────


def test_late_timer_does_not_demote_queued_study(
    tmp_path: Path, hub: DICOMDestination
) -> None:
    spool = auto_spool(tmp_path, [hub])
    study_id = spool.store_instance(make_dataset("1.2.3.4", "1.2.3.4.1", "1.2.3.4.1.1"))

    # Operator/service enqueues manually before the timer fires.
    spool.enqueue(study_id, [hub])
    assert spool.state(study_id) == StudyState.QUEUED

    time.sleep(_DELAY_SEC + 0.3)  # let the auto-enqueue timer fire
    assert spool.state(study_id) == StudyState.QUEUED
    assert len(spool._db.get_routes(study_id)) == 1, "route duplicated by late timer"


# ── 5. end-to-end through the Receiver C-STORE path ───────────────────


def test_receiver_path_auto_enqueues(tmp_path: Path, hub: DICOMDestination) -> None:
    import socket

    from pynetdicom import AE

    from mercure_gateway.receiver import Receiver

    cfg = default_config()
    cfg.storage.spool_dir = str(tmp_path / "spool")
    cfg.receiver.auto_enqueue_delay_sec = 0.1
    cfg.destinations = [hub]
    spool = Spool(mem_database(), cfg)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    receiver = Receiver(ReceiverConfig(ae_title="GATEWAY", port=port), spool)
    receiver.start()
    try:
        ds = make_dataset("1.2.3.5", "1.2.3.5.1", "1.2.3.5.1.1")
        ae = AE(ae_title="MODALITY")
        ae.add_requested_context(CTImageStorage)
        assoc = ae.associate("127.0.0.1", port, ae_title="GATEWAY")
        assert assoc.is_established
        status = assoc.send_c_store(ds)
        assoc.release()
        assert status is not None and status.Status in (0x0000, 0xFF00)

        study_id = int(spool._db.get_study_by_uid("1.2.3.5")["id"])
        assert wait_until(lambda: spool.state(study_id) == StudyState.QUEUED)
        assert [r["target_name"] for r in spool._db.get_routes(study_id)] == ["hub"]
    finally:
        receiver.stop()
