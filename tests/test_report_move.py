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


# ── Association budget (review P1-12) ───────────────────────────────────


def test_timeout_defaults_to_the_dimse_budget(tmp_path) -> None:
    """An unset timeout inherits the DIMSE convention rather than None.

    pynetdicom 3.0.4 has no ``timeout=`` kwarg on ``associate()``; the budget
    is applied to the AE inside ``retrieve()``.
    """
    from mercure_gateway.reports.move import (
        _DEFAULT_ASSOCIATE_TIMEOUT_SEC,
        ReportRetrieve,
    )

    retrieve = ReportRetrieve(
        host="127.0.0.1",
        port=11112,
        aet="PACS",
        store_scp_port=0,
        store_scp_ae_title="GATEWAY",
        reports_dir=tmp_path / "reports",
    )
    assert retrieve.timeout == _DEFAULT_ASSOCIATE_TIMEOUT_SEC


def test_configured_timeout_is_honoured(tmp_path) -> None:
    """A site with a slow PACS raises the C-MOVE budget from config."""
    from mercure_gateway.reports.move import ReportRetrieve

    retrieve = ReportRetrieve(
        host="127.0.0.1",
        port=11112,
        aet="PACS",
        store_scp_port=0,
        store_scp_ae_title="GATEWAY",
        reports_dir=tmp_path / "reports",
        timeout=90.0,
    )
    assert retrieve.timeout == 90.0


# ── Path-traversal guard (review P0-1) ──────────────────────────────────


@pytest.mark.parametrize(
    ("study_uid", "sop_uid"),
    [
        # A PACS that answers a C-FIND with these would otherwise write the
        # report outside reports_dir (P0-1, CVSS 9.8).
        ("../../etc", "1.2.3"),
        ("1.2.3", "../../evil"),
        ("1.2.3", ".."),
        ("", "1.2.3"),
        ("1.2.3", ""),
    ],
)
def test_save_rejects_uids_that_escape_reports_dir(
    tmp_path: Path, study_uid: str, sop_uid: str
) -> None:
    """The C-MOVE save path refuses UIDs that could traverse out of the sandbox."""
    from mercure_gateway.reports.move import ReportRetrieve
    from mercure_gateway.spool import InvalidUIDError

    ds = _make_instance("1.2.3", SR_SOP_CLASS, "1.2.3.4")
    retrieve = ReportRetrieve(
        host="127.0.0.1",
        port=free_port(),
        aet="PACS",
        store_scp_port=free_port(),
        store_scp_ae_title="GATEWAY",
        reports_dir=tmp_path / "reports",
    )

    with pytest.raises(InvalidUIDError):
        retrieve._save(ds, study_uid, SR_SOP_CLASS, sop_uid)

    # Nothing was written anywhere under the sandbox root.
    assert not (tmp_path / "reports").exists() or not list((tmp_path / "reports").rglob("*.dcm"))


def test_save_rejects_overlong_uid(tmp_path: Path) -> None:
    """A UID longer than the 64-char DICOM limit is rejected, not truncated."""
    from mercure_gateway.reports.move import ReportRetrieve
    from mercure_gateway.spool import InvalidUIDError

    long_uid = "1." * 40  # 79 chars
    ds = _make_instance("1.2.3", SR_SOP_CLASS, "1.2.3.4")
    retrieve = ReportRetrieve(
        host="127.0.0.1",
        port=free_port(),
        aet="PACS",
        store_scp_port=free_port(),
        store_scp_ae_title="GATEWAY",
        reports_dir=tmp_path / "reports",
    )

    with pytest.raises(InvalidUIDError):
        retrieve._save(ds, long_uid, SR_SOP_CLASS, "1.2.3.4")


# ── Store SCP lifetime (review: server leak when associate raises) ─────


@pytest.fixture()
def store_scp_recorder(monkeypatch: pytest.MonkeyPatch) -> tuple[list[object], list[object]]:
    """Record every C-STORE SCP ``retrieve()`` starts and every ``shutdown()``.

    Patches the ``AE`` class methods the module bound at import time, so the
    server objects the code under test creates can be inspected after the fact.
    """
    from mercure_gateway.reports import move as move_module

    servers: list[object] = []
    shutdowns: list[object] = []
    real_start_server = move_module.AE.start_server

    def record_server(self: object, *args: object, **kwargs: object) -> object:
        server = real_start_server(self, *args, **kwargs)
        real_shutdown = server.shutdown  # type: ignore[attr-defined]

        def recorded_shutdown(*a: object, **kw: object) -> object:
            shutdowns.append(server)
            return real_shutdown(*a, **kw)

        server.shutdown = recorded_shutdown  # type: ignore[attr-defined]
        servers.append(server)
        return server

    monkeypatch.setattr(move_module.AE, "start_server", record_server)
    return servers, shutdowns


def _make_retrieve(tmp_path: Path, pacs_port: int, store_port: int) -> object:
    from mercure_gateway.reports.move import ReportRetrieve

    return ReportRetrieve(
        host="127.0.0.1",
        port=pacs_port,
        aet="PACS",
        store_scp_port=store_port,
        store_scp_ae_title="GATEWAY",
        reports_dir=tmp_path / "reports",
    )


def _a_sr_match() -> ReportMatch:
    return ReportMatch(
        sop_class_uid=SR_SOP_CLASS,
        study_uid="1.2.840.99",
        series_uid="1.2.3.4.5.6.100",
        sop_instance_uid="1.2.3.4.5.6.7.1",
    )


def test_associate_raising_still_shuts_down_the_store_scp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_scp_recorder: tuple[list[object], list[object]],
) -> None:
    """An exception out of ``associate()`` must not leak the store SCP.

    ``associate()`` has documented raise paths (bad address, TLS failure). The
    cleanup ``finally`` used to begin *after* it, so the SCP thread and its
    bound port survived — and with the port bound, the next retrieve could not
    start a store SCP at all.
    """
    from mercure_gateway.reports import move as move_module

    servers, shutdowns = store_scp_recorder

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("TLS handshake failed")

    monkeypatch.setattr(move_module.AE, "associate", boom)

    retrieve = _make_retrieve(tmp_path, pacs_port=free_port(), store_port=free_port())
    with pytest.raises(RuntimeError, match="TLS handshake failed"):
        retrieve.retrieve([_a_sr_match()])

    assert len(servers) == 1
    assert shutdowns == servers, "the store SCP must be shut down on the raising path"


def test_rejected_association_still_shuts_down_the_store_scp(
    tmp_path: Path, store_scp_recorder: tuple[list[object], list[object]]
) -> None:
    """A rejected association must shut the store SCP down too (regression)."""
    servers, shutdowns = store_scp_recorder

    retrieve = _make_retrieve(tmp_path, pacs_port=free_port(), store_port=free_port())
    from mercure_gateway.reports.move import ReportRetrieveError

    with pytest.raises(ReportRetrieveError):
        retrieve.retrieve([_a_sr_match()])

    assert len(servers) == 1
    assert shutdowns == servers


def test_store_scp_port_is_reusable_after_a_failed_retrieve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_scp_recorder: tuple[list[object], list[object]],
) -> None:
    """The bound port is released, so a second retrieve can start its own SCP.

    This is the user-visible symptom of the leak: the first retrieve fails, and
    every retrieve after it fails to even bind the store SCP.
    """
    from mercure_gateway.reports import move as move_module

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("TLS handshake failed")

    monkeypatch.setattr(move_module.AE, "associate", boom)

    store_port = free_port()
    retrieve = _make_retrieve(tmp_path, pacs_port=free_port(), store_port=store_port)

    with pytest.raises(RuntimeError):
        retrieve.retrieve([_a_sr_match()])

    # If the first server was never shut down, this bind fails with
    # EADDRINUSE. Use SO_REUSEADDR off so a lingering listener is detected.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        probe.bind(("127.0.0.1", store_port))
    finally:
        probe.close()
