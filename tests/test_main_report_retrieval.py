"""P0-6 / US-06 (review): a requested report reaches RETRIEVED end-to-end.

This is the test whose absence let the broken wiring ship: ``main.py``
constructed ``ReportRetriever`` with no ``finder``/``mover``, so every poll
hit ``RuntimeError("report transports (finder/mover) not configured")`` and
the report went to FAILED — while the feature was documented as working.

The composition root is now ``_build_report_retriever``; these tests drive it
against in-process pynetdicom SCPs so the whole C-FIND → C-MOVE → C-STORE
roundtrip is real, not faked. If the wiring regresses, the report stays
FAILED and the assertion names the missing transport.
"""

from __future__ import annotations

import socket
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian
from pynetdicom import AE, evt

from mercure_gateway.audit import AuditLog
from mercure_gateway.config import (
    GatewayConfig,
    ReceiverConfig,
    ReportConfig,
    ReportQuerySource,
    StorageConfig,
    default_config,
)
from mercure_gateway.main import _build_report_retriever
from mercure_gateway.reports import ReportStatus
from mercure_gateway.reports.find import SR_SOP_CLASS
from mercure_gateway.spool import Spool
from mercure_gateway.spool.db import mem_database

_IMAGE_LEVEL_FIND_SOP = "1.2.840.10008.5.1.4.1.2.2.1"
_STUDY_ROOT_MOVE = "1.2.840.10008.5.1.4.1.2.2.2"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _sr_instance(study_uid: str, sop_uid: str) -> Dataset:
    ds = Dataset()
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = f"{study_uid}.1"
    ds.SOPInstanceUID = sop_uid
    ds.SOPClassUID = SR_SOP_CLASS
    ds.PatientName = "TEST^PATIENT"
    ds.PatientID = "P001"
    ds.AccessionNumber = "ACC-001"
    ds.Modality = "SR"
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = SR_SOP_CLASS
    ds.file_meta.MediaStorageSOPInstanceUID = sop_uid
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    return ds


class _FakePACS:
    """In-process SCP answering C-FIND and C-MOVE for one SR instance.

    The C-MOVE handler follows pynetdicom's generator contract: yield the
    destination (addr, port) the *PACS* should open its C-STORE association
    to — which is the gateway's own store SCP — then the sub-operation count,
    then the instances themselves.
    """

    def __init__(self, port: int, store_scp_port: int, instance: Dataset) -> None:
        self._port = port
        self._store_scp_port = store_scp_port
        self._instance = instance
        self.ae = AE(ae_title="FAKEPACS")
        self.ae.add_supported_context(_IMAGE_LEVEL_FIND_SOP, ExplicitVRLittleEndian)
        self.ae.add_supported_context(_STUDY_ROOT_MOVE, ExplicitVRLittleEndian)
        # The C-MOVE handler opens a *sub-association* back to the gateway's
        # store SCP; without a requested context for the SR class pynetdicom
        # rejects the association attempt with status C515.
        self.ae.add_requested_context(SR_SOP_CLASS, ExplicitVRLittleEndian)

    def start(self) -> None:
        self.ae.start_server(
            ("127.0.0.1", self._port),
            evt_handlers=[
                (evt.EVT_C_FIND, self._on_c_find),
                (evt.EVT_C_MOVE, self._on_c_move),
            ],
            block=False,
        )
        # start_server returns before the socket is necessarily accepting.
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", self._port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(f"fake PACS did not bind port {self._port}")

    def shutdown(self) -> None:
        self.ae.shutdown()

    def _on_c_find(self, event: evt.Event) -> Any:
        yield (0xFF00, self._instance)
        yield (0x0000, None)

    def _on_c_move(self, event: evt.Event) -> Any:
        # Where the PACS pushes the instances: the gateway's C-STORE SCP.
        yield ("127.0.0.1", self._store_scp_port)
        yield 1
        # 0xFF00 (Pending) is the status that makes pynetdicom actually run
        # the C-STORE sub-operation for the dataset; yielding 0x0000 (Success)
        # ends the move with zero instances delivered.
        yield (0xFF00, self._instance)


def _gateway_config(tmp_path: Path, pacs_port: int, store_scp_port: int) -> GatewayConfig:
    cfg = default_config()
    cfg.receiver = ReceiverConfig(ae_title="GATEWAY")
    cfg.storage = StorageConfig(spool_dir=str(tmp_path / "spool"))
    cfg.reports = ReportConfig(
        enabled=True,
        query_source=ReportQuerySource(
            type="dicom", host="127.0.0.1", port=pacs_port, aet="FAKEPACS"
        ),
        store_scp_port=store_scp_port,
    )
    return cfg


def _retriever(cfg: GatewayConfig, tmp_path: Path) -> Any:
    db = mem_database()
    spool = Spool(db, cfg, spool_dir=tmp_path / "spool")
    audit = AuditLog(db)
    return _build_report_retriever(cfg, db, spool, audit), db, spool


@pytest.mark.skipif(
    __import__("sys").platform == "win32",
    reason="in-process pynetdicom SCP timing is flaky under the Windows CI runner",
)
def test_report_reaches_retrieved_through_real_transports(tmp_path: Path) -> None:
    """PENDING → RETRIEVING → RETRIEVED via the composition-root wiring."""
    pacs_port = free_port()
    store_scp_port = free_port()
    study_uid = "1.2.840.10008.99.42"
    instance = _sr_instance(study_uid, f"{study_uid}.1.1")

    pacs = _FakePACS(pacs_port, store_scp_port, instance)
    pacs.start()
    try:
        cfg = _gateway_config(tmp_path, pacs_port, store_scp_port)
        retriever, db, spool = _retriever(cfg, tmp_path)
        assert retriever.finder is not None, "composition root left finder unwired"
        assert retriever.mover is not None, "composition root left mover unwired"

        spool.receive(study_uid, accession="ACC-001", modality="CT")
        report_id = retriever.request_report(study_uid, "ACC-001", "sr")
        assert retriever.retrieve(report_id) == ReportStatus.RETRIEVED

        row = db.get_report(report_id)
        assert row is not None
        assert row["status"] == ReportStatus.RETRIEVED
    finally:
        pacs.shutdown()

    # The instance the PACS pushed must be on disk under the spool.
    saved = list((tmp_path / "spool" / "reports").rglob("*.dcm"))
    assert len(saved) == 1, f"expected 1 retrieved report file, found {len(saved)}"


@pytest.mark.skipif(
    __import__("sys").platform == "win32",
    reason="in-process pynetdicom SCP timing is flaky under the Windows CI runner",
)
def test_report_fails_loudly_when_pacs_unreachable(tmp_path: Path) -> None:
    """Unreachable PACS → FAILED, not a swallowed exception (regression guard)."""
    # Nothing listening here.
    cfg = _gateway_config(tmp_path, free_port(), free_port())
    retriever, db, spool = _retriever(cfg, tmp_path)

    study_uid = "1.2.840.10008.99.43"
    spool.receive(study_uid, accession="ACC-002", modality="CT")
    report_id = retriever.request_report(study_uid, "ACC-002", "sr")

    assert retriever.retrieve(report_id) == ReportStatus.FAILED
    row = db.get_report(report_id)
    assert row is not None
    assert row["status"] == ReportStatus.FAILED


def test_disabled_reports_leaves_transports_unwired(tmp_path: Path) -> None:
    """Reports off ⇒ no query_source ⇒ no transport, and no crash."""
    cfg = _gateway_config(tmp_path, free_port(), free_port())
    cfg.reports.enabled = False
    cfg.reports.query_source = None

    retriever, _db, _spool = _retriever(cfg, tmp_path)
    assert retriever.finder is None
    assert retriever.mover is None


# ══════════════════════════════════════════════════════════════════════
# DICOMweb query sources: build_dicomweb_transport() existed and was tested,
# but no factory registered it — a valid dicomweb source fell through to the
# registry's generic cls(source) fallback, which raised TypeError (the
# constructor needs base_url), and the except in _build_report_retriever
# swallowed it to a log line. Reports looked configured and retrieved nothing.
# ══════════════════════════════════════════════════════════════════════


def _dicomweb_config(
    tmp_path: Path, *, path: str = "dicomweb", port: int = 8443
) -> GatewayConfig:
    cfg = default_config()
    cfg.storage = StorageConfig(spool_dir=str(tmp_path / "spool"))
    cfg.reports = ReportConfig(
        enabled=True,
        query_source=ReportQuerySource(
            type="dicomweb", host="pacs.local", port=port, aet="GATEWAY", path=path
        ),
    )
    return cfg


@patch("requests.get")
def test_dicomweb_query_source_wires_a_working_transport(
    mock_get: MagicMock, tmp_path: Path
) -> None:
    """A dicomweb source builds its transport through the composition root.

    Before the factory was registered this left finder/mover None and every
    report FAILED — the feature's own test suite passed because it built the
    transport directly, bypassing the composition root entirely.
    """
    resp = MagicMock()
    resp.ok = True
    resp.json.return_value = []  # no report instances on the server
    mock_get.return_value = resp

    retriever, _db, _spool = _retriever(_dicomweb_config(tmp_path), tmp_path)
    assert retriever.finder is not None, "dicomweb source left finder unwired"
    assert retriever.mover is not None, "dicomweb source left mover unwired"

    # The wired transport actually queries the configured service root — this
    # is the call that used to never happen.
    assert retriever.finder(study_uid="1.2.3") == []
    url = mock_get.call_args[0][0]
    assert url.startswith("https://pacs.local:8443/dicomweb/studies")


@patch("requests.get")
def test_dicomweb_service_root_is_configurable(mock_get: MagicMock, tmp_path: Path) -> None:
    """The QIDO/WADO root is not hardcoded: dcm4chee and cloud stores differ.

    A hardcoded /dicomweb made the transport unreachable for any server not
    laid out like the one it was written against, failing with a 404 the
    operator could not map to a setting that does not exist.
    """
    resp = MagicMock()
    resp.ok = True
    resp.json.return_value = []
    mock_get.return_value = resp

    cfg = _dicomweb_config(
        tmp_path, path="dcm4chee-arc/aets/DCM4CHEE/rs/", port=8443
    )
    retriever, _db, _spool = _retriever(cfg, tmp_path)
    assert retriever.finder is not None
    retriever.finder(study_uid="1.2.3")

    url = mock_get.call_args[0][0]
    # Leading/trailing slashes are tolerated in either form.
    assert url.startswith("https://pacs.local:8443/dcm4chee-arc/aets/DCM4CHEE/rs/studies")


def test_dicomweb_query_source_over_https_verifies_tls(tmp_path: Path) -> None:
    """Report content over plaintext HTTP is not offered by default."""
    from mercure_gateway.reports.dicomweb import build_dicomweb_transport

    transport = build_dicomweb_transport(
        _dicomweb_config(tmp_path).reports.query_source,  # type: ignore[arg-type]
        reports_dir=tmp_path / "reports",
    )
    assert transport._base_url.startswith("https://")
    assert transport._verify_tls is True


@pytest.mark.parametrize("experimental_type", ["fhir", "hl7"])
def test_experimental_transports_are_refused_not_silently_broken(
    experimental_type: str, tmp_path: Path
) -> None:
    """HL7/FHIR builds cleanly and then raises on every poll.

    So the registry's dispatch would log "report retrieval wired" and fail
    each report at runtime with NotImplementedError. The flag lives with the
    transport; while it is off, boot must refuse the source outright.
    """
    cfg = default_config()
    cfg.storage = StorageConfig(spool_dir=str(tmp_path / "spool"))
    cfg.reports = ReportConfig(
        enabled=True,
        query_source=ReportQuerySource(
            type=experimental_type,  # type: ignore[arg-type]
            host="pacs.local",
            port=8443,
            aet="GATEWAY",
        ),
    )

    retriever, _db, _spool = _retriever(cfg, tmp_path)
    assert retriever.finder is None, f"{experimental_type} source was wired anyway"
    assert retriever.mover is None
