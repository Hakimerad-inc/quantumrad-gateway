"""TDD (S01-T2, RED): Fake modality SCU for development and testing.

Behaviors:
1. Synthetic study generation produces valid DICOM datasets with correct UIDs
2. Synthetic study datasets have required patient/study tags populated
3. ``send_study`` sends instances to a live Receiver and succeeds
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from pydicom.dataset import Dataset

from mercure_gateway.config import ReceiverConfig
from mercure_gateway.receiver import Receiver
from mercure_gateway.spool import Spool
from mercure_gateway.spool.db import mem_database


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _spool_config(spool_dir: str) -> object:
    from mercure_gateway.config import default_config
    cfg = default_config()
    cfg.storage.spool_dir = spool_dir
    return cfg


def make_spool(tmp_path: Path) -> Spool:
    return Spool(mem_database(), _spool_config(str(tmp_path / "spool")))


@pytest.fixture()
def receiver(tmp_path: Path) -> Receiver:
    spool = make_spool(tmp_path)
    recv = Receiver(ReceiverConfig(ae_title="GATEWAY", port=free_port()), spool)
    recv.start()
    yield recv
    recv.stop()


# ── Test 1: Synthetic study generation ────────────────────────────────


def test_synthetic_study_generates_correct_datasets() -> None:
    from demo.fake_modality import FakeModality

    fake = FakeModality()
    datasets = fake.create_synthetic_study(
        study_uid="1.2.840.10008.1.1",
        num_series=2,
        instances_per_series=3,
        modality="CT",
    )

    assert len(datasets) == 6  # 2 series × 3 instances
    assert all(isinstance(ds, Dataset) for ds in datasets)

    # All instances share the same study UID
    assert all(str(ds.StudyInstanceUID) == "1.2.840.10008.1.1" for ds in datasets)

    # Series UIDs are deterministic
    series_0 = [ds for ds in datasets if str(ds.SeriesInstanceUID).endswith(".1")]
    series_1 = [ds for ds in datasets if str(ds.SeriesInstanceUID).endswith(".2")]
    assert len(series_0) == 3
    assert len(series_1) == 3

    # Instance UIDs are unique and deterministic
    uids = [str(ds.SOPInstanceUID) for ds in datasets]
    assert len(set(uids)) == 6

    # All required tags present
    for ds in datasets:
        assert str(ds.PatientName) == "TEST^FAKE"
        assert str(ds.Modality) == "CT"
        assert str(ds.AccessionNumber) == "ACC-1"
        assert str(ds.SOPClassUID) == "1.2.840.10008.5.1.4.1.1.2"  # CTImageStorage


# ── Test 2: Send study to a live Receiver ─────────────────────────────


def test_fake_modality_sends_to_receiver(receiver: Receiver) -> None:
    from demo.fake_modality import FakeModality

    fake = FakeModality(ae_title="FAKEMODALITY")
    datasets = fake.create_synthetic_study(
        study_uid="1.2.840.10008.1.2",
        num_series=1,
        instances_per_series=2,
        modality="MR",
    )

    result = fake.send_study(
        datasets,
        host="127.0.0.1",
        port=receiver.port,
        aet_target="GATEWAY",
    )

    assert result["success"] == 2
    assert result["failure"] == 0
    assert len(receiver.spool._db.list_studies()) == 1


# ── Test 3: Send from directory of DICOM files ────────────────────────


def test_fake_modality_sends_directory(receiver: Receiver, tmp_path: Path) -> None:
    from demo.fake_modality import FakeModality

    fake = FakeModality(ae_title="FAKEMODALITY")
    datasets = fake.create_synthetic_study(
        study_uid="1.2.840.10008.1.3",
        num_series=1,
        instances_per_series=2,
        modality="CT",
    )

    dicom_dir = tmp_path / "dicoms"
    dicom_dir.mkdir()
    for i, ds in enumerate(datasets):
        ds.save_as(str(dicom_dir / f"instance_{i}.dcm"), enforce_file_format=True)

    result = fake.send_directory(
        str(dicom_dir),
        host="127.0.0.1",
        port=receiver.port,
        aet_target="GATEWAY",
    )

    assert result["success"] == 2
    assert result["failure"] == 0
    assert len(receiver.spool._db.list_studies()) == 1
