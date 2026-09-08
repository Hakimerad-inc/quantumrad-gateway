"""SFTP handler — paramiko SSH file transfer (PRD §2.3 v1.1, S07-T3).

Satisfies the :class:`~mercure_gateway.forwarder.DestinationHandler` protocol.

Delivery uploads every DICOM file from the spooled study to the configured
remote directory via SFTP.  Credentials come from the destination's own
``password``/``private_key`` fields, or from the encrypted credential store
(:class:`~mercure_gateway.credentials.CredentialVault`) when those are unset.

The operation is a **copy** — spool files remain intact.

Security: the server's host key is verified against ``known_hosts`` (configured
per destination). Unknown keys are rejected, not auto-accepted — an operator
must pre-seed ``known_hosts`` (e.g. with ``ssh-keyscan``), otherwise every
connection fails closed (review H4).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from mercure_gateway.config import SFTPDestination
from mercure_gateway.forwarder import DeliveryResult
from mercure_gateway.spool import Spool

__all__ = ["SFTPHandler"]


class _CredentialEntry(Protocol):
    password_encrypted: str | None
    private_key_encrypted: str | None
    passphrase_encrypted: str | None


class _CredentialVault(Protocol):
    @property
    def entry(self) -> _CredentialEntry | None: ...
    def decrypt_field(self, encrypted: str) -> str: ...


class SFTPHandler:
    """Upload a study's DICOM files to a remote SFTP directory.

    Usage::

        handler = SFTPHandler(destination, spool)
        forwarder.register_handler("sftp", handler)
    """

    def __init__(
        self,
        destination: SFTPDestination,
        spool: Spool,
        vault: _CredentialVault | None = None,
    ) -> None:
        self.destination = destination
        self.spool = spool
        self._vault = vault  # optional CredentialVault for decryption

    def deliver(self, task: object, spool_dir: Path) -> DeliveryResult:
        """Upload the study's DICOM files to the SFTP destination."""
        from mercure_gateway.spool import ClaimedTask

        assert isinstance(task, ClaimedTask)
        try:
            study_uid = self.spool.study_uid(task.study_id)
        except KeyError:
            return DeliveryResult(ok=False, error="study not found")

        files = self.spool.study_files(study_uid)
        if not files:
            return DeliveryResult(ok=False, error="no DICOM files found for study")

        try:
            import paramiko  # type: ignore[import-untyped]
        except ImportError:  # pragma: no cover — paramiko is a runtime dep
            return DeliveryResult(ok=False, error="paramiko not installed")

        password, private_key, passphrase = self._resolve_credentials()

        client = paramiko.SSHClient()
        # Reject unknown host keys instead of blindly trusting the first
        # connection (the old AutoAddPolicy let a MITM impersonate the PACS,
        # review H4). Keys are taken from a configured known_hosts file; with
        # no file configured, nothing is trusted and connections fail closed.
        known_hosts = self.destination.known_hosts
        if known_hosts:
            known_hosts_path = Path(known_hosts)
            if not known_hosts_path.exists():
                known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
                known_hosts_path.touch()
            try:
                client.load_host_keys(str(known_hosts_path))
            except (OSError, ValueError) as exc:
                return DeliveryResult(
                    ok=False, error=f"could not read known_hosts file: {exc}"
                )
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            if private_key:
                key = self._load_private_key(private_key, passphrase)
                client.connect(
                    hostname=self.destination.host,
                    port=self.destination.port,
                    username=self.destination.username,
                    pkey=key,
                )
            else:
                client.connect(
                    hostname=self.destination.host,
                    port=self.destination.port,
                    username=self.destination.username,
                    password=password,
                )
            sftp = client.open_sftp()
            try:
                for f in files:
                    remote = f"{self.destination.remote_path.rstrip('/')}/{f.name}"
                    sftp.put(str(f), remote)
            finally:
                sftp.close()
        except Exception as exc:  # noqa: BLE001 — boundary: map to DeliveryResult
            return DeliveryResult(ok=False, error=str(exc))
        finally:
            client.close()

        return DeliveryResult(ok=True)

    # -- credential resolution ------------------------------------------

    def _resolve_credentials(self) -> tuple[str | None, str | None, str | None]:
        """Return ``(password, private_key, passphrase)`` for the destination.

        Prefers the destination's own plaintext fields; falls back to the
        encrypted credential store (via ``vault``) keyed by destination name.
        """
        password = self.destination.password
        private_key = self.destination.private_key
        passphrase = self.destination.passphrase
        if self._vault is not None and password is None and private_key is None:
            entry = getattr(self._vault, "entry", None)
            if entry is not None:
                if entry.password_encrypted:
                    password = self._vault.decrypt_field(entry.password_encrypted)
                if entry.private_key_encrypted:
                    private_key = self._vault.decrypt_field(entry.private_key_encrypted)
                if entry.passphrase_encrypted:
                    passphrase = self._vault.decrypt_field(entry.passphrase_encrypted)
        return password, private_key, passphrase

    @staticmethod
    def _load_private_key(private_key: str, passphrase: str | None) -> Any:
        """Load an RSA/ECDSA/Ed25519 key from its PEM string."""
        import io

        import paramiko

        for cls in (paramiko.RSAKey, paramiko.ECDSAKey, paramiko.Ed25519Key):
            try:
                return cls.from_private_key(io.StringIO(private_key), password=passphrase)
            except paramiko.ssh_exception.SSHException:
                continue
        raise ValueError("unable to parse private key (supported: RSA, ECDSA, Ed25519)")
