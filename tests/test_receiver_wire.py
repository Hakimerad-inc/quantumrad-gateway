"""TDD (S02-T2, RED): pynetdicom C-STORE SCP transport over real associations.

Behaviors (US-01 acceptance criteria):
1. Real SCU association from pynetdicom over localhost delivers a dataset
2. Wrong calling-AET rejected when allow-list configured
3. ≥25 concurrent associations all complete (US-01 concurrency AC)
4. ALL compressed transfer syntaxes accepted (JPEG 2000 lossless/lossy,
   JPEG-LS, RLE, Deflated, JPLL) — refinement spec §2.1
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import (
    JPEG2000,
    CTImageStorage,
    DeflatedExplicitVRLittleEndian,
    ExplicitVRLittleEndian,
    JPEG2000Lossless,
    JPEGLosslessSV1,
    JPEGLSLossless,
    RLELossless,
)
from pynetdicom import AE

from mercure_gateway.config import ReceiverConfig
from mercure_gateway.receiver import Receiver
from mercure_gateway.spool import Spool
from mercure_gateway.spool.db import mem_database


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def make_spool(tmp_path: Path) -> Spool:
    cfg_path = tmp_path / "spool"
    return Spool(mem_database(), _spool_config(str(cfg_path)))


def _spool_config(spool_dir: str) -> object:
    from mercure_gateway.config import default_config

    cfg = default_config()
    cfg.storage.spool_dir = spool_dir
    return cfg


def make_dataset(
    study_uid: str,
    syntax_uid: str = ExplicitVRLittleEndian,
    instance_uid: str | None = None,
) -> Dataset:
    ds = Dataset()
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = f"{study_uid}.1"
    ds.SOPInstanceUID = instance_uid or f"{study_uid}.1.1"
    ds.SOPClassUID = CTImageStorage
    ds.Modality = "CT"
    ds.PatientName = "TEST^P"
    ds.AccessionNumber = "A1"
    # Compressed synthetic pixel data: tiny, valid per-syntax encodings are
    # produced by pydicom's encapsulation helpers below where needed.
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = CTImageStorage
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.file_meta.TransferSyntaxUID = syntax_uid
    return ds


def send_dataset(
    port: int, aet_called: str, dataset: Dataset, ae_title: str = "MODALITY"
) -> bool:
    ae = AE(ae_title=ae_title)
    ae.add_requested_context(dataset.SOPClassUID, dataset.file_meta.TransferSyntaxUID)
    assoc = ae.associate("127.0.0.1", port, ae_title=aet_called)
    if not assoc.is_established:
        return False
    status = assoc.send_c_store(dataset)
    assoc.release()
    return status is not None and status.Status in (0x0000, 0xFF00)


@pytest.fixture()
def receiver(tmp_path: Path) -> Receiver:
    spool = make_spool(tmp_path)
    recv = Receiver(ReceiverConfig(ae_title="GATEWAY", port=free_port()), spool)
    recv.start()
    yield recv
    recv.stop()


# ── Baseline: uncompressed over a real association ────────────────────


def test_real_association_delivers_dataset(receiver: Receiver) -> None:
    ok = send_dataset(receiver.port, "GATEWAY", make_dataset("1.2.3.4.5"))

    assert ok is True
    assert len(receiver.spool._db.list_studies()) == 1


# ── AE allow-list over the wire ───────────────────────────────────────


def test_wrong_aet_rejected_over_wire(tmp_path: Path) -> None:
    spool = make_spool(tmp_path)
    recv = Receiver(
        ReceiverConfig(
            ae_title="GATEWAY", port=free_port(), allowed_ae_titles=["KNOWNMOD"]
        ),
        spool,
    )
    recv.start()
    try:
        ok = send_dataset(recv.port, "GATEWAY", make_dataset("1.2.3.4.6"))
        assert ok is False
        assert recv.spool._db.list_studies() == []
    finally:
        recv.stop()


# ── Concurrency: ≥25 simultaneous associations (US-01 AC) ─────────────


def test_25_concurrent_associations(tmp_path: Path) -> None:
    spool = make_spool(tmp_path)
    recv = Receiver(
        ReceiverConfig(ae_title="GATEWAY", port=free_port(), max_associations=30),
        spool,
    )
    recv.start()
    try:
        results: list[bool] = []
        lock = threading.Lock()

        def send_one(i: int) -> None:
            ds = make_dataset(f"1.2.840.{i}")
            ok = send_dataset(recv.port, "GATEWAY", ds)
            with lock:
                results.append(ok)

        threads = [threading.Thread(target=send_one, args=(i,)) for i in range(25)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert results == [True] * 25
        assert len(recv.spool._db.list_studies()) == 25
    finally:
        recv.stop()


# ── ALL compressed transfer syntaxes (refinement §2.1) ────────────────

# Each syntax → an encapsulated 8×8 pixel payload pydicom can wrap.
_COMPRESSED_SYNTAXES = [
    pytest.param(JPEG2000Lossless, id="jpeg2000-lossless"),
    pytest.param(JPEG2000, id="jpeg2000-lossy"),
    pytest.param(JPEGLSLossless, id="jpeg-ls"),
    pytest.param(RLELossless, id="rle"),
    pytest.param(JPEGLosslessSV1, id="jpeg-lossless-sv1"),
]


def _with_encapsulated_pixels(ds: Dataset, syntax: str) -> Dataset:
    """Attach a minimal encapsulated pixel-data payload for *syntax*.

    RLE has a well-defined header format; the JPEG-family syntaxes just need
    *some* fragment structure (the SCP accepts storage of encapsulated data
    without decoding it — the gateway stores, it doesn't render).
    """
    from pydicom.encaps import encapsulate

    if syntax == RLELossless:
        # RLE header: 64-byte header, one segment of 64 pixels (8x8, 8-bit)
        import struct

        pixels = bytes(range(64))
        segment = struct.pack("<I", len(pixels)) + pixels
        if len(segment) % 2:
            segment += b"\x00"
        header = struct.pack("<I", 64) + b"\x00" * 60  # offset of segment 1
        ds.PixelData = encapsulate([header + segment])
    else:
        # JPEG-family: a single fragment of dummy compressed bytes. The
        # gateway stores opaque encapsulated data; decoding is out of scope.
        ds.PixelData = encapsulate([b"\xff\xd8" + b"\x00" * 30])
    ds["PixelData"].is_undefined_length = True
    return ds


@pytest.mark.parametrize(("syntax_uid",), _COMPRESSED_SYNTAXES)
def test_compressed_syntax_accepted(
    receiver: Receiver, syntax_uid: str
) -> None:
    ds = make_dataset("1.2.3.4.5.1", syntax_uid=syntax_uid)
    ds = _with_encapsulated_pixels(ds, syntax_uid)

    ok = send_dataset(receiver.port, "GATEWAY", ds)

    assert ok is True, f"compressed syntax {syntax_uid} rejected"
    assert len(receiver.spool._db.list_studies()) == 1


def test_deflated_explicit_vr_accepted(receiver: Receiver) -> None:
    """Deflated Explicit VR LE is a native (non-encapsulated) compressed
    syntax — pydicom writes it with zlib-compressed dataset bytes."""
    ds = make_dataset("1.2.3.4.5.2", syntax_uid=DeflatedExplicitVRLittleEndian)

    ok = send_dataset(receiver.port, "GATEWAY", ds)

    assert ok is True
    assert len(receiver.spool._db.list_studies()) == 1
