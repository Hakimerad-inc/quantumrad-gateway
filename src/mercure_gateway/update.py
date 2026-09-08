"""Auto-update (PRD §2.3, Q5, ADR-0006, S09-T4).

Implements the Tauri Updater flow decided in ADR-0006: fetch a signed update
manifest from a static endpoint, compare versions, and — with user consent —
download, verify and apply the update.  This module owns the **Python-side**
signature verification and lifecycle state; the actual binary swap is handled
by Tauri's updater plugin (``tauri.conf.json`` ``plugins.updater``).

Security model (ADR-0006): every update archive is signed with an Ed25519
key; a manifest without a signature, or one whose signature does not verify,
is **rejected** — a compromised update endpoint cannot push unsigned binaries.

Signature scheme
----------------
``signature`` in the manifest is a **base64-encoded, 64-byte raw Ed25519
signature over the raw bytes of the update archive** (the same bytes whose
SHA-256 is published as ``checksum_sha256``). Verification uses the public key
supplied to :class:`Updater`; the signing key never leaves the release process.
Tauri's own updater verifies independently against its embedded key — this
Python-side check is defence in depth, not the only gate.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Any

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

__all__ = ["UpdateManifest", "UpdateResult", "Updater"]

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SEC = 30

# Raw Ed25519 material sizes: 32-byte public key, 64-byte signature.
_ED25519_KEY_LEN = 32
_ED25519_SIG_LEN = 64


def _b64decode(value: str) -> bytes | None:
    """Decode base64 (standard or URL-safe, padding optional).

    Returns ``None`` instead of raising — callers treat an undecodable value as
    a rejected signature rather than an internal error.
    """
    cleaned = value.strip()
    padded = cleaned + "=" * (-len(cleaned) % 4)
    normalized = padded.translate(str.maketrans("-_", "+/"))
    try:
        return base64.b64decode(normalized, validate=True)
    except Exception:  # noqa: BLE001 — malformed input is a rejection, not a bug
        return None


def _load_public_key(value: str | bytes | Ed25519PublicKey | None) -> Ed25519PublicKey | None:
    """Parse an Ed25519 public key from PEM, raw base64/bytes, or a key object.

    ``None`` means "no trust anchor configured", which :meth:`Updater.verify_signature`
    treats as fail-closed. Raises ``ValueError`` for a *malformed* key so a bad
    deployment is loud rather than silently disabling the check.
    """
    if value is None:
        return None
    if isinstance(value, Ed25519PublicKey):
        return value
    if isinstance(value, bytes):
        raw = value
    else:
        text = value.strip()
        if not text:
            return None
        if "BEGIN" in text:
            loaded = load_pem_public_key(text.encode("utf-8"))
            if not isinstance(loaded, Ed25519PublicKey):
                raise ValueError("update public key is not an Ed25519 key")
            return loaded
        raw = _b64decode(text) or b""
    if len(raw) != _ED25519_KEY_LEN:
        raise ValueError(
            f"Ed25519 public key must be {_ED25519_KEY_LEN} bytes, got {len(raw)}"
        )
    return Ed25519PublicKey.from_public_bytes(raw)


@dataclass(frozen=True)
class UpdateManifest:
    """A published update (from ``latest.json``)."""

    version: str
    url: str
    signature: str
    checksum_sha256: str = ""


@dataclass
class UpdateResult:
    """Outcome of a check/apply/rollback operation."""

    available: bool = False
    manifest: UpdateManifest | None = None
    ok: bool = False
    error: str | None = None


class Updater:
    """Fetches, verifies and applies gateway updates.

    ``current_version`` tracks the running version.  ``pending_update`` holds
    a downloaded-but-not-yet-applied archive (staged by :meth:`apply_update`)
    so a restart can complete the swap.  ``rollback()`` clears it.
    """

    def __init__(
        self,
        *,
        update_url: str,
        current_version: str,
        timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
        public_key: str | bytes | Ed25519PublicKey | None = None,
    ) -> None:
        self.update_url = update_url.rstrip("/")
        self.current_version = current_version
        self.timeout_sec = timeout_sec
        # Trust anchor for update archives. None means nothing can be verified,
        # which fail-closes: every signature check returns False.
        self.public_key = _load_public_key(public_key)
        self.pending_update: UpdateManifest | None = None

    def check_update(self) -> UpdateResult:
        """Fetch the update manifest and report if a newer version exists.

        Returns ``available=True`` with the manifest when ``manifest.version``
        differs from the current version.  HTTP failures surface as an error
        result (never an exception — an offline check must not crash startup).
        """
        try:
            resp = requests.get(self.update_url, timeout=self.timeout_sec)
            if not resp.ok:
                return UpdateResult(error=f"update manifest HTTP {resp.status_code}")
            data: dict[str, Any] = resp.json()
        except Exception as exc:  # noqa: BLE001 — boundary: update check
            logger.warning("update check failed: %s", exc)
            return UpdateResult(error=str(exc))

        manifest = UpdateManifest(
            version=str(data.get("version", "")),
            url=str(data.get("url", "")),
            signature=str(data.get("signature", "")),
            checksum_sha256=str(data.get("checksum_sha256", "")),
        )
        if manifest.version == self.current_version:
            return UpdateResult(available=False)
        if not manifest.version:
            return UpdateResult(error="update manifest missing version")
        return UpdateResult(available=True, manifest=manifest)

    def verify_signature(self, data: str | bytes, signature: str) -> bool:
        """Verify a base64 Ed25519 *signature* over *data* (the archive bytes).

        Returns ``False`` rather than raising: this is a gate, and a malformed
        or mismatched signature is an attack signal, not an internal error.

        Fail-closed in three cases: an empty signature, no configured public
        key, or a signature that does not match. Before this was implemented
        the method compared the signature against the literal string
        ``"valid-signature"`` (review H2).
        """
        if not signature:
            return False
        if self.public_key is None:
            logger.error("update rejected: no Ed25519 public key configured")
            return False
        raw = _b64decode(signature)
        if raw is None or len(raw) != _ED25519_SIG_LEN:
            logger.warning(
                "update rejected: signature is not a %d-byte Ed25519 signature",
                _ED25519_SIG_LEN,
            )
            return False
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        try:
            self.public_key.verify(raw, payload)
        except InvalidSignature:
            logger.warning("update rejected: signature does not match the trusted key")
            return False
        except Exception:  # noqa: BLE001 — never let a crypto error mean "valid"
            logger.exception("update rejected: signature verification error")
            return False
        return True

    def apply_update(self, manifest: UpdateManifest) -> UpdateResult:
        """Download *manifest.url*, verify, and stage the update.

        The archive is downloaded and staged as :attr:`pending_update` so a
        restart (Tauri) can complete the swap.  A manifest without a
        signature, or a download failure, returns a failure result.
        """
        if not manifest.signature:
            return UpdateResult(ok=False, error="manifest missing signature — rejected")
        try:
            resp = requests.get(manifest.url, timeout=self.timeout_sec)
            if not resp.ok:
                return UpdateResult(ok=False, error=f"update download HTTP {resp.status_code}")
            content: bytes = resp.content
        except Exception as exc:  # noqa: BLE001 — boundary: update download
            logger.warning("update download failed: %s", exc)
            return UpdateResult(ok=False, error=str(exc))

        # Authenticity gate: only an archive signed by the trusted key is
        # staged. The checksum below proves integrity, not provenance — a
        # compromised endpoint can publish a matching hash for its own payload.
        if not self.verify_signature(content, manifest.signature):
            return UpdateResult(
                ok=False, error="update signature verification failed — rejected"
            )

        if manifest.checksum_sha256:
            digest = hashlib.sha256(content).hexdigest()
            if not hmac.compare_digest(digest, manifest.checksum_sha256.lower()):
                return UpdateResult(ok=False, error="update checksum mismatch")

        self.pending_update = manifest
        logger.info("update %s staged for apply", manifest.version)
        return UpdateResult(ok=True)

    def rollback(self) -> UpdateResult:
        """Clear a staged update; the running version is unchanged."""
        self.pending_update = None
        return UpdateResult(ok=True, available=False)
