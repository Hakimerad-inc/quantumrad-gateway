"""S06-T2 (RED): Full web admin REST API contract (product refinement §7).

Closes the v0 stubs from ``test_web_api.py`` with the real behaviors the SPA
needs:

1. ``PUT /config`` persists a real config update (not a stub)
2. ``POST /studies/{id}/reports`` creates a report row (on-demand, US-06)
3. ``POST /reports/{id}/refresh`` actually triggers the retriever
4. ``GET /reports/{id}/content`` renders SR text / extracts PDF bytes
5. OpenAPI schema is valid and self-describing
"""

from __future__ import annotations

import pytest
from conftest import FakeForwarder, FakeReceiver
from fastapi.testclient import TestClient
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

from mercure_gateway.config import default_config
from mercure_gateway.spool import Spool
from mercure_gateway.spool.db import mem_database
from mercure_gateway.web import create_app

_SR_SOP = "1.2.840.10008.5.1.4.1.1.88.33"
_PDF_SOP = "1.2.840.10008.5.1.4.1.1.104.2"


class FakeReportRetriever:
    """In-memory report retriever recording requests/refreshes (S06-T2)."""

    def __init__(self) -> None:
        self.requested: list[dict] = []
        self.refreshed: list[int] = []
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def request_report(
        self,
        study_uid: str,
        accession: str | None = None,
        report_type: str = "sr",
        *,
        retrieve_now: bool = False,
    ) -> int:
        self.requested.append(
            {
                "study_uid": study_uid,
                "accession": accession,
                "report_type": report_type,
                "retrieve_now": retrieve_now,
            }
        )
        return 1000 + len(self.requested)

    def retrieve(self, report_id: int) -> str:
        self.refreshed.append(report_id)
        return "retrieved"


def _write_sr(tmp_path, study_uid: str = "1.2.840.1") -> str:
    """Write a minimal SR DICOM file to disk and return its path."""
    from pydicom.dataset import Dataset as SrItem

    ds = Dataset()
    ds.SOPClassUID = _SR_SOP
    ds.SOPInstanceUID = "1.2.3.4.5.6.7.1"
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = "1.2.3.4.5.6.100"
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = _SR_SOP
    ds.file_meta.MediaStorageSOPInstanceUID = "1.2.3.4.5.6.7.1"
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.PatientName = "TEST^PATIENT"
    ds.PatientID = "P001"
    ds.StudyDescription = "Chest X-ray"
    ds.Modality = "SR"
    ds.ConversionType = "SYN"
    ds.VerificationFlag = "UNVERIFIED"

    code_ds = SrItem()
    code_ds.CodeValue = "121113"
    code_ds.CodingSchemeDesignator = "DCM"
    code_ds.CodeMeaning = "Report"
    item = SrItem()
    item.RelationshipType = "CONTAINS"
    item.ValueType = "TEXT"
    item.ConceptNameCodeSequence = [code_ds]
    item.TextValue = "Findings: Normal chest X-ray"
    root = SrItem()
    root.RelationshipType = "CONTAINS"
    root.ValueType = "CONTAINER"
    root.ConceptNameCodeSequence = [code_ds]
    root.ContinuityOfContent = "SEPARATE"
    root.ContentSequence = [item]
    ds.ContentSequence = [root]

    path = tmp_path / "report_sr.dcm"
    ds.save_as(str(path), write_like_original=False)
    return str(path)


def _write_pdf(tmp_path, study_uid: str = "1.2.840.2") -> str:
    """Write a minimal Encapsulated PDF DICOM file and return its path."""
    ds = Dataset()
    ds.SOPClassUID = _PDF_SOP
    ds.SOPInstanceUID = "1.2.3.4.5.6.7.2"
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = "1.2.3.4.5.6.101"
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = _PDF_SOP
    ds.file_meta.MediaStorageSOPInstanceUID = "1.2.3.4.5.6.7.2"
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.PatientName = "TEST^PATIENT"
    ds.PatientID = "P001"
    ds.StudyDescription = "Chest X-ray Report"
    ds.Modality = "DOC"
    ds.DocumentTitle = "Radiology Report"
    ds.EncapsulatedDocument = b"%PDF-1.4 fake pdf\n%%EOF\n"
    ds.MIMETypeOfEncapsulatedDocument = "application/pdf"

    path = tmp_path / "report_pdf.dcm"
    ds.save_as(str(path), write_like_original=False)
    return str(path)


@pytest.fixture()
def spool() -> Spool:
    return Spool(mem_database())


@pytest.fixture()
def retriever() -> FakeReportRetriever:
    return FakeReportRetriever()


@pytest.fixture()
def app(
    spool: Spool,
    retriever: FakeReportRetriever,
    fake_receiver: FakeReceiver,
    fake_forwarder: FakeForwarder,
    tmp_path,
):
    cfg = default_config()
    application = create_app(cfg, spool, config_path=tmp_path / "mercure-gateway.json")
    application.state.receiver = fake_receiver
    application.state.forwarder = fake_forwarder
    application.state.report_retriever = retriever
    return application


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app)


# ══════════════════════════════════════════════════════════════════════
# Config endpoints  (§7.1)
# ══════════════════════════════════════════════════════════════════════

def test_put_config_persists_update(client: TestClient, app, tmp_path) -> None:
    """PUT /config must save real changes, not just acknowledge them."""
    cfg = default_config()
    payload = cfg.model_dump(mode="json")
    payload["general"]["appliance_name"] = "Clinic-Beta"
    r = client.put("/api/config", json=payload)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    saved_path = tmp_path / "mercure-gateway.json"
    assert saved_path.exists(), "config was not written to disk"
    from mercure_gateway.config import load_config

    loaded = load_config(saved_path)
    assert loaded.general.appliance_name == "Clinic-Beta"


# ══════════════════════════════════════════════════════════════════════
# On-demand report request  (US-06, §7.3)
# ══════════════════════════════════════════════════════════════════════

def test_request_report_for_study(
    client: TestClient, spool: Spool, retriever: FakeReportRetriever
) -> None:
    study_id = spool.receive("1.2.840.1", accession="A100")
    r = client.post(f"/api/studies/{study_id}/reports?report_type=sr")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "pending"
    assert data["report_id"] > 0

    assert len(retriever.requested) == 1
    req = retriever.requested[0]
    assert req["study_uid"] == "1.2.840.1"
    assert req["accession"] == "A100"
    assert req["report_type"] == "sr"


def test_request_report_type_both(
    client: TestClient, spool: Spool, retriever: FakeReportRetriever
) -> None:
    study_id = spool.receive("1.2.840.2", accession="A101")
    r = client.post(f"/api/studies/{study_id}/reports?report_type=both")
    assert r.status_code == 200
    assert retriever.requested[0]["report_type"] == "both"


def test_request_report_unknown_study(client: TestClient) -> None:
    r = client.post("/api/studies/99999/reports")
    assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════
# Report refresh  (§7.3)
# ══════════════════════════════════════════════════════════════════════

def test_refresh_report_triggers_retriever(
    client: TestClient, spool: Spool, retriever: FakeReportRetriever
) -> None:
    study_id = spool.receive("1.2.840.1")
    report_id = spool._db.insert_report(study_id, "1.2.840.1", "sr")
    r = client.post(f"/api/reports/{report_id}/refresh")
    assert r.status_code == 200
    assert retriever.refreshed == [report_id]
    assert r.json()["status"] == "retrieved"


# ══════════════════════════════════════════════════════════════════════
# Report content  (§7.3)
# ══════════════════════════════════════════════════════════════════════

def test_report_content_sr_rendered_text(client: TestClient, spool: Spool, tmp_path) -> None:
    study_id = spool.receive("1.2.840.1")
    file_path = _write_sr(tmp_path, "1.2.840.1")
    report_id = spool._db.insert_report(
        study_id, "1.2.840.1", "sr", status="retrieved", file_path=file_path
    )
    r = client.get(f"/api/reports/{report_id}/content")
    assert r.status_code == 200
    data = r.json()
    assert data["report_type"] == "sr"
    assert "Normal chest X-ray" in data["content"]
    assert data["mime"] == "text/plain"


def test_report_content_pdf_bytes(client: TestClient, spool: Spool, tmp_path) -> None:
    study_id = spool.receive("1.2.840.2")
    file_path = _write_pdf(tmp_path, "1.2.840.2")
    report_id = spool._db.insert_report(
        study_id, "1.2.840.2", "pdf", status="retrieved", file_path=file_path
    )
    r = client.get(f"/api/reports/{report_id}/content")
    assert r.status_code == 200
    data = r.json()
    assert data["report_type"] == "pdf"
    assert data["mime"] == "application/pdf"
    import base64

    assert base64.b64decode(data["content"]).startswith(b"%PDF-")


def test_report_content_pending_returns_none(client: TestClient, spool: Spool) -> None:
    study_id = spool.receive("1.2.840.1")
    report_id = spool._db.insert_report(study_id, "1.2.840.1", "sr", status="pending")
    r = client.get(f"/api/reports/{report_id}/content")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "pending"
    assert data["content"] is None


# ══════════════════════════════════════════════════════════════════════
# OpenAPI  (§7 — auto-generated docs)
# ══════════════════════════════════════════════════════════════════════

def test_openapi_schema_valid(client: TestClient) -> None:
    r = client.get("/openapi.json")
    assert r.status_code == 200
    schema = r.json()
    assert schema["openapi"].startswith("3.")
    assert "paths" in schema
    assert "/api/studies/{study_id}/reports" in schema["paths"]
    assert "/api/reports/{report_id}/content" in schema["paths"]
