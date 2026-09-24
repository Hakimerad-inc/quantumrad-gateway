"""S07-T5 (RED): DICOMweb (STOW-RS) handler.

The DICOMweb handler sends study DICOM instances to a STOW-RS endpoint over
HTTPS using ``requests``.  Uses a fake HTTP server (or mocked ``requests``).

Behaviors:
1. Instances are STOW'd to the configured URL with multipart DICOM body
2. Non-200 response → DeliveryResult(ok=False)
3. Connection error → DeliveryResult(ok=False)
4. TLS is used when the URL is https
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mercure_gateway.config import DICOMwebDestination, default_config
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
    (d / "1.dcm").write_bytes(b"dicom-bytes")
    (d / "2.dcm").write_bytes(b"dicom-bytes-2")
    return sid


def _claim_task(spool: Spool, sid: int, dest: DICOMwebDestination) -> object:
    spool.enqueue(sid, [dest])
    return spool.claim_next(limit=1)[0]


@patch("requests.post")
def test_dicomweb_stow_uploads(mock_post, tmp_path: Path, spool: Spool) -> None:
    from mercure_gateway.forwarder.handlers.dicomweb import DICOMwebHandler

    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    mock_post.return_value = resp

    dest = DICOMwebDestination(name="web", url="https://pacs.local/dicomweb/studies")
    handler = DICOMwebHandler(dest, spool)
    sid = _write_study(spool)
    task = _claim_task(spool, sid, dest)
    result = handler.deliver(task, spool.spool_dir)

    assert result.ok is True
    # Should have sent at least one STOW-RS request with the multipart body
    assert mock_post.call_count >= 1
    kwargs = mock_post.call_args[1]
    assert kwargs["url"].startswith("https://")
    assert "data" in kwargs


@patch("requests.post")
def test_dicomweb_non_200_fails(mock_post, tmp_path: Path, spool: Spool) -> None:
    from mercure_gateway.forwarder.handlers.dicomweb import DICOMwebHandler

    resp = MagicMock()
    resp.ok = False
    resp.status_code = 500
    mock_post.return_value = resp

    dest = DICOMwebDestination(name="web", url="https://pacs.local/dicomweb/studies")
    handler = DICOMwebHandler(dest, spool)
    sid = _write_study(spool)
    task = _claim_task(spool, sid, dest)
    result = handler.deliver(task, spool.spool_dir)

    assert result.ok is False
    assert "500" in (result.error or "")


@patch("requests.post")
def test_dicomweb_connection_error(mock_post, tmp_path: Path, spool: Spool) -> None:
    from mercure_gateway.forwarder.handlers.dicomweb import DICOMwebHandler

    mock_post.side_effect = ConnectionError("no route to host")

    dest = DICOMwebDestination(name="web", url="https://pacs.local/dicomweb/studies")
    handler = DICOMwebHandler(dest, spool)
    sid = _write_study(spool)
    task = _claim_task(spool, sid, dest)
    result = handler.deliver(task, spool.spool_dir)

    assert result.ok is False


@patch("requests.post")
def test_dicomweb_sends_the_bearer_token_when_configured(
    mock_post, tmp_path: Path, spool: Spool
) -> None:
    """A cloud PACS behind an authenticating proxy needs the token on the wire.

    ``auth_token`` is declared on the destination, encrypted at rest, restored
    on boot, and redacted from the API — so a operator who sets it reasonably
    expects it to reach the server.  This fails today: the request carried only
    a Content-Type header, so every instance came back 401 and the study
    exhausted its retry budget with no field left to try.
    """
    from mercure_gateway.forwarder.handlers.dicomweb import DICOMwebHandler

    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    mock_post.return_value = resp

    dest = DICOMwebDestination(
        name="cloud", url="https://pacs.cloud/studies", auth_token="sek-123"
    )
    handler = DICOMwebHandler(dest, spool)
    sid = _write_study(spool)
    task = _claim_task(spool, sid, dest)
    result = handler.deliver(task, spool.spool_dir)

    assert result.ok is True
    headers = mock_post.call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer sek-123"


@patch("requests.post")
def test_dicomweb_sends_the_ae_title_when_configured(
    mock_post, tmp_path: Path, spool: Spool
) -> None:
    """The AE title travels as an X-AE-Title header.

    DICOMweb defines no on-the-wire AE title, so this is a convention rather
    than a spec — some cloud PACS use it to attribute the upload to a modality.
    It is only sent when set, and only alongside the studies endpoint.
    """
    from mercure_gateway.forwarder.handlers.dicomweb import DICOMwebHandler

    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    mock_post.return_value = resp

    dest = DICOMwebDestination(
        name="cloud", url="https://pacs.cloud/studies", aet="GATEWAY"
    )
    handler = DICOMwebHandler(dest, spool)
    sid = _write_study(spool)
    task = _claim_task(spool, sid, dest)
    result = handler.deliver(task, spool.spool_dir)

    assert result.ok is True
    headers = mock_post.call_args[1]["headers"]
    assert headers["X-AE-Title"] == "GATEWAY"


@patch("requests.post")
def test_dicomweb_omits_authorization_when_no_token(
    mock_post, tmp_path: Path, spool: Spool
) -> None:
    """An unauthenticated endpoint must not receive a header it may reject.

    Some STOW-RS servers are strict about unexpected Authorization headers
    (a mismatched scheme can be a hard reject), so the default is to send none.
    """
    from mercure_gateway.forwarder.handlers.dicomweb import DICOMwebHandler

    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    mock_post.return_value = resp

    dest = DICOMwebDestination(name="web", url="https://pacs.local/studies")
    handler = DICOMwebHandler(dest, spool)
    sid = _write_study(spool)
    task = _claim_task(spool, sid, dest)
    result = handler.deliver(task, spool.spool_dir)

    assert result.ok is True
    headers = mock_post.call_args[1]["headers"]
    assert "Authorization" not in headers


@patch("requests.post")
def test_dicomweb_through_forwarder(mock_post, tmp_path: Path, spool: Spool) -> None:
    from mercure_gateway.forwarder.handlers.dicomweb import DICOMwebHandler

    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    mock_post.return_value = resp

    dest = DICOMwebDestination(name="web", url="https://pacs.local/dicomweb/studies")
    fwd = Forwarder(default_config(), spool, retry=RetryPolicy(max_attempts=1))
    fwd.register_handler("dicomweb", DICOMwebHandler(dest, spool))
    sid = _write_study(spool)
    spool.enqueue(sid, [dest])
    fwd.process_once()

    assert spool.state(sid) == StudyState.SENT
