"""TDD: unit tests for the DICOM C-STORE SCP Receiver (PRD §5.2 step 1-2).

Behaviors covered (in RED-GREEN order):
1.  SCU sends one CT instance → study RECEIVED, DICOM file on disk  (tracer bullet)
2.  Second instance of the same study (new association) → same study, num_instances=2
3.  Tags extracted (modality/accession/patient_name) stored on the study row
4.  AE allow-list: unlisted SCU association rejected, listed accepted
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian
from pynetdicom import AE

from mercure_gateway.config import ReceiverConfig, default_config
from mercure_gateway.receiver import Receiver
from mercure_gateway.spool import Spool, StudyState
from mercure_gateway.spool.db import mem_database


def free_port() -> int:
    """Find an ephemeral free port (best-effort; fine for tests)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def make_spool(tmp_path: Path) -> Spool:
    cfg = default_config()
    cfg.storage.spool_dir = str(tmp_path / "spool")
    return Spool(mem_database(), cfg)


def make_dataset(
    study_uid: str, series_uid: str = "1.2.3.4.5.6.1", instance_uid: str | None = None
) -> Dataset:
    ds = Dataset()
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.SOPInstanceUID = instance_uid or f"{study_uid}.1"
    ds.SOPClassUID = CTImageStorage
    ds.PatientName = "TEST^PATIENT"
    ds.Modality = "CT"
    ds.AccessionNumber = "ACC-001"
    # pynetdicom send_c_store requires file_meta with TransferSyntaxUID
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = CTImageStorage
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    return ds


def send_ct(host: str, port: int, aet_called: str, dataset: Dataset) -> bool:
    """Send one CT instance to an SCP; returns True if C-STORE succeeded."""
    ae = AE(ae_title="MODALITY")
    ae.add_requested_context(CTImageStorage)
    assoc = ae.associate(host, port, ae_title=aet_called)
    if not assoc.is_established:
        return False
    status = assoc.send_c_store(dataset)
    assoc.release()
    return status is not None and status.Status in (0x0000, 0xFF00)


@pytest.fixture()
def receiver(tmp_path: Path) -> Receiver:
    spool = make_spool(tmp_path)
    cfg = ReceiverConfig(ae_title="GATEWAY", port=free_port())
    recv = Receiver(cfg, spool)
    recv.start()
    yield recv
    recv.stop()


# ── Slice 1 (tracer bullet): accept one CT instance ────────────────────


def test_receiver_accepts_ct_instance(tmp_path: Path, receiver: Receiver) -> None:
    study_uid = "1.2.3.4.5.6"

    ok = send_ct("127.0.0.1", receiver.port, "GATEWAY", make_dataset(study_uid))

    assert ok is True
    studies = receiver.spool._db.list_studies()
    assert len(studies) == 1
    assert studies[0]["study_uid"] == study_uid
    assert spool_state(receiver, studies[0]["id"]) == StudyState.RECEIVED
    # File persisted to disk at spool/{study_uid}/...
    files = list((tmp_path / "spool" / study_uid).rglob("*.dcm"))
    assert len(files) == 1


# ── Slice 2: second instance of the same study upserts ─────────────────


def test_second_instance_same_study_upserts(
    tmp_path: Path, receiver: Receiver
) -> None:
    study_uid = "1.2.3.4.5.6"
    series_uid = "1.2.3.4.5.6.1"

    ok1 = send_ct(
        "127.0.0.1", receiver.port, "GATEWAY",
        make_dataset(study_uid, series_uid, "1.2.3.4.5.6.1.1"),
    )
    ok2 = send_ct(
        "127.0.0.1", receiver.port, "GATEWAY",
        make_dataset(study_uid, series_uid, "1.2.3.4.5.6.1.2"),
    )

    assert ok1 is True and ok2 is True
    studies = receiver.spool._db.list_studies()
    assert len(studies) == 1  # same study row, not duplicated
    assert studies[0]["study_uid"] == study_uid
    files = list((tmp_path / "spool" / study_uid).rglob("*.dcm"))
    assert len(files) == 2  # two instances persisted


# ── Slice 3: tags extracted onto the study row ─────────────────────────


def test_tags_extracted_onto_study_row(
    tmp_path: Path, receiver: Receiver
) -> None:
    ds = make_dataset("1.2.3.4.5.6")
    ds.Modality = "CT"
    ds.PatientName = "DOE^JOHN"
    ds.AccessionNumber = "ACC-777"

    ok = send_ct("127.0.0.1", receiver.port, "GATEWAY", ds)

    assert ok is True
    studies = receiver.spool._db.list_studies()
    row = studies[0]
    assert row["modality"] == "CT"
    assert row["patient_name"] == "DOE^JOHN"
    assert row["accession"] == "ACC-777"


# ── Slice 4: AE allow-list enforcement ─────────────────────────────────


def test_allowed_ae_titles_rejects_unknown(tmp_path: Path) -> None:
    spool = make_spool(tmp_path)
    cfg = ReceiverConfig(
        ae_title="GATEWAY",
        port=free_port(),
        allowed_ae_titles=["KNOWNMOD"],
    )
    recv = Receiver(cfg, spool)
    recv.start()
    try:
        # "MODALITY" is not in the allow-list -> rejected
        ok = send_ct("127.0.0.1", recv.port, "GATEWAY", make_dataset("1.2.3.4"))
        assert ok is False
        assert recv.spool._db.list_studies() == []
    finally:
        recv.stop()


def test_allowed_ae_titles_accepts_listed(tmp_path: Path) -> None:
    spool = make_spool(tmp_path)
    cfg = ReceiverConfig(
        ae_title="GATEWAY",
        port=free_port(),
        allowed_ae_titles=["MODALITY"],
    )
    recv = Receiver(cfg, spool)
    recv.start()
    try:
        ok = send_ct("127.0.0.1", recv.port, "GATEWAY", make_dataset("1.2.3.4"))
        assert ok is True
        assert len(recv.spool._db.list_studies()) == 1
    finally:
        recv.stop()


def spool_state(receiver: Receiver, study_id: int) -> StudyState:
    return receiver.spool.state(study_id)
