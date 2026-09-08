"""Review C4: the gateway must be modality-agnostic end to end.

The receiver (C-STORE SCP) has to accept every storage SOP class, and the
forwarding handler (C-STORE SCU) has to request the classes a study actually
contains. Before this, both negotiated CT + MR only, so an ultrasound or CR
study was rejected before it ever reached the spool.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import (
    ComputedRadiographyImageStorage,
    CTImageStorage,
    ExplicitVRLittleEndian,
    MRImageStorage,
    PositronEmissionTomographyImageStorage,
    SecondaryCaptureImageStorage,
    UltrasoundImageStorage,
)

from mercure_gateway.config import ReceiverConfig
from mercure_gateway.forwarder.handlers.dicom import DICOMHandler
from mercure_gateway.receiver import Receiver
from mercure_gateway.sop_classes import STORAGE_SOP_CLASSES, sop_classes_for_files

# Modalities the PRD explicitly calls out; none of these were accepted before.
REQUIRED_MODALITIES = {
    "CT": CTImageStorage,
    "MR": MRImageStorage,
    "US": UltrasoundImageStorage,
    "CR": ComputedRadiographyImageStorage,
    "PT": PositronEmissionTomographyImageStorage,
    "SC": SecondaryCaptureImageStorage,
}


def test_context_budget_is_within_the_protocol_limit() -> None:
    """DICOM allows 128 presentation contexts; the SCP needs one per class."""
    assert len(STORAGE_SOP_CLASSES) <= 120


def test_no_duplicate_sop_classes() -> None:
    assert len(set(STORAGE_SOP_CLASSES)) == len(STORAGE_SOP_CLASSES)


@pytest.mark.parametrize("modality", sorted(REQUIRED_MODALITIES))
def test_receiver_accepts_each_required_modality(modality: str) -> None:
    """Every required modality has a supported presentation context."""
    spool = object()  # Receiver only touches the spool inside event handlers
    receiver = Receiver(ReceiverConfig(ae_title="GATEWAY"), spool)  # type: ignore[arg-type]

    sop_class = REQUIRED_MODALITIES[modality]
    supported = {str(c.abstract_syntax) for c in receiver._ae.supported_contexts}

    assert str(sop_class) in supported, f"{modality} ({sop_class}) not accepted"


def test_receiver_covers_more_than_ct_and_mr() -> None:
    """Guards the original bug: a two-element context list."""
    spool = object()
    receiver = Receiver(ReceiverConfig(ae_title="GATEWAY"), spool)  # type: ignore[arg-type]

    assert len(receiver._ae.supported_contexts) == len(STORAGE_SOP_CLASSES)
    assert len(STORAGE_SOP_CLASSES) > 2


def _write_instance(path: Path, sop_class_uid: str, instance_uid: str) -> None:
    ds = Dataset()
    ds.SOPClassUID = sop_class_uid
    ds.SOPInstanceUID = instance_uid
    ds.StudyInstanceUID = "1.2.3"
    ds.SeriesInstanceUID = "1.2.3.1"
    ds.Modality = "OT"
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = sop_class_uid
    ds.file_meta.MediaStorageSOPInstanceUID = instance_uid
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    path.parent.mkdir(parents=True, exist_ok=True)
    ds.save_as(str(path), enforce_file_format=True)


def test_sop_classes_for_files_reads_only_the_present_classes(tmp_path: Path) -> None:
    _write_instance(tmp_path / "a.dcm", str(UltrasoundImageStorage), "1.2.3.1.1")
    _write_instance(tmp_path / "b.dcm", str(MRImageStorage), "1.2.3.1.2")
    _write_instance(tmp_path / "c.dcm", str(UltrasoundImageStorage), "1.2.3.1.3")

    found = sop_classes_for_files([tmp_path / "a.dcm", tmp_path / "b.dcm", tmp_path / "c.dcm"])

    assert set(found) == {UltrasoundImageStorage, MRImageStorage}


def test_sop_classes_for_files_skips_unreadable_files(tmp_path: Path) -> None:
    good = tmp_path / "good.dcm"
    _write_instance(good, str(CTImageStorage), "1.2.3.1.1")
    junk = tmp_path / "junk.dcm"
    junk.write_bytes(b"this is not a DICOM file")

    assert sop_classes_for_files([good, junk]) == (CTImageStorage,)


class _FakeAssoc:
    """Association stand-in that accepts every C-STORE."""

    is_established = True

    def send_c_store(self, ds: object) -> object:
        class _Status:
            Status = 0x0000

        return _Status()

    def release(self) -> None:
        return None


class _RecordingAE:
    """Stand-in for pynetdicom's AE that records requested presentation contexts.

    Replacing ``AE`` (rather than the socket) lets the test exercise the real
    context-selection logic in ``DICOMHandler._send_files``.
    """

    last: _RecordingAE | None = None

    def __init__(self, ae_title: str) -> None:
        self.ae_title = ae_title
        self.maximum_pdu_size = 0
        self.requested: list[tuple[str, object]] = []
        _RecordingAE.last = self

    def add_requested_context(self, abstract_syntax: object, transfer_syntax: object) -> None:
        self.requested.append((str(abstract_syntax), transfer_syntax))

    def associate(self, host: str, port: int, ae_title: str | None = None) -> _FakeAssoc:
        return _FakeAssoc()


@pytest.fixture()
def handler(monkeypatch: pytest.MonkeyPatch) -> DICOMHandler:
    from mercure_gateway.config import DICOMDestination
    from mercure_gateway.forwarder.handlers import dicom as dicom_handler

    monkeypatch.setattr(dicom_handler, "AE", _RecordingAE)
    return DICOMHandler(
        DICOMDestination(name="pacs", host="pacs.local", port=104, aet_target="PACS"),
        spool=None,  # type: ignore[arg-type]  # only used by deliver(), not _send_files
    )


def test_forwarder_requests_the_studys_own_sop_class(
    tmp_path: Path, handler: DICOMHandler
) -> None:
    """A US study must negotiate US, not the old hard-coded CT/MR pair."""
    us_file = tmp_path / "us.dcm"
    _write_instance(us_file, str(UltrasoundImageStorage), "1.2.3.1.1")

    handler._send_files([us_file])

    assert _RecordingAE.last is not None
    requested = _RecordingAE.last.requested
    assert requested, "no presentation context requested"
    assert {sop for sop, _ in requested} == {str(UltrasoundImageStorage)}


def test_forwarder_requests_each_class_in_a_mixed_study(
    tmp_path: Path, handler: DICOMHandler
) -> None:
    """A study spanning two SOP classes negotiates both."""
    _write_instance(tmp_path / "a.dcm", str(UltrasoundImageStorage), "1.2.3.1.1")
    _write_instance(tmp_path / "b.dcm", str(MRImageStorage), "1.2.3.1.2")

    handler._send_files([tmp_path / "a.dcm", tmp_path / "b.dcm"])

    assert _RecordingAE.last is not None
    requested = _RecordingAE.last.requested
    assert {sop for sop, _ in requested} == {str(UltrasoundImageStorage), str(MRImageStorage)}
    # One context with all syntaxes, plus one per dedicated compressed syntax.
    assert len(requested) == 2 * 4


def test_forwarder_falls_back_to_all_classes_when_unrecognised(
    tmp_path: Path, handler: DICOMHandler
) -> None:
    """A SOP class outside the known list still gets delivered.

    Rather than fail the task on a class we do not recognise, request every
    storage context and let the SCP accept the one it knows.
    """
    from mercure_gateway.forwarder.handlers.dicom import _STORAGE_CONTEXTS

    # Valid DICOM, but a non-storage abstract syntax (Verification SOP Class).
    _write_instance(tmp_path / "v.dcm", "1.2.840.10008.1.1", "1.2.3.1.1")

    handler._send_files([tmp_path / "v.dcm"])

    assert _RecordingAE.last is not None
    assert len(_RecordingAE.last.requested) == len(_STORAGE_CONTEXTS) * 4
