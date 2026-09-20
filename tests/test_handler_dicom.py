"""TDD: unit tests for the DICOM C-STORE SCU handler (dicom target type).

Behaviors covered (in RED-GREEN order):
1.  A study with DICOM files is delivered via C-STORE to a live SCP → SENT
2.  Destination unreachable (connection refused) → handler reports failure
3.  Study has no DICOM files on disk → graceful failure
4.  SCP rejects the association (bad AE title) → failure
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import (
    AllTransferSyntaxes,
    CTImageStorage,
    ExplicitVRLittleEndian,
    JPEGBaseline8Bit,
    JPEGLossless,
    RLELossless,
    generate_uid,
)
from pynetdicom import AE, evt
from pynetdicom.sop_class import CTImageStorage as CTContext

from mercure_gateway.config import DICOMDestination, default_config
from mercure_gateway.forwarder import Forwarder, RetryPolicy
from mercure_gateway.forwarder.handlers.dicom import (
    _DEFAULT_ASSOCIATE_TIMEOUT_SEC,
    DICOMHandler,
)
from mercure_gateway.spool import Spool, StudyState
from mercure_gateway.spool.db import mem_database


def make_dicom_file(path: Path, study_uid: str, series_uid: str, instance_uid: str) -> None:
    """Write a minimal, valid CT DICOM file to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CTImageStorage
    meta.MediaStorageSOPInstanceUID = instance_uid
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(path, {}, file_meta=meta, preamble=b"\x00" * 128)
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.SOPInstanceUID = instance_uid
    ds.SOPClassUID = CTImageStorage
    ds.PatientName = "TEST^PATIENT"
    ds.Modality = "CT"
    ds.save_as(path, enforce_file_format=True)


@pytest.fixture()
def dicom_scp() -> Iterator[tuple[list[object], int]]:
    """Run a real C-STORE SCP on an ephemeral port; yields (received, port).

    Accepts CT with ALL transfer syntaxes (like modern PACS — and like the
    gateway's own receiver), so compressed studies can be delivered in tests.
    """
    received: list[object] = []

    def handle_store(event: object) -> int:
        received.append(event)
        return 0x0000

    scp = AE(ae_title="TESTSCP")
    scp.add_supported_context(CTContext, AllTransferSyntaxes)
    # pydicom's AllTransferSyntaxes omits RLE Lossless — accept it explicitly
    # so compressed-as-stored studies can be delivered (review F7).
    scp.add_supported_context(CTContext, RLELossless)
    scp.add_supported_context(CTContext, JPEGBaseline8Bit)
    scp.add_supported_context(CTContext, JPEGLossless)
    server = scp.start_server(
        ("127.0.0.1", 0),
        evt_handlers=[(evt.EVT_C_STORE, handle_store)],
        block=False,
    )
    port = server.server_address[1]
    yield received, port
    server.shutdown()


def make_spool(tmp_path: Path) -> Spool:
    cfg = default_config()
    cfg.storage.spool_dir = str(tmp_path / "spool")
    spool = Spool(mem_database(), cfg)
    return spool


def make_forwarder(spool: Spool, target: DICOMDestination, max_attempts: int = 1) -> Forwarder:
    """Build a Forwarder wired with a DICOMHandler for ``target``."""
    cfg = default_config()
    fwd = Forwarder(cfg, spool, retry=RetryPolicy(base_delay_sec=0, max_attempts=max_attempts))
    fwd.register_handler("dicom", DICOMHandler(target, spool))
    return fwd


# ── Slice 1 (tracer bullet): deliver a study to a live SCP ─────────────


def test_dicom_delivery_to_live_scp(tmp_path: Path, dicom_scp: tuple[list[object], int]) -> None:
    received, port = dicom_scp
    spool = make_spool(tmp_path)
    study_uid = "1.2.3.4.5.6"
    series_uid = "1.2.3.4.5.6.1"
    make_dicom_file(
        tmp_path / "spool" / study_uid / series_uid / "1.dcm",
        study_uid,
        series_uid,
        generate_uid(),
    )
    study_id = spool.receive(study_uid)
    target = DICOMDestination(
        name="pacs", type="dicom", host="127.0.0.1", port=port, aet_target="TESTSCP"
    )
    spool.enqueue(study_id, [target])

    fwd = make_forwarder(spool, target, max_attempts=2)
    count = fwd.process_once()

    assert count == 1
    assert spool.state(study_id) == StudyState.SENT
    assert len(received) == 1


# ── Slice 2: unreachable destination reports failure ───────────────────


def test_unreachable_destination_reports_failure(
    tmp_path: Path,
) -> None:
    spool = make_spool(tmp_path)
    study_uid = "1.2.3.4.5.6"
    series_uid = "1.2.3.4.5.6.1"
    make_dicom_file(
        tmp_path / "spool" / study_uid / series_uid / "1.dcm",
        study_uid,
        series_uid,
        generate_uid(),
    )
    study_id = spool.receive(study_uid)
    # Port 1 is almost certainly closed -> connection refused
    target = DICOMDestination(
        name="dead", type="dicom", host="127.0.0.1", port=1, aet_target="NOPE"
    )
    spool.enqueue(study_id, [target])

    fwd = make_forwarder(spool, target)
    count = fwd.process_once()

    assert count == 1
    # max_attempts=1 -> single failure marks the study FAILED (not SENT)
    assert spool.state(study_id) == StudyState.FAILED


# ── Slice 3: no DICOM files on disk fails gracefully ───────────────────


def test_no_files_on_disk_fails(tmp_path: Path, dicom_scp: tuple[list[object], int]) -> None:
    _, port = dicom_scp
    spool = make_spool(tmp_path)
    study_uid = "1.2.3.4.5.6"
    # Study exists in the DB but no files were written to the spool dir
    study_id = spool.receive(study_uid)
    target = DICOMDestination(
        name="pacs", type="dicom", host="127.0.0.1", port=port, aet_target="TESTSCP"
    )
    spool.enqueue(study_id, [target])

    fwd = make_forwarder(spool, target)
    count = fwd.process_once()

    assert count == 1
    assert spool.state(study_id) == StudyState.FAILED


# ── Slice 5 (review F7): compressed transfer syntax must forward ───────


def test_compressed_study_forwards_to_live_scp(
    tmp_path: Path, dicom_scp: tuple[list[object], int]
) -> None:
    """A study stored with JPEG 2000 (e.g. decompress_common=false or codec
    unavailable) must still deliver — the SCU negotiates compressed
    presentation contexts (review F7)."""
    received, port = dicom_scp
    spool = make_spool(tmp_path)
    study_uid = "1.2.3.4.5.6"
    series_uid = "1.2.3.4.5.6.1"

    from pydicom.uid import RLELossless

    path = tmp_path / "spool" / study_uid / series_uid / "1.dcm"
    make_dicom_file(path, study_uid, series_uid, generate_uid())
    # Re-write as RLE Lossless: add minimal 8-bit pixel data (1x1) so pydicom
    # can encode it (RLE needs no external codec), then compress. This mirrors
    # a study stored as-is because decompress_common is off or the codec for
    # the received syntax was unavailable.
    ds = pydicom.dcmread(str(path))
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows = 1
    ds.Columns = 1
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.PixelData = b"\x80"
    ds.compress(RLELossless)
    ds.save_as(str(path), enforce_file_format=True)

    study_id = spool.receive(study_uid)
    target = DICOMDestination(
        name="pacs", type="dicom", host="127.0.0.1", port=port, aet_target="TESTSCP"
    )
    spool.enqueue(study_id, [target])

    fwd = make_forwarder(spool, target, max_attempts=1)
    count = fwd.process_once()

    assert count == 1
    assert spool.state(study_id) == StudyState.SENT, (
        "compressed study failed to forward — SCU offered no compressed context"
    )
    assert len(received) == 1


# ── Slice 4: SCP rejects association ───────────────────────────────────


@pytest.fixture()
def rejecting_scp() -> Iterator[tuple[list[object], int]]:
    received: list[object] = []

    def handle_store(event: object) -> int:
        received.append(event)
        return 0x0000

    scp = AE(ae_title="TESTSCP")
    scp.require_called_aet = True
    scp.add_supported_context(CTContext)
    server = scp.start_server(
        ("127.0.0.1", 0),
        evt_handlers=[(evt.EVT_C_STORE, handle_store)],
        block=False,
    )
    port = server.server_address[1]
    yield received, port
    server.shutdown()


def test_association_rejected(tmp_path: Path, rejecting_scp: tuple[list[object], int]) -> None:
    received, port = rejecting_scp
    spool = make_spool(tmp_path)
    study_uid = "1.2.3.4.5.6"
    series_uid = "1.2.3.4.5.6.1"
    make_dicom_file(
        tmp_path / "spool" / study_uid / series_uid / "1.dcm",
        study_uid,
        series_uid,
        generate_uid(),
    )
    study_id = spool.receive(study_uid)
    # Wrong AE title -> SCP rejects
    target = DICOMDestination(
        name="pacs",
        type="dicom",
        host="127.0.0.1",
        port=port,
        aet_target="WRONG",
    )
    spool.enqueue(study_id, [target])

    fwd = make_forwarder(spool, target)
    count = fwd.process_once()

    assert count == 1
    assert spool.state(study_id) == StudyState.FAILED
    assert len(received) == 0


# ── Slice 5: association budget (review P1-12) ─────────────────────────


def test_timeouts_are_applied_to_the_ae(tmp_path: Path) -> None:
    """The DIMSE budget is bound explicitly rather than left implicit.

    pynetdicom 3.0.4 has no ``timeout=`` kwarg on ``associate()`` — unlike the
    other handlers, which pass one — so the budget has to be set on the AE
    instance. A site with a slow PACS raises it via ``timeout_sec``.
    """
    target = DICOMDestination(
        name="pacs",
        type="dicom",
        host="127.0.0.1",
        port=11112,
        aet_target="PACS",
        timeout_sec=90.0,
    )
    handler = DICOMHandler(target, make_spool(tmp_path))

    ae = AE(ae_title="GATEWAY")
    handler._apply_timeouts(ae)

    assert ae.connection_timeout == 90.0
    assert ae.acse_timeout == 90.0
    assert ae.network_timeout == 90.0


def test_timeouts_fall_back_to_the_default(tmp_path: Path) -> None:
    """An unset ``timeout_sec`` inherits the DIMSE convention, not None."""
    target = DICOMDestination(
        name="pacs", type="dicom", host="127.0.0.1", port=11112, aet_target="PACS"
    )
    handler = DICOMHandler(target, make_spool(tmp_path))

    ae = AE(ae_title="GATEWAY")
    handler._apply_timeouts(ae)

    assert ae.connection_timeout == _DEFAULT_ASSOCIATE_TIMEOUT_SEC
    assert ae.acse_timeout == _DEFAULT_ASSOCIATE_TIMEOUT_SEC
    assert ae.network_timeout == _DEFAULT_ASSOCIATE_TIMEOUT_SEC


def test_the_tcp_connect_phase_is_bounded(tmp_path: Path) -> None:
    """``connection_timeout`` is the only attribute bounding ``socket.connect``.

    It defaults to ``None`` (blocking) and is what ``AE.connect`` passes to
    ``settimeout`` before connecting — so a black-holed host pins the worker
    until the OS stack gives up. Unlike acse/network/dimse, the library ships
    no default for it; without this line the phase is unbounded.
    """
    target = DICOMDestination(
        name="pacs", type="dicom", host="10.0.0.99", port=11112, aet_target="PACS"
    )
    handler = DICOMHandler(target, make_spool(tmp_path))

    ae = AE(ae_title="GATEWAY")
    handler._apply_timeouts(ae)

    assert ae.connection_timeout is not None, (
        "the connect phase must be bounded — AE.connection_timeout defaults to "
        "None, and nothing in pynetdicom fills it in"
    )


def test_pynetdicom_defaults_already_bound_a_silent_peer() -> None:
    """Guard the correction to review P1-12: on the pinned pynetdicom, a peer
    that accepts the socket but never answers cannot hang the worker forever.

    The review described DIMSE associations as waiting indefinitely. They do
    not — the library's own defaults bound them, so the fix here is explicit
    configurability rather than a missing timeout. If pynetdicom ever regresses
    these defaults, this test fails and the budget becomes load-bearing.
    """
    ae = AE(ae_title="GATEWAY")
    assert ae.acse_timeout is not None
    assert ae.network_timeout is not None
    assert ae.dimse_timeout is not None
