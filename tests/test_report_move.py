"""S05-T2 (RED): Report retrieve transport — C-MOVE SCU (US-05, refinement §2.3).

``ReportRetrieve`` runs its own C-STORE SCP on an internal port and issues
C-MOVE to the PACS.  Received instances are saved under
``reports/{study_uid}/sr/`` (SR) or ``reports/{study_uid}/pdf/`` (PDF).
The full C-MOVE roundtrip is tested by the Orthanc test rig; these unit tests
verify the file storage logic and error handling.

Behaviors:
1. C-MOVE with an unreachable PACS raises ``ReportRetrieveError``
2. A received SR instance is saved to the ``sr/`` subdirectory
3. A received PDF instance is saved to the ``pdf/`` subdirectory
4. The file path is set correctly on the ``RetrievedReport``
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

from mercure_gateway.reports.find import PDF_SOP_CLASS, SR_SOP_CLASS, ReportMatch


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _make_instance(study_uid: str, sop_class: str, sop_uid: str) -> Dataset:
    ds = Dataset()
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = "1.2.3.4.5.6.100"
    ds.SOPInstanceUID = sop_uid
    ds.SOPClassUID = sop_class
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = sop_class
    ds.file_meta.MediaStorageSOPInstanceUID = sop_uid
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.PatientName = "TEST^PATIENT"
    ds.Modality = "SR" if sop_class == SR_SOP_CLASS else "DOC"
    ds.PatientID = "P001"
    ds.AccessionNumber = "ACC-001"
    return ds


def test_connection_error_raises() -> None:
    from mercure_gateway.reports.move import ReportRetrieve, ReportRetrieveError

    port = free_port()  # nothing listening on this port
    retrieve = ReportRetrieve(
        host="127.0.0.1",
        port=port,
        aet="PACS",
        store_scp_port=free_port(),
        store_scp_ae_title="GATEWAY",
        reports_dir=Path("/tmp/reports"),
    )
    match = ReportMatch(
        sop_class_uid=SR_SOP_CLASS,
        study_uid="1.2.840.99",
        series_uid="1.2.3.4.5.6.100",
        sop_instance_uid="1.2.3.4.5.6.7.1",
    )
    with pytest.raises(ReportRetrieveError):
        retrieve.retrieve([match])


def test_save_sr_instance(tmp_path: Path) -> None:
    from mercure_gateway.reports.move import ReportRetrieve

    sr_uid = "1.2.3.4.5.6.7.1"
    study_uid = "1.2.840.1"
    sr_instance = _make_instance(study_uid, SR_SOP_CLASS, sr_uid)

    retrieve = ReportRetrieve(
        host="127.0.0.1",
        port=free_port(),
        aet="PACS",
        store_scp_port=free_port(),
        store_scp_ae_title="GATEWAY",
        reports_dir=tmp_path / "reports",
    )
    saved = retrieve._save(sr_instance, study_uid, SR_SOP_CLASS, sr_uid)

    assert saved.exists()
    assert "sr" in str(saved)
    assert sr_uid in str(saved)
    # Verify it re-reads cleanly
    from pydicom import dcmread

    reloaded = dcmread(str(saved))
    assert str(reloaded.StudyInstanceUID) == study_uid


def test_save_pdf_instance(tmp_path: Path) -> None:
    from mercure_gateway.reports.move import ReportRetrieve

    pdf_uid = "1.2.3.4.5.6.7.2"
    study_uid = "1.2.840.2"
    pdf_instance = _make_instance(study_uid, PDF_SOP_CLASS, pdf_uid)

    retrieve = ReportRetrieve(
        host="127.0.0.1",
        port=free_port(),
        aet="PACS",
        store_scp_port=free_port(),
        store_scp_ae_title="GATEWAY",
        reports_dir=tmp_path / "reports",
    )
    saved = retrieve._save(pdf_instance, study_uid, PDF_SOP_CLASS, pdf_uid)

    assert saved.exists()
    assert "pdf" in str(saved)
    assert pdf_uid in str(saved)


def test_save_creates_parent_dirs(tmp_path: Path) -> None:
    from mercure_gateway.reports.move import ReportRetrieve

    ds = _make_instance("1.2.3", SR_SOP_CLASS, "1.2.3.4")
    deep = tmp_path / "nested" / "reports"

    retrieve = ReportRetrieve(
        host="127.0.0.1",
        port=free_port(),
        aet="PACS",
        store_scp_port=free_port(),
        store_scp_ae_title="GATEWAY",
        reports_dir=deep,
    )
    saved = retrieve._save(ds, "1.2.3", SR_SOP_CLASS, "1.2.3.4")

    assert saved.exists()
    assert deep.exists()
