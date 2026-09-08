"""TDD (S02-T3, GREEN): DICOM file storage + selective decompression.

Behaviors (PRD §3.4-1, §5.2-2, refinement §2.1):
1. Instance persisted at ``spool/{studyUID}/{seriesUID}/{instanceUID}.dcm``
   before the ack status returns (store-before-acknowledge ordering)
2. Persisted files are self-contained Part-10 files that re-read cleanly
3. Original transfer syntax provenance kept in the ``.tags`` sidecar even
   after decompression
4. Selective decompression: common syntaxes (JPEG 2000 lossless, JPEG-LS,
   RLE) decompressed on receive when ``receiver.decompress_common`` is on;
   rare/proprietary syntaxes stored as-is; flag off = store everything as-is
"""

from __future__ import annotations

import struct
from pathlib import Path

import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import (
    CTImageStorage,
    DeflatedExplicitVRLittleEndian,
    ExplicitVRLittleEndian,
    JPEG2000Lossless,
    RLELossless,
)

from mercure_gateway.config import default_config
from mercure_gateway.spool import Spool, StudyState
from mercure_gateway.spool.db import mem_database


def make_dataset(
    study_uid: str = "1.2.3.4.5",
    syntax_uid: str = ExplicitVRLittleEndian,
) -> Dataset:
    ds = Dataset()
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = f"{study_uid}.1"
    ds.SOPInstanceUID = f"{study_uid}.1.1"
    ds.SOPClassUID = CTImageStorage
    ds.Modality = "CT"
    ds.PatientName = "TEST^P"
    ds.AccessionNumber = "A1"
    ds.Rows = 8
    ds.Columns = 8
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = CTImageStorage
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.file_meta.TransferSyntaxUID = syntax_uid
    return ds


def rle_pixel_data(width: int = 8, height: int = 8) -> bytes:
    """Minimal valid RLE-encoded 8-bit grayscale payload."""
    pixels = bytes(range(width * height))
    num_segments = 1
    header = struct.pack("<I", num_segments)
    header += struct.pack("<I", 64)  # offset of segment 1
    header += b"\x00" * (64 - 8)     # pad header to 64 bytes
    segment = struct.pack("<I", len(pixels)) + pixels
    return header + segment


def make_spool(tmp_path: Path, *, decompress_common: bool = True) -> Spool:
    cfg = default_config()
    cfg.storage.spool_dir = str(tmp_path / "spool")
    cfg.receiver.decompress_common = decompress_common
    return Spool(mem_database(), cfg)


class TestFileStorage:
    def test_layout_and_self_contained_file(self, tmp_path: Path) -> None:
        spool = make_spool(tmp_path)
        study_uid = "1.2.3.4.5"
        ds = make_dataset(study_uid)

        study_id = spool.store_instance(ds)

        expected = (
            tmp_path / "spool" / study_uid / f"{study_uid}.1" / f"{study_uid}.1.1.dcm"
        )
        assert expected.exists()

        # Persisted file re-reads cleanly as a Part-10 file
        reloaded = pydicom.dcmread(str(expected))
        assert str(reloaded.StudyInstanceUID) == study_uid
        assert str(reloaded.SOPInstanceUID) == f"{study_uid}.1.1"
        assert reloaded.file_meta.TransferSyntaxUID == ExplicitVRLittleEndian

        # DB row exists and matches (store-before-ack: file + row before return)
        row = spool._db.get_study(study_id)
        assert row["study_uid"] == study_uid
        assert row["state"] == StudyState.RECEIVED.value

    def test_second_instance_increments_counters(self, tmp_path: Path) -> None:
        spool = make_spool(tmp_path)
        study_uid = "1.2.3.4.5"
        ds1 = make_dataset(study_uid)
        ds2 = make_dataset(study_uid)
        ds2.SOPInstanceUID = f"{study_uid}.1.2"

        spool.store_instance(ds1)
        spool.store_instance(ds2)

        row = spool._db.get_study_by_uid(study_uid)
        assert row["num_instances"] == 2

    def test_overwrite_same_instance_is_idempotent(self, tmp_path: Path) -> None:
        spool = make_spool(tmp_path)
        study_uid = "1.2.3.4.5"
        ds = make_dataset(study_uid)

        spool.store_instance(ds)
        spool.store_instance(ds)  # modality retried the same instance

        files = list((tmp_path / "spool").rglob("*.dcm"))
        assert len(files) == 1
        assert spool._db.get_study_by_uid(study_uid)["num_instances"] == 1

    def test_uid_validation_blocks_traversal(self, tmp_path: Path) -> None:
        spool = make_spool(tmp_path)
        ds = make_dataset("1.2.3.4.5")
        ds.StudyInstanceUID = "../../evil"

        from mercure_gateway.spool import InvalidUIDError

        with pytest.raises(InvalidUIDError):
            spool.store_instance(ds)

        # Nothing written outside the spool
        assert list((tmp_path / "spool").rglob("*.dcm")) == []


class TestSelectiveDecompression:
    def _receive_rle(self, tmp_path: Path, *, decompress_common: bool) -> Spool:
        spool = make_spool(tmp_path, decompress_common=decompress_common)
        ds = make_dataset(syntax_uid=RLELossless)
        from pydicom.encaps import encapsulate

        ds.PixelData = encapsulate([rle_pixel_data()])
        ds["PixelData"].is_undefined_length = True
        spool.store_instance(ds)
        return spool

    def test_common_syntax_decompressed_on_receive(self, tmp_path: Path) -> None:
        """RLE (a 'common' syntax per refinement §2.1) is stored uncompressed."""
        spool = self._receive_rle(tmp_path, decompress_common=True)

        stored = spool.study_files("1.2.3.4.5")[0]
        reloaded = pydicom.dcmread(str(stored))

        assert reloaded.file_meta.TransferSyntaxUID == ExplicitVRLittleEndian
        assert not reloaded.file_meta.TransferSyntaxUID.is_compressed

    def test_original_syntax_preserved_in_provenance(self, tmp_path: Path) -> None:
        """Decompression must not lose provenance: the original syntax UID is
        recorded (S02-T3 AC: 'store original transfer syntax UID in metadata')."""
        spool = self._receive_rle(tmp_path, decompress_common=True)

        stored = spool.study_files("1.2.3.4.5")[0]
        reloaded = pydicom.dcmread(str(stored))

        # Private provenance tag (creator: mercure-gateway) or file_meta backup
        assert str(reloaded.file_meta.TransferSyntaxUID) == str(ExplicitVRLittleEndian)
        # Provenance recorded in the dataset via a private block
        block = reloaded.private_block(0x000B, "mercure-gateway", create=False)
        assert block is not None
        assert str(block[0x01].value) == str(RLELossless)

    def test_flag_off_stores_compressed_as_is(self, tmp_path: Path) -> None:
        """decompress_common=False → store in the received syntax untouched."""
        spool = self._receive_rle(tmp_path, decompress_common=False)

        stored = spool.study_files("1.2.3.4.5")[0]
        reloaded = pydicom.dcmread(str(stored))

        assert str(reloaded.file_meta.TransferSyntaxUID) == str(RLELossless)

    def test_native_syntax_untouched_even_with_flag_on(self, tmp_path: Path) -> None:
        spool = make_spool(tmp_path, decompress_common=True)
        ds = make_dataset(syntax_uid=ExplicitVRLittleEndian)

        spool.store_instance(ds)

        stored = spool.study_files("1.2.3.4.5")[0]
        reloaded = pydicom.dcmread(str(stored))
        assert str(reloaded.file_meta.TransferSyntaxUID) == str(ExplicitVRLittleEndian)
        # No private provenance needed when nothing was transformed
        with pytest.raises(KeyError):
            reloaded.private_block(0x000B, "mercure-gateway", create=False)

    def test_deflated_stored_as_is(self, tmp_path: Path) -> None:
        """Deflated is a native-syntax compression applied to the whole dataset;
        pydicom handles read/write transparently — no pixel decompression applies."""
        spool = make_spool(tmp_path, decompress_common=True)
        ds = make_dataset(syntax_uid=DeflatedExplicitVRLittleEndian)

        spool.store_instance(ds)

        stored = spool.study_files("1.2.3.4.5")[0]
        reloaded = pydicom.dcmread(str(stored))
        assert str(reloaded.file_meta.TransferSyntaxUID) == str(
            DeflatedExplicitVRLittleEndian
        )

    def test_unknown_compressed_syntax_passes_through(self, tmp_path: Path) -> None:
        """A syntax not in the 'common' list (e.g. JPEG 2000 without codecs
        installed) is stored as-is rather than failing the receive."""
        spool = make_spool(tmp_path, decompress_common=True)
        ds = make_dataset(syntax_uid=JPEG2000Lossless)
        from pydicom.encaps import encapsulate

        ds.PixelData = encapsulate([b"\xff\x4f\xff\x51" + b"\x00" * 16])
        ds["PixelData"].is_undefined_length = True

        # Must not raise even if JPEG2000 codecs are unavailable
        study_id = spool.store_instance(ds)
        assert study_id > 0
        assert len(spool.study_files("1.2.3.4.5")) == 1


class TestStoreBeforeAcknowledgeDurability:
    """Review H1: the ack is only honest if the bytes survive power loss.

    ``store_instance`` must flush the instance file and the directory entries
    that name it *before* the database row claims the study is RECEIVED.  If
    those two steps are swapped, a crash in between leaves a study the database
    insists exists with no file behind it — and the C-STORE was already acked.
    """

    def test_fsync_barrier_runs_before_the_database_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spool = make_spool(tmp_path)
        events: list[str] = []

        real_fsync = spool._fsync_instance
        real_upsert = spool._db.upsert_study_instance

        def fsync_spy(path: Path, *, dirs: list[Path]) -> None:
            events.append("fsync")
            real_fsync(path, dirs=dirs)

        def upsert_spy(**kwargs: object) -> int:
            events.append("commit")
            return real_upsert(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(spool, "_fsync_instance", fsync_spy)
        monkeypatch.setattr(spool._db, "upsert_study_instance", upsert_spy)

        spool.store_instance(make_dataset())

        assert "fsync" in events and "commit" in events
        assert events.index("fsync") < events.index("commit"), (
            f"instance bytes must be durable before the DB row commits, got {events}"
        )

    def test_barrier_flushes_the_file_and_its_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spool = make_spool(tmp_path)
        fsynced: list[int] = []
        monkeypatch.setattr(
            "mercure_gateway.spool.os.fsync", lambda fd: fsynced.append(fd)
        )

        path = tmp_path / "spool" / "1.2.3.4.5" / "1.2.3.4.5.1" / "1.2.3.4.5.1.1.dcm"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"DICM")

        spool._fsync_instance(path, dirs=[path.parent])

        # One fsync for the file, one for the directory entry naming it.
        assert len(fsynced) == 2

    def test_newly_created_ancestors_are_synced_too(self, tmp_path: Path) -> None:
        out_dir = tmp_path / "spool" / "1.2.3.4.5" / "1.2.3.4.5.1"
        out_dir.mkdir(parents=True)

        # Nothing pre-existed: the series dir is new, so the study dir that
        # names it must be synced, and so must the spool root that names it.
        dirs = Spool._dirs_to_sync(out_dir, set())
        assert dirs == [out_dir, out_dir.parent, out_dir.parent.parent]

        # Everything already existed: only the file's own directory is needed.
        dirs = Spool._dirs_to_sync(out_dir, {out_dir, out_dir.parent})
        assert dirs == [out_dir]

    def test_store_refuses_to_acknowledge_when_fsync_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spool = make_spool(tmp_path)

        def boom(fd: int) -> None:
            raise OSError("ENOSPC: cannot flush")

        monkeypatch.setattr("mercure_gateway.spool.os.fsync", boom)

        with pytest.raises(OSError):
            spool.store_instance(make_dataset())

        # Nothing was committed: the study must not appear as received.
        assert spool._db.list_studies() == []

    def test_database_commits_are_durable_across_power_loss(self, tmp_path: Path) -> None:
        """WAL + synchronous=NORMAL would lose an acked commit on power loss.

        Only FULL fsyncs the WAL at COMMIT. The DB is the source of truth for
        "we hold this study", so it must not be able to forget (review H1).
        """
        from mercure_gateway.spool.db import open_database

        db = open_database(tmp_path / "spool.db")
        try:
            mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
            synchronous = db._conn.execute("PRAGMA synchronous").fetchone()[0]
        finally:
            db.close()

        assert mode.lower() == "wal"
        assert synchronous == 2, f"synchronous must be FULL (2), got {synchronous}"
