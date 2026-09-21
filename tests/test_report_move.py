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
5. One STUDY-level C-MOVE is issued per distinct study, not one per match
6. The instance is persisted inside the C-STORE handler, before the ack
7. The saved path and file_meta come from the dataset, not the C-FIND match
8. A store that cannot be persisted is acked 0xC120 and surfaces as an error
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian
from pynetdicom import AE, evt

from mercure_gateway.reports.find import PDF_SOP_CLASS, SR_SOP_CLASS, ReportMatch
from mercure_gateway.reports.move import _STUDY_ROOT_MOVE


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


def _make_wire_instance(study_uid: str, sop_class: str, sop_uid: str) -> Dataset:
    """An instance as it arrives over DIMSE — no ``file_meta``.

    Group 0002 is file-format only and is never transmitted, which is why the
    in-memory ``_make_instance`` (with its hand-built file_meta) left the
    rebuild path unexercised.
    """
    ds = _make_instance(study_uid, sop_class, sop_uid)
    del ds.file_meta
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
    saved = retrieve._save(sr_instance)

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
    saved = retrieve._save(pdf_instance)

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
    saved = retrieve._save(ds)

    assert saved.exists()
    assert deep.exists()


def test_save_rebuilds_file_meta_for_a_wire_dataset(tmp_path: Path) -> None:
    """A dataset off the wire has no group 0002; it is rebuilt before save_as.

    ``save_as(enforce_file_format=True)`` would otherwise raise "Required File
    Meta Information elements are either missing" before anything hit disk.
    """
    from mercure_gateway.reports.move import ReportRetrieve

    study_uid, sop_uid = "1.2.840.3", "1.2.840.3.1"
    retrieve = ReportRetrieve(
        host="127.0.0.1",
        port=free_port(),
        aet="PACS",
        store_scp_port=free_port(),
        store_scp_ae_title="GATEWAY",
        reports_dir=tmp_path / "reports",
    )

    saved = retrieve._save(_make_wire_instance(study_uid, SR_SOP_CLASS, sop_uid))

    from pydicom import dcmread

    reloaded = dcmread(str(saved))
    assert str(reloaded.StudyInstanceUID) == study_uid
    assert str(reloaded.file_meta.MediaStorageSOPInstanceUID) == sop_uid
    assert str(reloaded.file_meta.MediaStorageSOPClassUID) == SR_SOP_CLASS


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
        # A PACS that pushes a dataset carrying these would otherwise write the
        # report outside reports_dir (P0-1, CVSS 9.8). The UIDs are read from
        # the dataset now, so that is where the payload arrives.
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

    ds = _make_wire_instance(study_uid, SR_SOP_CLASS, sop_uid)
    retrieve = ReportRetrieve(
        host="127.0.0.1",
        port=free_port(),
        aet="PACS",
        store_scp_port=free_port(),
        store_scp_ae_title="GATEWAY",
        reports_dir=tmp_path / "reports",
    )

    with pytest.raises(InvalidUIDError):
        retrieve._save(ds)

    # Nothing was written anywhere under the sandbox root.
    assert not (tmp_path / "reports").exists() or not list((tmp_path / "reports").rglob("*.dcm"))


def test_save_rejects_overlong_uid(tmp_path: Path) -> None:
    """A UID longer than the 64-char DICOM limit is rejected, not truncated."""
    from mercure_gateway.reports.move import ReportRetrieve
    from mercure_gateway.spool import InvalidUIDError

    long_uid = "1." * 40  # 79 chars
    ds = _make_wire_instance(long_uid, SR_SOP_CLASS, "1.2.3.4")
    retrieve = ReportRetrieve(
        host="127.0.0.1",
        port=free_port(),
        aet="PACS",
        store_scp_port=free_port(),
        store_scp_ae_title="GATEWAY",
        reports_dir=tmp_path / "reports",
    )

    with pytest.raises(InvalidUIDError):
        retrieve._save(ds)


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


# ── Store-before-ack, dedup, and dataset-derived UIDs ───────────────────
# These exercise the real C-MOVE roundtrip against an in-process SCP, the
# pattern in tests/test_main_report_retrieval.py's _FakePACS (which this
# file may not modify).


class _StubEvent:
    """Minimal stand-in for ``evt.Event`` — ``_on_c_store`` only reads .dataset."""

    def __init__(self, dataset: Dataset) -> None:
        self.dataset = dataset


class _FakePACS:
    """In-process SCP that answers a study-level C-MOVE by pushing instances.

    The C-MOVE generator yields the destination the *PACS* opens its C-STORE
    sub-association to — the gateway's own store SCP — then the sub-operation
    count, then the instances. pynetdicom runs the C-STORE sub-operation when
    the generator resumes after a ``(0xFF00, dataset)`` yield, so ``probe``
    (called on the next line) observes the filesystem *while* the gateway's
    store SCP is still listening — which is what distinguishes store-in-handler
    from the old buffer-then-flush flow.
    """

    def __init__(
        self,
        port: int,
        store_scp_port: int,
        instances: list[Dataset],
        probe: Callable[[], Any] | None = None,
    ) -> None:
        self._port = port
        self._store_scp_port = store_scp_port
        self._instances = list(instances)
        self._probe = probe
        # The C-MOVE query datasets the gateway sent, one per move.
        self.move_queries: list[dict[str, str]] = []
        # One probe result per pushed instance, taken mid-association.
        self.probe_results: list[Any] = []
        self.ae = AE(ae_title="FAKEPACS")
        self.ae.add_supported_context(_STUDY_ROOT_MOVE, ExplicitVRLittleEndian)
        # The sub-association back to the gateway's store SCP; without a
        # requested context for the report class pynetdicom rejects the
        # association attempt with status C515.
        self.ae.add_requested_context(SR_SOP_CLASS, ExplicitVRLittleEndian)
        self.ae.add_requested_context(PDF_SOP_CLASS, ExplicitVRLittleEndian)

    def start(self) -> None:
        self.ae.start_server(
            ("127.0.0.1", self._port),
            evt_handlers=[(evt.EVT_C_MOVE, self._on_c_move)],
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

    def _on_c_move(self, event: evt.Event) -> Any:
        identifier = event.identifier
        self.move_queries.append(
            {
                "QueryRetrieveLevel": str(getattr(identifier, "QueryRetrieveLevel", "")),
                "StudyInstanceUID": str(getattr(identifier, "StudyInstanceUID", "")),
            }
        )
        yield ("127.0.0.1", self._store_scp_port)
        yield len(self._instances)
        for instance in self._instances:
            # 0xFF00 (Pending) is the status that makes pynetdicom actually run
            # the C-STORE sub-operation; yielding 0x0000 (Success) would end the
            # move with zero instances delivered.
            yield (0xFF00, instance)
            # Control returns here once that C-STORE has been acked by the
            # gateway's store SCP — the association is still very much open.
            if self._probe is not None:
                self.probe_results.append(self._probe())
        yield (0x0000, None)


def _match(study_uid: str, sop_uid: str, sop_class: str = SR_SOP_CLASS) -> ReportMatch:
    return ReportMatch(
        sop_class_uid=sop_class,
        study_uid=study_uid,
        series_uid=f"{study_uid}.1",
        sop_instance_uid=sop_uid,
    )


def test_one_c_move_per_study_for_n_matches_in_the_same_study(tmp_path: Path) -> None:
    """A study with N matched instances gets ONE study-level C-MOVE, not N.

    The C-FIND answer is one row per instance, but the query carries only the
    study UID — so the old per-match loop issued N byte-identical full-study
    transfers.
    """
    store_port, pacs_port = free_port(), free_port()
    study_uid = "1.2.840.6"
    sop_uids = [f"{study_uid}.{i}" for i in range(1, 4)]
    pacs = _FakePACS(
        pacs_port, store_port, [_make_instance(study_uid, SR_SOP_CLASS, u) for u in sop_uids]
    )
    pacs.start()
    try:
        retrieve = _make_retrieve(tmp_path, pacs_port, store_port)
        saved = retrieve.retrieve([_match(study_uid, u) for u in sop_uids])
    finally:
        pacs.shutdown()

    assert len(pacs.move_queries) == 1, "one C-MOVE per study, not one per match"
    assert pacs.move_queries[0]["QueryRetrieveLevel"] == "STUDY"
    assert pacs.move_queries[0]["StudyInstanceUID"] == study_uid
    # Nothing was lost: each pushed instance was persisted.
    assert {r.sop_instance_uid for r in saved} == set(sop_uids)
    assert all(r.file_path.exists() for r in saved)


def test_one_c_move_per_distinct_study(tmp_path: Path) -> None:
    """The dedup does not over-collapse two different studies."""
    store_port, pacs_port = free_port(), free_port()
    study_a, study_b = "1.2.840.61", "1.2.840.62"
    pacs = _FakePACS(
        pacs_port,
        store_port,
        [
            _make_instance(study_a, SR_SOP_CLASS, f"{study_a}.1"),
            _make_instance(study_b, PDF_SOP_CLASS, f"{study_b}.1"),
        ],
    )
    pacs.start()
    try:
        retrieve = _make_retrieve(tmp_path, pacs_port, store_port)
        saved = retrieve.retrieve(
            [_match(study_a, f"{study_a}.1"), _match(study_b, f"{study_b}.1")]
        )
    finally:
        pacs.shutdown()

    assert len(pacs.move_queries) == 2
    assert {q["StudyInstanceUID"] for q in pacs.move_queries} == {study_a, study_b}
    assert {r.report_type for r in saved} == {"sr", "pdf"}


def test_the_instance_is_saved_while_the_association_is_still_open(tmp_path: Path) -> None:
    """The file is on disk before the C-STORE handler returns — not after release.

    The probe runs inside the fake PACS's C-MOVE handler, between the C-STORE
    sub-operation completing and retrieve() shutting the store SCP down. With
    the old buffer-then-flush flow it would observe no file.
    """
    store_port, pacs_port = free_port(), free_port()
    study_uid, sop_uid = "1.2.840.5", "1.2.840.5.1"
    expected = tmp_path / "reports" / study_uid / "sr" / f"{sop_uid}.dcm"
    pacs = _FakePACS(
        pacs_port,
        store_port,
        [_make_instance(study_uid, SR_SOP_CLASS, sop_uid)],
        probe=expected.exists,
    )
    pacs.start()
    try:
        retrieve = _make_retrieve(tmp_path, pacs_port, store_port)
        saved = retrieve.retrieve([_match(study_uid, sop_uid)])
    finally:
        pacs.shutdown()

    assert pacs.probe_results == [True], "the file must exist before the ack returns"
    assert len(saved) == 1
    assert saved[0].file_path == expected


def test_an_instance_is_saved_under_its_own_uid_not_the_match(tmp_path: Path) -> None:
    """A pushed instance is keyed by its dataset UIDs, not the C-FIND match's."""
    store_port, pacs_port = free_port(), free_port()
    study_uid = "1.2.840.7"
    match_uid, pushed_uid = f"{study_uid}.1", f"{study_uid}.2"
    pacs = _FakePACS(
        pacs_port, store_port, [_make_instance(study_uid, SR_SOP_CLASS, pushed_uid)]
    )
    pacs.start()
    try:
        retrieve = _make_retrieve(tmp_path, pacs_port, store_port)
        saved = retrieve.retrieve([_match(study_uid, match_uid)])
    finally:
        pacs.shutdown()

    assert len(saved) == 1
    assert saved[0].sop_instance_uid == pushed_uid
    assert saved[0].file_path.name == f"{pushed_uid}.dcm"
    # The match's UID was never used as a filename.
    assert not (tmp_path / "reports" / study_uid / "sr" / f"{match_uid}.dcm").exists()


def test_on_c_store_acks_success_and_records_the_saved_report(tmp_path: Path) -> None:
    """The handler persists, records the report, and acks 0x0000."""
    retrieve = _make_retrieve(tmp_path, free_port(), free_port())
    study_uid, sop_uid = "1.2.840.8", "1.2.840.8.1"
    ds = _make_wire_instance(study_uid, SR_SOP_CLASS, sop_uid)

    assert retrieve._on_c_store(_StubEvent(ds)) == 0x0000  # type: ignore[arg-type]
    assert retrieve._failures == []
    assert len(retrieve._saved) == 1
    assert retrieve._saved[0].sop_instance_uid == sop_uid
    assert retrieve._saved[0].file_path.exists()


def test_on_c_store_acks_processing_failure_when_persistence_fails(tmp_path: Path) -> None:
    """A bad UID is rejected after the store arrived, so the ack is 0xC120.

    Acking success would leave the report row RETRIEVED pointing at a file
    that does not exist (P0-1 path, now hit by the failure it used to miss).
    """
    retrieve = _make_retrieve(tmp_path, free_port(), free_port())
    ds = _make_wire_instance("../../etc", SR_SOP_CLASS, "1.2.3.4")

    assert retrieve._on_c_store(_StubEvent(ds)) == 0xC120  # type: ignore[arg-type]
    assert retrieve._saved == []
    assert retrieve._failures == ["1.2.3.4"]
    assert not list((tmp_path / "reports").rglob("*.dcm"))


def test_retrieve_raises_when_a_pushed_instance_cannot_be_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 0xC120 store reaches the caller as an error, never a phantom success.

    Simulates a persist failure (disk full, permission denied) at the point
    the old code had no handling for: the instance arrived intact, the store
    failed anyway, and retrieve() used to report success on a missing file.
    """
    from pydicom.dataset import Dataset

    from mercure_gateway.reports.move import ReportRetrieveError

    def disk_full(*args: object, **kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Dataset, "save_as", disk_full)

    store_port, pacs_port = free_port(), free_port()
    study_uid, sop_uid = "1.2.840.9", "1.2.840.9.1"
    pacs = _FakePACS(
        pacs_port, store_port, [_make_instance(study_uid, SR_SOP_CLASS, sop_uid)]
    )
    pacs.start()
    try:
        retrieve = _make_retrieve(tmp_path, pacs_port, store_port)
        with pytest.raises(ReportRetrieveError):
            retrieve.retrieve([_match(study_uid, sop_uid)])
    finally:
        pacs.shutdown()

    assert not list((tmp_path / "reports").rglob("*.dcm"))


def test_on_c_store_ignores_a_non_report_instance(tmp_path: Path) -> None:
    """A class the store SCP does not serve is skipped, not filed under ``pdf/``."""
    retrieve = _make_retrieve(tmp_path, free_port(), free_port())
    ct = "1.2.840.10008.5.1.4.1.1.2"  # CT Image Storage
    ds = _make_wire_instance("1.2.840.10", ct, "1.2.840.10.1")

    assert retrieve._on_c_store(_StubEvent(ds)) == 0x0000  # type: ignore[arg-type]
    assert retrieve._saved == []
    assert retrieve._failures == []
