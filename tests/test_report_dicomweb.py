"""S08-T4 (RED): DICOMweb QIDO/WADO report transport (PRD §2.3, Q2).

Retrieves SR/PDF reports from a DICOMweb server: QIDO-RS to locate the report
instances, WADO-RS to fetch them over HTTPS.  Behaviors:

1. find() issues a QIDO-RS query filtered by StudyInstanceUID / AccessionNumber
   and returns ReportMatch records for SR and PDF instances
2. QIDO-RS results are followed across pages (offset pagination)
3. retrieve() fetches each instance via WADO-RS and saves it to disk
4. HTTPS is used and TLS verification is controlled (verify flag)
5. HTTP errors map to a transport error (no silent data loss)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from mercure_gateway.config import ReportQuerySource


def _qido_instance(
    sop_class: str,
    sop_instance_uid: str,
    study_uid: str = "1.2.3",
    series_uid: str = "1.2.3.4",
) -> dict:
    """One QIDO-RS instance row (DICOMweb JSON attribute format)."""
    return {
        "00080005": {"vr": "CS", "Value": ["ISO_IR 192"]},
        "00080020": {"vr": "DA", "Value": ["20250101"]},
        "00080050": {"vr": "SH"},
        "0020000D": {"vr": "UI", "Value": [study_uid]},
        "0020000E": {"vr": "UI", "Value": [series_uid]},
        "00080016": {"vr": "UI", "Value": [sop_class]},
        "00080018": {"vr": "UI", "Value": [sop_instance_uid]},
    }


SR_SOP = "1.2.840.10008.5.1.4.1.1.88.33"
PDF_SOP = "1.2.840.10008.5.1.4.1.1.104.2"


@pytest.fixture()
def qs() -> ReportQuerySource:
    return ReportQuerySource(
        type="dicomweb", host="pacs.local", port=443, aet="GATEWAY"
    )


# ══════════════════════════════════════════════════════════════════════
# find(): QIDO-RS query
# ══════════════════════════════════════════════════════════════════════

@patch("requests.get")
def test_find_issues_qido_query(mock_get: MagicMock) -> None:
    """find() queries QIDO-RS with the study UID and returns matches."""
    from mercure_gateway.reports.dicomweb import DICOMwebReportTransport

    resp = MagicMock()
    resp.ok = True
    resp.json.return_value = [
        _qido_instance(SR_SOP, "1.2.3.4.5.6", study_uid="1.2.3"),
        _qido_instance(PDF_SOP, "1.2.3.4.5.7", study_uid="1.2.3"),
    ]
    mock_get.return_value = resp

    transport = DICOMwebReportTransport(
        base_url="https://pacs.local:443/dicomweb", reports_dir=Path("/tmp/r")
    )
    matches = transport.find(study_uid="1.2.3", report_types=["sr", "pdf"])

    assert len(matches) == 2
    assert matches[0].sop_class_uid == SR_SOP
    assert matches[1].sop_class_uid == PDF_SOP
    # QIDO-RS query URL carries the study UID and include fields
    url = mock_get.call_args[0][0]
    assert "StudyInstanceUID=1.2.3" in url
    assert "SOPClassUID" in url


@patch("requests.get")
def test_find_by_accession(mock_get: MagicMock) -> None:
    """find() can query by AccessionNumber instead of study UID."""
    from mercure_gateway.reports.dicomweb import DICOMwebReportTransport

    resp = MagicMock()
    resp.ok = True
    resp.json.return_value = [_qido_instance(SR_SOP, "1.2.3.4.5.6")]
    mock_get.return_value = resp

    transport = DICOMwebReportTransport(
        base_url="https://pacs.local:443/dicomweb", reports_dir=Path("/tmp/r")
    )
    matches = transport.find(accession="ACC-001", report_types=["sr"])

    url = mock_get.call_args[0][0]
    assert "AccessionNumber=ACC-001" in url
    assert len(matches) == 1


@patch("requests.get")
def test_find_follows_pagination(mock_get: MagicMock) -> None:
    """QIDO-RS paged results are followed via the Link rel=next header."""
    from mercure_gateway.reports.dicomweb import DICOMwebReportTransport

    page1 = MagicMock()
    page1.ok = True
    page1.json.return_value = [
        _qido_instance(SR_SOP, "1.2.3.4.5.6", study_uid="1.2.3"),
        _qido_instance(SR_SOP, "1.2.3.4.5.7", study_uid="1.2.3"),
    ]
    page1.headers = {"Link": '<https://pacs.local:443/dicomweb/studies?offset=2>; rel="next"'}
    page2 = MagicMock()
    page2.ok = True
    page2.json.return_value = [
        _qido_instance(SR_SOP, "1.2.3.4.5.8", study_uid="1.2.3"),
    ]
    page2.headers = {"Link": ""}
    mock_get.side_effect = [page1, page2]

    transport = DICOMwebReportTransport(
        base_url="https://pacs.local:443/dicomweb", reports_dir=Path("/tmp/r")
    )
    matches = transport.find(study_uid="1.2.3", report_types=["sr"])

    assert len(matches) == 3
    assert mock_get.call_count == 2
    # The second page is fetched from the Link header's next URL
    assert "offset=2" in mock_get.call_args_list[1][0][0]


@patch("requests.get")
def test_find_filters_report_types(mock_get: MagicMock) -> None:
    """Only the requested report SOP classes are returned."""
    from mercure_gateway.reports.dicomweb import DICOMwebReportTransport

    resp = MagicMock()
    resp.ok = True
    resp.json.return_value = [
        _qido_instance(SR_SOP, "1.2.3.4.5.6", study_uid="1.2.3"),
        _qido_instance(PDF_SOP, "1.2.3.4.5.7", study_uid="1.2.3"),
    ]
    mock_get.return_value = resp

    transport = DICOMwebReportTransport(
        base_url="https://pacs.local:443/dicomweb", reports_dir=Path("/tmp/r")
    )
    matches = transport.find(study_uid="1.2.3", report_types=["pdf"])

    assert [m.sop_class_uid for m in matches] == [PDF_SOP]


@patch("requests.get")
def test_find_http_error_raises(mock_get: MagicMock) -> None:
    """A non-OK QIDO-RS response maps to a transport error (no silent data loss)."""
    from mercure_gateway.reports.dicomweb import DICOMwebError, DICOMwebReportTransport

    resp = MagicMock()
    resp.ok = False
    resp.status_code = 500
    mock_get.return_value = resp

    transport = DICOMwebReportTransport(
        base_url="https://pacs.local:443/dicomweb", reports_dir=Path("/tmp/r")
    )
    with pytest.raises(DICOMwebError, match="500"):
        transport.find(study_uid="1.2.3")


# ══════════════════════════════════════════════════════════════════════
# retrieve(): WADO-RS fetch
# ══════════════════════════════════════════════════════════════════════

@patch("requests.get")
def test_retrieve_wado_saves_dicom(mock_get: MagicMock, tmp_path: Path) -> None:
    """WADO-RS bytes are parsed and saved under reports/<study>/<type>/."""
    # Build a minimal SR DICOM dataset.
    import io

    from pydicom.dataset import Dataset
    from pydicom.uid import ExplicitVRLittleEndian

    from mercure_gateway.reports.dicomweb import DICOMwebReportTransport
    from mercure_gateway.reports.find import ReportMatch

    ds = Dataset()
    ds.SOPClassUID = SR_SOP
    ds.SOPInstanceUID = "1.2.3.4.5.6"
    ds.StudyInstanceUID = "1.2.3"
    ds.SeriesInstanceUID = "1.2.3.4"
    ds.file_meta = Dataset()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)

    resp = MagicMock()
    resp.ok = True
    resp.headers.get.return_value = "application/dicom"
    resp.content = buf.getvalue()
    mock_get.return_value = resp

    transport = DICOMwebReportTransport(
        base_url="https://pacs.local:443/dicomweb", reports_dir=tmp_path
    )
    match = ReportMatch(
        sop_class_uid=SR_SOP,
        study_uid="1.2.3",
        series_uid="1.2.3.4",
        sop_instance_uid="1.2.3.4.5.6",
    )
    saved = transport.retrieve([match])

    assert len(saved) == 1
    assert saved[0].file_path.exists()
    assert saved[0].file_path.suffix == ".dcm"
    assert saved[0].sop_class_uid == SR_SOP
    assert saved[0].report_type == "sr"


@patch("requests.get")
def test_retrieve_wado_failure_raises(mock_get: MagicMock) -> None:
    """A WADO-RS failure raises DICOMwebError instead of silently losing data."""
    from mercure_gateway.reports.dicomweb import DICOMwebError, DICOMwebReportTransport
    from mercure_gateway.reports.find import ReportMatch

    resp = MagicMock()
    resp.ok = False
    resp.status_code = 404
    mock_get.return_value = resp

    transport = DICOMwebReportTransport(
        base_url="https://pacs.local:443/dicomweb", reports_dir=Path("/tmp/r")
    )
    match = ReportMatch(
        sop_class_uid=SR_SOP,
        study_uid="1.2.3",
        series_uid="1.2.3.4",
        sop_instance_uid="1.2.3.4.5.6",
    )
    with pytest.raises(DICOMwebError, match="404"):
        transport.retrieve([match])


# ══════════════════════════════════════════════════════════════════════
# TLS
# ══════════════════════════════════════════════════════════════════════

@patch("requests.get")
def test_tls_verification_controlled_by_flag(mock_get: MagicMock) -> None:
    """The verify flag controls TLS certificate verification on WADO-RS."""
    from mercure_gateway.reports.dicomweb import DICOMwebReportTransport
    from mercure_gateway.reports.find import ReportMatch

    resp = MagicMock()
    resp.ok = True
    resp.headers.get.return_value = "application/dicom"
    resp.content = b""
    mock_get.return_value = resp

    transport = DICOMwebReportTransport(
        base_url="https://pacs.local:443/dicomweb",
        reports_dir=Path("/tmp/r"),
        verify_tls=False,
    )
    match = ReportMatch(
        sop_class_uid=SR_SOP,
        study_uid="1.2.3",
        series_uid="1.2.3.4",
        sop_instance_uid="1.2.3.4.5.6",
    )

    import contextlib

    with contextlib.suppress(Exception):
        transport.retrieve([match])

    assert mock_get.called
    assert mock_get.call_args[1].get("verify") is False


# ══════════════════════════════════════════════════════════════════════
# Query-source factory wiring
# ══════════════════════════════════════════════════════════════════════

def test_factory_builds_from_query_source(tmp_path: Path) -> None:
    """A dicomweb query source builds a DICOMweb transport via the factory."""
    from mercure_gateway.reports.dicomweb import build_dicomweb_transport

    transport = build_dicomweb_transport(
        ReportQuerySource(type="dicomweb", host="pacs.local", port=443, aet="GATEWAY"),
        reports_dir=tmp_path,
    )
    assert callable(transport.find)
    assert callable(transport.retrieve)


# ══════════════════════════════════════════════════════════════════════
# Path-traversal guard (P0-1): the UIDs in a QIDO match are server-controlled
# and compose the save path, so a malicious DICOMweb server can otherwise write
# anywhere the process can. _save must reject them before touching the FS.
# ══════════════════════════════════════════════════════════════════════


def _empty_dataset() -> Any:
    """A dataset the traversal tests never actually serialize.

    ``_save`` must reject a malicious UID *before* it reaches ``save_as``, so
    the payload need not be a valid, serializable DICOM object — if validation
    works, ``save_as`` is never called.
    """
    from pydicom.dataset import Dataset

    return Dataset()


@pytest.mark.parametrize(
    ("study_uid", "sop_instance_uid"),
    [
        ("../../etc", "1.2.3.4"),
        ("1.2.3", "../../evil"),
        ("1.2.3", ".."),
        ("", "1.2.3.4"),
        ("1.2.3", ""),
    ],
)
def test_save_rejects_uids_that_escape_reports_dir(
    tmp_path: Path, study_uid: str, sop_instance_uid: str
) -> None:
    """The WADO save path refuses UIDs that could traverse out of the sandbox."""
    from mercure_gateway.reports.dicomweb import DICOMwebReportTransport
    from mercure_gateway.reports.find import ReportMatch
    from mercure_gateway.spool import InvalidUIDError

    transport = DICOMwebReportTransport(
        base_url="https://pacs.local:443/dicomweb", reports_dir=tmp_path
    )
    match = ReportMatch(
        sop_class_uid=SR_SOP,
        study_uid=study_uid,
        series_uid="1.2.3.4",
        sop_instance_uid=sop_instance_uid,
    )

    with pytest.raises(InvalidUIDError):
        transport._save(_empty_dataset(), match)

    assert not list(tmp_path.rglob("*.dcm"))


def test_save_rejects_overlong_uid(tmp_path: Path) -> None:
    """A UID longer than the 64-char DICOM limit is rejected, not truncated."""
    from mercure_gateway.reports.dicomweb import DICOMwebReportTransport
    from mercure_gateway.reports.find import ReportMatch
    from mercure_gateway.spool import InvalidUIDError

    transport = DICOMwebReportTransport(
        base_url="https://pacs.local:443/dicomweb", reports_dir=tmp_path
    )
    match = ReportMatch(
        sop_class_uid=SR_SOP,
        study_uid="1." * 40,  # 79 chars
        series_uid="1.2.3.4",
        sop_instance_uid="1.2.3.4.5",
    )

    with pytest.raises(InvalidUIDError):
        transport._save(_empty_dataset(), match)
