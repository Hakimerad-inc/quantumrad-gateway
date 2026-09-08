"""TDD (S02-T5, GREEN): study ingest lifecycle wiring.

A multi-instance study flows: first instance → RECEIVED row created;
subsequent instances upsert the row (series/instance counters advance);
tags extracted to the DB row; per-instance provenance recorded in
``instance_meta``. Enqueue to enabled destinations then moves the study
RECEIVED → QUEUED (reuses ``Spool.receive``/``enqueue`` semantics).

Also covered: two different series in one study advance ``num_series``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, MRImageStorage

from mercure_gateway.config import DICOMDestination, default_config
from mercure_gateway.spool import Spool, StudyState
from mercure_gateway.spool.db import mem_database


def make_dataset(
    study_uid: str,
    series_uid: str,
    instance_uid: str,
    sop_class: str = CTImageStorage,
    modality: str = "CT",
) -> Dataset:
    ds = Dataset()
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.SOPInstanceUID = instance_uid
    ds.SOPClassUID = sop_class
    ds.Modality = modality
    ds.PatientName = "TEST^P"
    ds.PatientID = "PID-1"
    ds.AccessionNumber = "ACC-1"
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = sop_class
    ds.file_meta.MediaStorageSOPInstanceUID = instance_uid
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    return ds


@pytest.fixture()
def spool(tmp_path: Path) -> Spool:
    cfg = default_config()
    cfg.storage.spool_dir = str(tmp_path / "spool")
    return Spool(mem_database(), cfg)


class TestStudyIngestLifecycle:
    def test_multi_instance_study_counters_and_state(self, spool: Spool) -> None:
        study_uid = "1.2.3.4.5"
        sid1 = spool.store_instance(make_dataset(study_uid, f"{study_uid}.1", f"{study_uid}.1.1"))
        sid2 = spool.store_instance(make_dataset(study_uid, f"{study_uid}.1", f"{study_uid}.1.2"))

        assert sid1 == sid2, "both instances belong to the same study row"
        row = spool._db.get_study(sid1)
        assert row["num_instances"] == 2
        assert row["num_series"] == 1
        assert row["state"] == StudyState.RECEIVED.value

    def test_two_series_advance_num_series(self, spool: Spool) -> None:
        study_uid = "1.2.3.4.6"
        spool.store_instance(make_dataset(study_uid, f"{study_uid}.1", f"{study_uid}.1.1"))
        spool.store_instance(make_dataset(study_uid, f"{study_uid}.2", f"{study_uid}.2.1"))

        row = spool._db.get_study_by_uid(study_uid)
        assert row["num_series"] == 2
        assert row["num_instances"] == 2

    def test_instance_meta_provenance_recorded(self, spool: Spool) -> None:
        study_uid = "1.2.3.4.7"
        spool.store_instance(make_dataset(study_uid, f"{study_uid}.1", f"{study_uid}.1.1"))

        metas = spool._db.list_instance_meta(study_uid)
        assert len(metas) == 1
        assert metas[0]["received_syntax"] == str(ExplicitVRLittleEndian)
        assert metas[0]["stored_syntax"] == str(ExplicitVRLittleEndian)
        assert metas[0]["num_bytes"] > 0
        assert metas[0]["file_path"].endswith(".dcm")

    def test_full_receive_then_enqueue_flow(self, spool: Spool) -> None:
        """Ingest walk (US-01/US-02): 3 instances / 2 series → RECEIVED →
        enqueue to enabled destinations → QUEUED with routes created."""
        study_uid = "1.2.3.4.8"
        for series, instance in (("1", "1"), ("1", "2"), ("2", "1")):
            spool.store_instance(
                make_dataset(study_uid, f"{study_uid}.{series}", f"{study_uid}.{series}.{instance}")
            )

        study_id = spool._db.get_study_by_uid(study_uid)["id"]
        assert spool.state(study_id) == StudyState.RECEIVED

        hub = DICOMDestination(name="hub", host="hub.local", port=11112, aet_target="MERCURE")
        disabled = DICOMDestination(name="off", host="x", port=1, aet_target="X")
        disabled.enabled = False
        spool.enqueue(study_id, [hub, disabled])

        assert spool.state(study_id) == StudyState.QUEUED
        routes = spool._db.get_routes(study_id)
        assert [r["target_name"] for r in routes] == ["hub"]

    def test_mr_study_row_from_mr_dataset(self, spool: Spool) -> None:
        """MR datasets (SOP class MRImageStorage) ingest identically."""
        study_uid = "1.2.3.4.9"
        spool.store_instance(
            make_dataset(
                study_uid,
                f"{study_uid}.1",
                f"{study_uid}.1.1",
                sop_class=MRImageStorage,
                modality="MR",
            )
        )

        row = spool._db.get_study_by_uid(study_uid)
        assert row["modality"] == "MR"
        assert row["state"] == StudyState.RECEIVED.value
