"""S07-T4 (RED): XNAT handler — REST upload to XNAT server.

The XNAT handler uploads study files to an XNAT project via REST API.
``requests`` (or ``httpx``) is mocked for unit tests.

Behaviors:
1. Files are uploaded to the configured XNAT project/experiment
2. Non-200 HTTP response → DeliveryResult(ok=False)
3. Connection error → DeliveryResult(ok=False)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from mercure_gateway.config import XNATDestination, default_config
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


def _claim_task(spool: Spool, sid: int, dest: XNATDestination) -> object:
    spool.enqueue(sid, [dest])
    return spool.claim_next(limit=1)[0]


@patch("requests.Session")
def test_xnat_uploads_files(mock_session, tmp_path: Path, spool: Spool) -> None:
    from mercure_gateway.forwarder.handlers.xnat import XNATHandler

    sess = MagicMock()
    mock_session.return_value = sess
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    # POST for creating the experiment, PUT for each file upload
    sess.post.return_value = resp
    sess.put.return_value = resp

    dest = XNATDestination(
        name="xnat", url="https://xnat.local", username="u", password="p", project="PROJ"
    )
    handler = XNATHandler(dest, spool)
    sid = _write_study(spool)
    task = _claim_task(spool, sid, dest)
    result = handler.deliver(task, spool.spool_dir)
    assert result.ok is True
    assert sess.put.call_count >= 1


@patch("requests.Session")
def test_xnat_auth_failure(mock_session, tmp_path: Path, spool: Spool) -> None:
    from mercure_gateway.forwarder.handlers.xnat import XNATHandler

    sess = MagicMock()
    mock_session.return_value = sess
    resp = MagicMock()
    resp.ok = False
    resp.status_code = 401
    sess.put.return_value = resp

    dest = XNATDestination(
        name="xnat", url="https://xnat.local", username="u", password="p", project="PROJ"
    )
    handler = XNATHandler(dest, spool)
    sid = _write_study(spool)
    task = _claim_task(spool, sid, dest)
    result = handler.deliver(task, spool.spool_dir)
    assert result.ok is False


@patch("requests.Session")
def test_xnat_connection_error(mock_session, tmp_path: Path, spool: Spool) -> None:
    from mercure_gateway.forwarder.handlers.xnat import XNATHandler

    sess = MagicMock()
    mock_session.return_value = sess
    sess.put.side_effect = requests.exceptions.ConnectionError("no route to host")

    dest = XNATDestination(
        name="xnat", url="https://xnat.local", username="u", password="p", project="PROJ"
    )
    handler = XNATHandler(dest, spool)
    sid = _write_study(spool)
    task = _claim_task(spool, sid, dest)
    result = handler.deliver(task, spool.spool_dir)
    assert result.ok is False


@patch("requests.Session")
def test_xnat_through_forwarder(mock_session, tmp_path: Path, spool: Spool) -> None:
    from mercure_gateway.forwarder.handlers.xnat import XNATHandler

    sess = MagicMock()
    mock_session.return_value = sess
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    sess.post.return_value = resp
    sess.put.return_value = resp

    dest = XNATDestination(
        name="xnat", url="https://xnat.local", username="u", password="p", project="PROJ"
    )
    fwd = Forwarder(default_config(), spool, retry=RetryPolicy(max_attempts=1))
    fwd.register_handler("xnat", XNATHandler(dest, spool))
    sid = _write_study(spool)
    spool.enqueue(sid, [dest])
    fwd.process_once()
    assert spool.state(sid) == StudyState.SENT