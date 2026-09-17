"""S07-T4 (RED): rsync handler — rsync-over-SSH delivery.

The rsync handler copies study files to a remote path via the ``rsync`` CLI
over SSH.  ``subprocess.run`` is mocked for unit tests.

Behaviors:
1. Files are rsynced to the configured remote path
2. Non-zero rsync exit → DeliveryResult(ok=False)
3. Files are copied (not moved)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mercure_gateway.config import RsyncDestination, default_config
from mercure_gateway.forwarder import Forwarder, RetryPolicy
from mercure_gateway.spool import Spool, StudyState
from mercure_gateway.spool.db import mem_database


@pytest.fixture()
def spool(tmp_path: Path) -> Spool:
    cfg = default_config()
    cfg.storage.spool_dir = str(tmp_path / "spool")
    s = Spool(mem_database(), cfg)
    (tmp_path / "spool").mkdir(parents=True, exist_ok=True)
    return s


def _write_study(spool: Spool, uid: str = "1.2.3.4.1") -> int:
    sid = spool.receive(uid)
    d = spool.spool_dir / uid / "1.2.3.4.5"
    d.mkdir(parents=True, exist_ok=True)
    (d / "1.dcm").write_bytes(b"dicom")
    return sid


def _claim_task(spool: Spool, sid: int, dest: RsyncDestination) -> object:
    spool.enqueue(sid, [dest])
    return spool.claim_next(limit=1)[0]


@patch("subprocess.run")
def test_rsync_delivers(mock_run, tmp_path: Path, spool: Spool) -> None:
    from mercure_gateway.forwarder.handlers.rsync import RsyncHandler

    mock_run.return_value = MagicMock(returncode=0)
    dest = RsyncDestination(name="nas", host="nas.local", username="u", remote_path="/data")
    handler = RsyncHandler(dest, spool)
    sid = _write_study(spool)
    task = _claim_task(spool, sid, dest)
    result = handler.deliver(task, spool.spool_dir)
    assert result.ok is True
    mock_run.assert_called_once()


@patch("subprocess.run")
def test_rsync_nonzero_exit_fails(mock_run, tmp_path: Path, spool: Spool) -> None:
    from mercure_gateway.forwarder.handlers.rsync import RsyncHandler

    mock_run.return_value = MagicMock(returncode=1)
    dest = RsyncDestination(name="nas", host="nas.local", username="u", remote_path="/data")
    handler = RsyncHandler(dest, spool)
    sid = _write_study(spool)
    task = _claim_task(spool, sid, dest)
    result = handler.deliver(task, spool.spool_dir)
    assert result.ok is False


@patch("subprocess.run")
def test_rsync_missing_study_fails(mock_run, tmp_path: Path, spool: Spool) -> None:
    from mercure_gateway.forwarder.handlers.rsync import RsyncHandler

    mock_run.return_value = MagicMock(returncode=0)
    dest = RsyncDestination(name="nas", host="nas.local", username="u", remote_path="/data")
    handler = RsyncHandler(dest, spool)
    sid = spool.receive("1.2.3.4.9")  # no files on disk
    task = _claim_task(spool, sid, dest)
    result = handler.deliver(task, spool.spool_dir)
    assert result.ok is False
    mock_run.assert_not_called()


@patch("subprocess.run")
def test_rsync_through_forwarder(mock_run, tmp_path: Path, spool: Spool) -> None:
    from mercure_gateway.forwarder.handlers.rsync import RsyncHandler

    mock_run.return_value = MagicMock(returncode=0)
    dest = RsyncDestination(name="nas", host="nas.local", username="u", remote_path="/data")
    fwd = Forwarder(default_config(), spool, retry=RetryPolicy(max_attempts=1))
    fwd.register_handler("rsync", RsyncHandler(dest, spool))
    sid = _write_study(spool)
    spool.enqueue(sid, [dest])
    fwd.process_once()
    assert spool.state(sid) == StudyState.SENT
