"""Fake modality SCU for development and testing (S01-T2/T6).

Simulates a DICOM modality: generates synthetic studies and C-STORE sends
them to a target SCP (the gateway receiver, mercure hub, or test PACS).

Usage (CLI)::

    python -m demo.fake_modality --host 127.0.0.1 --port 11112 --aet GATEWAY
    python -m demo.fake_modality --dir /path/to/dicoms --host 127.0.0.1 --port 11112

The library surface (``create_synthetic_study`` / ``send_study`` /
``send_directory``) is the RED contract for the Sprint 01 demo chain.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import UID, CTImageStorage, ExplicitVRLittleEndian, MRImageStorage
from pynetdicom import AE

__all__ = ["FakeModality", "main"]

# SOP classes this fake modality can emit (PRD §5.5 baseline CT/MR).
_SOP_CLASSES = {
    "CT": CTImageStorage,
    "MR": MRImageStorage,
}

_MAX_PDU_SIZE = 131072

# Deterministic UID roots so synthetic studies are reproducible.
_STUDY_ROOT = "1.2.840.10008"
_STUDY_SUFFIX = ".999.1"


class FakeModality:
    """A scriptable modality that generates and sends synthetic studies."""

    def __init__(self, ae_title: str = "FAKEMODALITY") -> None:
        self.ae_title = ae_title

    def create_synthetic_study(
        self,
        study_uid: str,
        *,
        num_series: int = 1,
        instances_per_series: int = 1,
        modality: str = "CT",
    ) -> list[Dataset]:
        """Generate ``num_series * instances_per_series`` synthetic instances.

        All instances share ``study_uid``; each series and instance gets a
        deterministic UID derived from it. The dataset carries minimal tags
        a real modality would send (patient, study, series, instance).
        """
        sop_class = _SOP_CLASSES[modality]
        datasets: list[Dataset] = []
        for series_no in range(1, num_series + 1):
            series_uid = f"{study_uid}.{series_no}"
            for instance_no in range(1, instances_per_series + 1):
                instance_uid = f"{study_uid}.{series_no}.{instance_no}"
                ds = Dataset()
                ds.StudyInstanceUID = study_uid
                ds.SeriesInstanceUID = series_uid
                ds.SOPInstanceUID = instance_uid
                ds.SOPClassUID = sop_class
                ds.PatientName = "TEST^FAKE"
                ds.PatientID = "FAKE-0001"
                ds.AccessionNumber = "ACC-1"
                ds.Modality = modality
                ds.StudyDescription = "Fake modality demo study"
                ds.SeriesNumber = series_no
                ds.InstanceNumber = instance_no
                ds.file_meta = FileMetaDataset()
                ds.file_meta.MediaStorageSOPClassUID = sop_class
                ds.file_meta.MediaStorageSOPInstanceUID = UID(instance_uid)
                ds.file_meta.TransferSyntaxUID = UID(ExplicitVRLittleEndian)
                datasets.append(ds)
        return datasets

    def send_study(
        self,
        datasets: list[Dataset],
        *,
        host: str,
        port: int,
        aet_target: str,
        aet_source: str | None = None,
    ) -> dict[str, int]:
        """C-STORE ``datasets`` to ``host:port`` as one association.

        Returns ``{"success": n, "failure": n}`` counted per instance. One
        association is opened for the whole study (as a real modality does).
        """
        ae = AE(ae_title=aet_source or self.ae_title)
        ae.maximum_pdu_size = _MAX_PDU_SIZE
        seen: set[tuple[str, str]] = set()
        for ds in datasets:
            key = (str(ds.SOPClassUID), str(ds.file_meta.TransferSyntaxUID))
            if key not in seen:
                seen.add(key)
                ae.add_requested_context(ds.SOPClassUID, ds.file_meta.TransferSyntaxUID)

        assoc = ae.associate(host, port, ae_title=aet_target)
        if not assoc.is_established:
            return {"success": 0, "failure": len(datasets)}

        success = 0
        failure = 0
        try:
            for ds in datasets:
                status = assoc.send_c_store(ds)
                if status is not None and status.Status in (0x0000, 0xFF00):
                    success += 1
                else:
                    failure += 1
        finally:
            assoc.release()
        return {"success": success, "failure": failure}

    def send_directory(
        self,
        dicom_dir: str | Path,
        *,
        host: str,
        port: int,
        aet_target: str,
        aet_source: str | None = None,
    ) -> dict[str, int]:
        """C-STORE every ``*.dcm`` file under ``dicom_dir`` to the target."""
        files = sorted(Path(dicom_dir).rglob("*.dcm"))
        datasets: list[Dataset] = [pydicom.dcmread(str(f)) for f in files]
        return self.send_study(
            datasets,
            host=host,
            port=port,
            aet_target=aet_target,
            aet_source=aet_source,
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fake-modality",
        description="Send synthetic or on-disk DICOM studies to a target SCP.",
    )
    parser.add_argument("--host", required=True, help="Target SCP host")
    parser.add_argument("--port", type=int, required=True, help="Target SCP port")
    parser.add_argument("--aet", required=True, help="Target called AE title")
    parser.add_argument("--source-aet", default="FAKEMODALITY", help="Calling AE title")
    parser.add_argument(
        "--dir",
        default=None,
        help="Directory of DICOM files to send (default: a synthetic study)",
    )
    parser.add_argument(
        "--study-uid",
        default=f"{_STUDY_ROOT}.1{_STUDY_SUFFIX}",
        help="Study Instance UID for synthetic studies",
    )
    parser.add_argument("--modality", default="CT", choices=sorted(_SOP_CLASSES))
    parser.add_argument("--series", type=int, default=1, help="Series for synthetic studies")
    parser.add_argument("--instances", type=int, default=1, help="Instances per series")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the fake modality."""
    args = _build_parser().parse_args(argv)
    fake = FakeModality(ae_title=args.source_aet)

    if args.dir:
        result = fake.send_directory(
            args.dir,
            host=args.host,
            port=args.port,
            aet_target=args.aet,
        )
        print(f"Sent directory {args.dir!r}: {result}")
    else:
        datasets = fake.create_synthetic_study(
            args.study_uid,
            num_series=args.series,
            instances_per_series=args.instances,
            modality=args.modality,
        )
        result = fake.send_study(
            datasets,
            host=args.host,
            port=args.port,
            aet_target=args.aet,
        )
        print(f"Sent synthetic study {args.study_uid!r}: {result}")

    return 0 if result["failure"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
