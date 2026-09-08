"""TDD (S02-T4, RED): ``*.tags`` metadata sidecars per received instance.

mercure's ``getdcmtags`` extracts a flat tag summary alongside each DICOM
file (PRD §5.2-2, §8.3). The gateway writes a ``<instance_uid>.tags`` sidecar
next to each ``.dcm`` file containing the tags used for routing and display.

Behaviors:
1. A sidecar exists per stored instance, next to the .dcm file
2. The sidecar parses as JSON and carries Modality/AccessionNumber/
   StudyInstanceUID/PatientID/SeriesInstanceUID/SOPInstanceUID
3. Sidecar values match the DB study row (Files + DB consistent)
4. Tags are flat ``{keyword: string value}`` — no binary data, no PHI beyond
   the configured tag list (mercure getdcmtags convention)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian

from mercure_gateway.config import default_config
from mercure_gateway.spool import Spool
from mercure_gateway.spool.db import mem_database


def make_dataset(study_uid: str = "1.2.3.4.5") -> Dataset:
    ds = Dataset()
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = f"{study_uid}.1"
    ds.SOPInstanceUID = f"{study_uid}.1.1"
    ds.SOPClassUID = CTImageStorage
    ds.Modality = "CT"
    ds.PatientName = "DOE^JOHN"
    ds.PatientID = "PID-42"
    ds.AccessionNumber = "ACC-77"
    ds.StudyDate = "20260829"
    ds.StudyDescription = "CHEST"
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = CTImageStorage
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    return ds


def make_spool(tmp_path: Path) -> Spool:
    cfg = default_config()
    cfg.storage.spool_dir = str(tmp_path / "spool")
    return Spool(mem_database(), cfg)


def read_tags(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8")))


class TestTagsSidecar:
    def test_sidecar_exists_per_instance(self, tmp_path: Path) -> None:
        spool = make_spool(tmp_path)
        spool.store_instance(make_dataset())

        dcm = spool.study_files("1.2.3.4.5")[0]
        sidecar = dcm.with_suffix(".tags")

        assert sidecar.exists()

    def test_sidecar_parses_and_contains_routing_tags(self, tmp_path: Path) -> None:
        spool = make_spool(tmp_path)
        spool.store_instance(make_dataset())

        dcm = spool.study_files("1.2.3.4.5")[0]
        tags = read_tags(dcm.with_suffix(".tags"))

        assert tags["Modality"] == "CT"
        assert tags["AccessionNumber"] == "ACC-77"
        assert tags["StudyInstanceUID"] == "1.2.3.4.5"
        assert tags["SeriesInstanceUID"] == "1.2.3.4.5.1"
        assert tags["SOPInstanceUID"] == "1.2.3.4.5.1.1"
        assert tags["PatientID"] == "PID-42"
        assert tags["StudyDate"] == "20260829"

    def test_sidecar_matches_db_row(self, tmp_path: Path) -> None:
        spool = make_spool(tmp_path)
        study_id = spool.store_instance(make_dataset())

        dcm = spool.study_files("1.2.3.4.5")[0]
        tags = read_tags(dcm.with_suffix(".tags"))
        row = spool._db.get_study(study_id)

        assert tags["StudyInstanceUID"] == row["study_uid"]
        assert tags["Modality"] == row["modality"]
        assert tags["AccessionNumber"] == row["accession"]
        assert tags["PatientID"] == row["mrn"]

    def test_sidecar_is_flat_json_without_binary(self, tmp_path: Path) -> None:
        """Sidecars feed the rules engine (S07-T6) — they must be flat,
        string-valued, JSON-parseable, and free of binary pixel data."""
        spool = make_spool(tmp_path)
        ds = make_dataset()
        # A private opaque binary element that must NOT appear in the sidecar
        from pydicom.datadict import add_private_dict_entries
        from pydicom.tag import Tag

        add_private_dict_entries(
            0x000B,
            {Tag(0x000B, 0x99): ("OB", "1", "Test Binary Blob")},
        )
        ds.add_new(Tag(0x000B, 0x99), "OB", b"\x00" * 64)
        spool.store_instance(ds)

        dcm = spool.study_files("1.2.3.4.5")[0]
        raw = dcm.with_suffix(".tags").read_text(encoding="utf-8")
        tags = json.loads(raw)

        assert isinstance(tags, dict)
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in tags.items())
        assert "PixelData" not in tags
        assert not any("0099" in k for k in tags)

    def test_missing_optional_tag_omitted(self, tmp_path: Path) -> None:
        spool = make_spool(tmp_path)
        ds = make_dataset()
        del ds.AccessionNumber
        spool.store_instance(ds)

        dcm = spool.study_files("1.2.3.4.5")[0]
        tags = read_tags(dcm.with_suffix(".tags"))

        assert "AccessionNumber" not in tags
        assert tags["Modality"] == "CT"
