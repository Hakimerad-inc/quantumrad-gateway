"""S07-T4 (RED): S3 handler — boto3 object-store delivery.

The S3 handler uploads study files to an S3 bucket using boto3.
Uses ``unittest.mock`` to stub boto3 (no moto needed for unit tests).

Behaviors:
1. Files are uploaded to the configured bucket/prefix
2. Auth failure (NoCredentialsError) → DeliveryResult(ok=False)
3. Files are copied (not moved)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mercure_gateway.config import S3Destination, default_config
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


def _claim_task(spool: Spool, sid: int, dest: S3Destination) -> object:
    spool.enqueue(sid, [dest])
    return spool.claim_next(limit=1)[0]


@patch("boto3.client")
def test_s3_uploads_files(mock_boto, tmp_path: Path, spool: Spool) -> None:
    from mercure_gateway.forwarder.handlers.s3 import S3Handler

    mock_s3 = MagicMock()
    mock_boto.return_value = mock_s3
    dest = S3Destination(name="s3", bucket="my-bucket", remote_prefix="studies/")
    handler = S3Handler(dest, spool)
    sid = _write_study(spool)
    task = _claim_task(spool, sid, dest)
    result = handler.deliver(task, spool.spool_dir)
    assert result.ok is True
    assert mock_s3.upload_file.call_count >= 1


@patch("boto3.client")
def test_s3_auth_failure(mock_boto, tmp_path: Path, spool: Spool) -> None:
    from botocore.exceptions import NoCredentialsError

    from mercure_gateway.forwarder.handlers.s3 import S3Handler

    mock_s3 = MagicMock()
    mock_s3.upload_file.side_effect = NoCredentialsError()
    mock_boto.return_value = mock_s3
    dest = S3Destination(name="s3", bucket="b", remote_prefix="p/")
    handler = S3Handler(dest, spool)
    sid = _write_study(spool)
    task = _claim_task(spool, sid, dest)
    result = handler.deliver(task, spool.spool_dir)
    assert result.ok is False


@patch("boto3.client")
def test_s3_is_copy(mock_boto, tmp_path: Path, spool: Spool) -> None:
    from mercure_gateway.forwarder.handlers.s3 import S3Handler

    mock_boto.return_value = MagicMock()
    dest = S3Destination(name="s3", bucket="b", remote_prefix="p/")
    handler = S3Handler(dest, spool)
    sid = _write_study(spool)
    task = _claim_task(spool, sid, dest)
    handler.deliver(task, spool.spool_dir)
    assert spool.study_files("1.2.3.4.1")


@patch("boto3.client")
def test_s3_through_forwarder(mock_boto, tmp_path: Path, spool: Spool) -> None:
    from mercure_gateway.forwarder.handlers.s3 import S3Handler

    mock_boto.return_value = MagicMock()
    dest = S3Destination(name="s3", bucket="b", remote_prefix="p/")
    fwd = Forwarder(default_config(), spool, retry=RetryPolicy(max_attempts=1))
    fwd.register_handler("s3", S3Handler(dest, spool))
    sid = _write_study(spool)
    spool.enqueue(sid, [dest])
    fwd.process_once()
    assert spool.state(sid) == StudyState.SENT