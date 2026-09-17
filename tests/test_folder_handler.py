"""S07-T2 (RED): Folder handler — filesystem drop-folder delivery.

The Folder handler copies a study's DICOM files into a target directory
(drop-folder).  Behaviors:

1. Deliver copies every study file into the target path
2. A missing study fails gracefully with ``DeliveryResult(ok=False)``
3. Path templates (``{study_uid}``) are expanded per study
4. Filesystem errors are returned as ``DeliveryResult(ok=False)``
5. Copy is a copy, not a move (spool files remain)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mercure_gateway.config import FolderDestination, default_config
from mercure_gateway.forwarder import Forwarder, RetryPolicy
from mercure_gateway.forwarder.handlers.folder import FolderHandler
from mercure_gateway.spool import Spool, StudyState
from mercure_gateway.spool.db import mem_database


@pytest.fixture()
def spool(tmp_path: Path) -> Spool:
    cfg = default_config()
    cfg.storage.spool_dir = str(tmp_path / "spool")
    s = Spool(mem_database(), cfg)
    (tmp_path / "spool").mkdir(parents=True, exist_ok=True)
    return s


def _make_spool(tmp_path: Path) -> Spool:
    cfg = default_config()
    cfg.storage.spool_dir = str(tmp_path / "spool")
    s = Spool(mem_database(), cfg)
    (tmp_path / "spool").mkdir(parents=True, exist_ok=True)
    return s


def _write_study(spool: Spool, tmp_path: Path, study_uid: str) -> int:
    """Create a study with 2 files and return its id."""
    study_id = spool.receive(study_uid)
    study_dir = spool.spool_dir / study_uid / "1.2.3.4.5"
    study_dir.mkdir(parents=True, exist_ok=True)
    (study_dir / "1.dcm").write_bytes(b"dicom-1")
    (study_dir / "2.dcm").write_bytes(b"dicom-2")
    return study_id


def _claim_task(spool: Spool, study_id: int) -> object:
    """Enqueue a dummy route and claim it so the handler gets a real task."""
    dest = FolderDestination(name="drop", path="ignored")
    spool.enqueue(study_id, [dest])
    return spool.claim_next(limit=1)[0]


# ══════════════════════════════════════════════════════════════════════
# Basic copy
# ══════════════════════════════════════════════════════════════════════

def test_folder_handler_copies_study(tmp_path: Path) -> None:
    spool = _make_spool(tmp_path)
    study_id = _write_study(spool, tmp_path, "1.2.3.4.1")

    dest = FolderDestination(name="drop", path=str(tmp_path / "out"))
    handler = FolderHandler(dest, spool)
    task = _claim_task(spool, study_id)

    result = handler.deliver(task, spool.spool_dir)

    assert result.ok is True
    out = tmp_path / "out" / "1.2.3.4.1"
    assert (out / "1.dcm").read_bytes() == b"dicom-1"
    assert (out / "2.dcm").read_bytes() == b"dicom-2"


def test_folder_handler_is_copy_not_move(tmp_path: Path) -> None:
    spool = _make_spool(tmp_path)
    study_id = _write_study(spool, tmp_path, "1.2.3.4.1")

    dest = FolderDestination(name="drop", path=str(tmp_path / "out"))
    handler = FolderHandler(dest, spool)
    task = _claim_task(spool, study_id)
    handler.deliver(task, spool.spool_dir)

    # Original spool files must still exist
    assert spool.study_files("1.2.3.4.1")


# ══════════════════════════════════════════════════════════════════════
# Path templates
# ══════════════════════════════════════════════════════════════════════

def test_folder_handler_expands_study_uid_template(tmp_path: Path) -> None:
    spool = _make_spool(tmp_path)
    study_id = _write_study(spool, tmp_path, "1.2.3.4.1")

    dest = FolderDestination(name="drop", path=str(tmp_path / "out" / "{study_uid}"))
    handler = FolderHandler(dest, spool)
    task = _claim_task(spool, study_id)

    result = handler.deliver(task, spool.spool_dir)

    assert result.ok is True
    assert (tmp_path / "out" / "1.2.3.4.1" / "1.dcm").exists()


# ══════════════════════════════════════════════════════════════════════
# Failure cases
# ══════════════════════════════════════════════════════════════════════

def test_folder_handler_missing_study_fails(tmp_path: Path) -> None:
    spool = _make_spool(tmp_path)
    dest = FolderDestination(name="drop", path=str(tmp_path / "out"))
    handler = FolderHandler(dest, spool)

    # A study with no files on disk
    sid = spool.receive("1.2.3.4.9")
    task = _claim_task(spool, sid)
    result = handler.deliver(task, spool.spool_dir)
    assert result.ok is False


def test_folder_handler_unwritable_target_fails(tmp_path: Path) -> None:
    spool = _make_spool(tmp_path)
    study_id = _write_study(spool, tmp_path, "1.2.3.4.1")

    # Target path is a *file*, so mkdir/copy will fail
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    dest = FolderDestination(name="drop", path=str(blocker))
    handler = FolderHandler(dest, spool)
    task = _claim_task(spool, study_id)

    result = handler.deliver(task, spool.spool_dir)

    assert result.ok is False
    assert result.error


# ══════════════════════════════════════════════════════════════════════
# End-to-end through the forwarder
# ══════════════════════════════════════════════════════════════════════

def test_folder_handler_through_forwarder(tmp_path: Path) -> None:
    spool = _make_spool(tmp_path)
    _write_study(spool, tmp_path, "1.2.3.4.1")

    dest = FolderDestination(name="drop", path=str(tmp_path / "out"))
    fwd = Forwarder(default_config(), spool, retry=RetryPolicy(max_attempts=1))
    fwd.register_handler("folder", FolderHandler(dest, spool))

    spool.enqueue(_study_id_by_uid(spool, "1.2.3.4.1"), [dest])
    fwd.process_once()

    assert spool.state(_study_id_by_uid(spool, "1.2.3.4.1")) == StudyState.SENT


def _study_id_by_uid(spool: Spool, uid: str) -> int:
    row = spool._db.get_study_by_uid(uid)
    assert row is not None
    return int(row["id"])
