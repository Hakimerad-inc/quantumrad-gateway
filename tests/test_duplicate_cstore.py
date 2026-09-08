"""TDD (review F6): duplicate C-STORE must not corrupt study lifecycle state.

Modalities routinely re-send instances after a timeout (normal DICOM
retry). The receive upsert must therefore distinguish a *duplicate*
(SOPInstanceUID already stored) from a *genuinely new* instance:

1. A duplicate C-STORE for a SENT study leaves the study SENT — no demotion
   to RECEIVED (which would strand it: no route to claim, retention skipped).
2. A duplicate for a QUEUED/ERROR/FAILED study leaves the state untouched
   and does not double-count instances/series.
3. A genuinely NEW instance of a delivered (SENT) study re-opens it: state
   → RECEIVED and complete routes → waiting so the updated study re-forwards.
4. Counters stay consistent: duplicates never increment; new instances do.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian

from mercure_gateway.config import DICOMDestination, default_config
from mercure_gateway.spool import Spool, StudyState
from mercure_gateway.spool.db import mem_database


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


@pytest.fixture()
def spool(tmp_path: Path) -> Spool:
    cfg = default_config()
    cfg.storage.spool_dir = str(tmp_path / "spool")
    # Disable the auto-enqueue timer to keep state transitions deterministic.
    cfg.receiver.auto_enqueue_delay_sec = 0
    return Spool(mem_database(), cfg)


STUDY = "1.2.9.1"
SERIES = "1.2.9.1.1"
INST1 = "1.2.9.1.1.1"


def test_duplicate_cstore_keeps_sent_study_sent(spool: Spool, target_hub: DICOMDestination) -> None:
    """Modality timeout re-send of an already-delivered instance must be a
    no-op for state and counters (review F6)."""
    study_id = spool.store_instance(make_dataset(STUDY, SERIES, INST1))
    spool.enqueue(study_id, [target_hub])
    spool.claim_next(limit=1)
    spool.complete(study_id, "hub")
    assert spool.state(study_id) == StudyState.SENT

    # Duplicate C-STORE of the same instance (e.g. modality retry).
    spool.store_instance(make_dataset(STUDY, SERIES, INST1))

    assert spool.state(study_id) == StudyState.SENT
    row = spool._db.get_study(study_id)
    assert row["num_instances"] == 1, "duplicate counted as a new instance"
    assert row["num_series"] == 1
    routes = spool._db.get_routes(study_id)
    assert all(r["status"] == "complete" for r in routes), "complete route re-opened"


def test_duplicate_cstore_keeps_queued_study_queued(
    spool: Spool, target_hub: DICOMDestination
) -> None:
    """Duplicate while QUEUED: no demotion, no counter drift."""
    study_id = spool.store_instance(make_dataset(STUDY, SERIES, INST1))
    spool.enqueue(study_id, [target_hub])
    assert spool.state(study_id) == StudyState.QUEUED

    spool.store_instance(make_dataset(STUDY, SERIES, INST1))

    assert spool.state(study_id) == StudyState.QUEUED
    row = spool._db.get_study(study_id)
    assert row["num_instances"] == 1
    assert row["num_series"] == 1


def test_duplicate_cstore_keeps_failed_study_failed(
    spool: Spool, target_hub: DICOMDestination
) -> None:
    """A FAILED study must not be silently resurrected to RECEIVED with
    errored routes (review F6) — operators re-forward it explicitly."""
    study_id = spool.store_instance(make_dataset(STUDY, SERIES, INST1))
    spool.enqueue(study_id, [target_hub])
    spool.claim_next(limit=1)
    spool.fail(study_id, "hub", "connection refused", max_attempts=1)
    assert spool.state(study_id) == StudyState.FAILED

    spool.store_instance(make_dataset(STUDY, SERIES, INST1))

    assert spool.state(study_id) == StudyState.FAILED
    assert all(r["status"] == "error" for r in spool._db.get_routes(study_id))


def test_new_instance_of_sent_study_requeues(spool: Spool, target_hub: DICOMDestination) -> None:
    """A genuinely NEW instance for a delivered study (late-arriving series)
    re-opens the study: RECEIVED state and complete routes back to waiting."""
    study_id = spool.store_instance(make_dataset(STUDY, SERIES, INST1))
    spool.enqueue(study_id, [target_hub])
    spool.claim_next(limit=1)
    spool.complete(study_id, "hub")
    assert spool.state(study_id) == StudyState.SENT

    spool.store_instance(make_dataset(STUDY, SERIES, "1.2.9.1.1.2"))

    assert spool.state(study_id) == StudyState.RECEIVED
    row = spool._db.get_study(study_id)
    assert row["num_instances"] == 2
    routes = spool._db.get_routes(study_id)
    assert all(r["status"] == "waiting" for r in routes), "delivered route not re-queued"
