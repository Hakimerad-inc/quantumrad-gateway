"""S07-T3 (RED): SFTP handler — paramiko SSH file transfer with encrypted creds.

The SFTP handler delivers study files to a remote SFTP destination using
paramiko.  Credentials are read from the SFTPDestination config fields
(password, private_key, passphrase) or, when those are None, looked up in the
encrypted credential store via the ``CredentialVault``.

Behaviors:
1. Study files are uploaded to the configured remote_path
2. Missing auth credentials → DeliveryResult(ok=False, error)
3. AuthException → DeliveryResult(ok=False, error)
4. SSHException → DeliveryResult(ok=False, error)
5. Files are copied (not moved); spool originals remain
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from paramiko import AuthenticationException, SSHException

from mercure_gateway.config import SFTPDestination, default_config
from mercure_gateway.forwarder import Forwarder, RetryPolicy
from mercure_gateway.forwarder.handlers.sftp import SFTPHandler
from mercure_gateway.spool import Spool, StudyState
from mercure_gateway.spool.db import mem_database

pytest.importorskip("paramiko")


@pytest.fixture()
def spool(tmp_path: Path) -> Spool:
    cfg = default_config()
    cfg.storage.spool_dir = str(tmp_path / "spool")
    s = Spool(mem_database(), cfg)
    (tmp_path / "spool").mkdir(parents=True, exist_ok=True)
    return s


def _write_study(spool: Spool, study_uid: str = "1.2.3.4.1") -> int:
    study_id = spool.receive(study_uid)
    d = spool.spool_dir / study_uid / "1.2.3.4.5"
    d.mkdir(parents=True, exist_ok=True)
    (d / "1.dcm").write_bytes(b"dicom-data")
    return study_id


def _claim_task(spool: Spool, study_id: int, dest: SFTPDestination) -> object:
    spool.enqueue(study_id, [dest])
    return spool.claim_next(limit=1)[0]


# ══════════════════════════════════════════════════════════════════════
# Successful delivery
# ══════════════════════════════════════════════════════════════════════

@patch("paramiko.SSHClient")
def test_sftp_delivery_uploads_files(mock_ssh, tmp_path: Path, spool: Spool) -> None:
    from mercure_gateway.forwarder.handlers.sftp import SFTPHandler

    mock_client = MagicMock()
    mock_sftp = MagicMock()
    mock_ssh.return_value = mock_client
    mock_client.open_sftp.return_value = mock_sftp

    study_id = _write_study(spool)
    dest = SFTPDestination(
        name="nas", type="sftp", host="nas.local", port=22, username="u", password="pw"
    )
    handler = SFTPHandler(dest, spool)
    task = _claim_task(spool, study_id, dest)

    result = handler.deliver(task, spool.spool_dir)

    assert result.ok is True
    mock_client.connect.assert_called_once()
    # The remote path should be used
    assert mock_sftp.put.call_count >= 1


# ══════════════════════════════════════════════════════════════════════
# Auth failures
# ══════════════════════════════════════════════════════════════════════

@patch("paramiko.SSHClient")
def test_sftp_auth_failure(mock_ssh, tmp_path: Path, spool: Spool) -> None:
    from mercure_gateway.forwarder.handlers.sftp import SFTPHandler

    mock_client = MagicMock()
    mock_ssh.return_value = mock_client
    mock_client.connect.side_effect = AuthenticationException("bad password")

    study_id = _write_study(spool)
    dest = SFTPDestination(
        name="nas", type="sftp", host="nas.local", port=22, username="u", password="wrong"
    )
    handler = SFTPHandler(dest, spool)
    task = _claim_task(spool, study_id, dest)

    result = handler.deliver(task, spool.spool_dir)

    assert result.ok is False
    assert "bad password" in (result.error or "")


@patch("paramiko.SSHClient")
def test_sftp_ssh_exception(mock_ssh, tmp_path: Path, spool: Spool) -> None:
    from mercure_gateway.forwarder.handlers.sftp import SFTPHandler

    mock_client = MagicMock()
    mock_ssh.return_value = mock_client
    mock_client.connect.side_effect = SSHException("no route to host")

    study_id = _write_study(spool)
    dest = SFTPDestination(
        name="nas", type="sftp", host="10.0.0.99", port=22, username="u", password="pw"
    )
    handler = SFTPHandler(dest, spool)
    task = _claim_task(spool, study_id, dest)

    result = handler.deliver(task, spool.spool_dir)

    assert result.ok is False
    assert "no route to host" in (result.error or "")


# ══════════════════════════════════════════════════════════════════════
# Copy semantics (spool files remain)
# ══════════════════════════════════════════════════════════════════════

@patch("paramiko.SSHClient")
def test_sftp_is_copy_not_move(mock_ssh, tmp_path: Path, spool: Spool) -> None:
    from mercure_gateway.forwarder.handlers.sftp import SFTPHandler

    mock_client = MagicMock()
    mock_sftp = MagicMock()
    mock_ssh.return_value = mock_client
    mock_client.open_sftp.return_value = mock_sftp

    study_id = _write_study(spool)
    dest = SFTPDestination(
        name="nas", type="sftp", host="nas.local", port=22, username="u", password="pw"
    )
    handler = SFTPHandler(dest, spool)
    task = _claim_task(spool, study_id, dest)

    result = handler.deliver(task, spool.spool_dir)

    assert result.ok is True
    # Spool files must still be present
    assert spool.study_files("1.2.3.4.1")


# ══════════════════════════════════════════════════════════════════════
# End-to-end through the forwarder
# ══════════════════════════════════════════════════════════════════════

@patch("paramiko.SSHClient")
def test_sftp_handler_through_forwarder(mock_ssh, tmp_path: Path, spool: Spool) -> None:
    from mercure_gateway.forwarder.handlers.sftp import SFTPHandler

    mock_client = MagicMock()
    mock_sftp = MagicMock()
    mock_ssh.return_value = mock_client
    mock_client.open_sftp.return_value = mock_sftp

    study_id = _write_study(spool)
    dest = SFTPDestination(
        name="nas", type="sftp", host="nas.local", port=22, username="u", password="pw"
    )
    fwd = Forwarder(default_config(), spool, retry=RetryPolicy(max_attempts=1))
    fwd.register_handler("sftp", SFTPHandler(dest, spool))

    spool.enqueue(study_id, [dest])
    fwd.process_once()

    assert spool.state(study_id) == StudyState.SENT

# ══════════════════════════════════════════════════════════════════════
# Host key verification (review H4)
# ══════════════════════════════════════════════════════════════════════

@patch("paramiko.SSHClient")
def test_sftp_no_longer_auto_accepts_host_keys(mock_ssh, tmp_path: Path, spool: Spool) -> None:
    """The AutoAddPolicy (blind trust) must be gone, replaced by RejectPolicy."""
    import paramiko

    mock_client = MagicMock()
    mock_ssh.return_value = mock_client
    mock_client.open_sftp.return_value = MagicMock()

    study_id = _write_study(spool)
    dest = SFTPDestination(
        name="nas", type="sftp", host="nas.local", port=22, username="u", password="pw"
    )
    task = _claim_task(spool, study_id, dest)
    result = SFTPHandler(dest, spool).deliver(task, spool.spool_dir)

    assert result.ok is True
    mock_client.set_missing_host_key_policy.assert_called_once()
    policy = mock_client.set_missing_host_key_policy.call_args[0][0]
    assert isinstance(policy, paramiko.RejectPolicy)
    assert not isinstance(policy, paramiko.AutoAddPolicy)


@patch("paramiko.SSHClient")
def test_sftp_loads_configured_known_hosts(mock_ssh, tmp_path: Path, spool: Spool) -> None:
    """A configured known_hosts file is loaded for verification."""
    mock_client = MagicMock()
    mock_ssh.return_value = mock_client
    mock_client.open_sftp.return_value = MagicMock()

    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("nas.local ssh-ed25519 AAAAC3NzaC1lZDI1NTE5example\n")

    study_id = _write_study(spool)
    dest = SFTPDestination(
        name="nas", type="sftp", host="nas.local", port=22, username="u",
        password="pw", known_hosts=str(known_hosts),
    )
    task = _claim_task(spool, study_id, dest)
    result = SFTPHandler(dest, spool).deliver(task, spool.spool_dir)

    assert result.ok is True
    mock_client.load_host_keys.assert_called_once_with(str(known_hosts))


@patch("paramiko.SSHClient")
def test_sftp_creates_missing_known_hosts_file(mock_ssh, tmp_path: Path, spool: Spool) -> None:
    """A configured but absent known_hosts file is created (then rejects)."""
    mock_client = MagicMock()
    mock_ssh.return_value = mock_client
    mock_client.open_sftp.return_value = MagicMock()

    known_hosts = tmp_path / "nested" / "known_hosts"

    study_id = _write_study(spool)
    dest = SFTPDestination(
        name="nas", type="sftp", host="nas.local", port=22, username="u",
        password="pw", known_hosts=str(known_hosts),
    )
    task = _claim_task(spool, study_id, dest)
    result = SFTPHandler(dest, spool).deliver(task, spool.spool_dir)

    assert known_hosts.exists()
    assert result.ok is True


@patch("paramiko.SSHClient")
def test_sftp_rejects_unknown_host_key(mock_ssh, tmp_path: Path, spool: Spool) -> None:
    """With no trusted keys, a connection is rejected rather than open (H4)."""
    from paramiko.ssh_exception import SSHException

    mock_client = MagicMock()
    mock_ssh.return_value = mock_client
    # RejectPolicy raises this when the host key is not in known_hosts.
    mock_client.connect.side_effect = SSHException(
        "Server 'nas.local' not found in known_hosts"
    )

    study_id = _write_study(spool)
    dest = SFTPDestination(
        name="nas", type="sftp", host="nas.local", port=22, username="u", password="pw"
    )
    task = _claim_task(spool, study_id, dest)
    result = SFTPHandler(dest, spool).deliver(task, spool.spool_dir)

    assert result.ok is False
    assert "known_hosts" in (result.error or "").lower()
