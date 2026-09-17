"""S09-T4 (RED): Auto-update implementation (PRD §2.3, Q5, ADR-0006).

Tauri Updater flow: fetch update manifest, verify Ed25519 signature,
download update, apply, and rollback on failure.  The Python side
handles signature verification and update lifecycle state.

Signature verification is real cryptography (review H2). These tests build an
actual Ed25519 keypair rather than asserting the old placeholder, which
compared the signature against the literal string ``"valid-signature"``.
"""

from __future__ import annotations

import base64
import hashlib
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from mercure_gateway.update import UpdateManifest, Updater, UpdateResult

# A per-session test keypair (Ed25519 keygen is cheap). The private key exists
# only so these tests can produce signatures the Updater should accept.
_PRIVATE_KEY = Ed25519PrivateKey.generate()
_PUBLIC_B64 = base64.b64encode(
    _PRIVATE_KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
).decode("ascii")


def sign(payload: bytes) -> str:
    """Return a base64 Ed25519 signature over *payload* (the manifest format)."""
    return base64.b64encode(_PRIVATE_KEY.sign(payload)).decode("ascii")


@pytest.fixture()
def updater() -> Updater:
    return Updater(
        update_url="https://updates.example.com/latest.json",
        current_version="1.0.0",
        public_key=_PUBLIC_B64,
    )


@pytest.fixture()
def archive() -> bytes:
    return b"mock-update-archive"


@pytest.fixture()
def valid_manifest(archive: bytes) -> UpdateManifest:
    return UpdateManifest(
        version="1.1.0",
        url="https://updates.example.com/releases/v1.1.0.tar.gz",
        signature=sign(archive),
        checksum_sha256=hashlib.sha256(archive).hexdigest(),
    )


# ══════════════════════════════════════════════════════════════════════
# Update detection
# ══════════════════════════════════════════════════════════════════════

@patch("requests.get")
def test_check_update_returns_available(
    mock_get: MagicMock, updater: Updater, valid_manifest: UpdateManifest
) -> None:
    """check_update() returns available version when newer than current."""
    resp = MagicMock()
    resp.ok = True
    resp.json.return_value = {
        "version": valid_manifest.version,
        "url": valid_manifest.url,
        "signature": valid_manifest.signature,
        "checksum_sha256": valid_manifest.checksum_sha256,
    }
    mock_get.return_value = resp

    result = updater.check_update()

    assert result.available is True
    assert result.manifest is not None
    assert result.manifest.version == "1.1.0"


@patch("requests.get")
def test_check_update_none_when_up_to_date(mock_get: MagicMock, updater: Updater) -> None:
    """check_update() returns not available when current version matches."""
    resp = MagicMock()
    resp.ok = True
    resp.json.return_value = {
        "version": "1.0.0",
        "url": "https://updates.example.com/releases/v1.0.0.tar.gz",
        "signature": "",
        "checksum_sha256": "",
    }
    mock_get.return_value = resp

    result = updater.check_update()

    assert result.available is False
    assert result.manifest is None


# ══════════════════════════════════════════════════════════════════════
# Downgrade protection (a differing version is not necessarily a newer one)
# ══════════════════════════════════════════════════════════════════════


def _check_with(manifest_version: str, current_version: str) -> UpdateResult:
    """Run check_update() with a manifest version against a running version."""
    with patch("requests.get") as mock_get:
        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = {
            "version": manifest_version,
            "url": f"https://updates.example.com/releases/{manifest_version}.tar.gz",
            "signature": "",
            "checksum_sha256": "",
        }
        mock_get.return_value = resp
        return Updater(
            update_url="https://updates.example.com/latest.json",
            current_version=current_version,
            public_key=_PUBLIC_B64,
        ).check_update()


@patch("requests.get")
def test_check_update_rejects_an_older_version(mock_get: MagicMock, updater: Updater) -> None:
    """An older signed release is not an update — a difference is not an upgrade.

    Previously any *differing* version was reported available, so a compromised
    endpoint could offer a validly-signed older, vulnerable archive as though it
    were newer (found while drafting the fleet key-compromise answer).
    """
    result = _check_with("0.9.0", "1.1.0")

    assert result.available is False
    assert result.manifest is None


def test_check_update_rejects_an_older_prerelease() -> None:
    """rc2 does not update a box already on rc3."""
    result = _check_with("1.1.0-rc2", "1.1.0-rc3")

    assert result.available is False


def test_check_update_offers_the_next_prerelease() -> None:
    """rc3 does update a box on rc2 — the gate blocks regressions only."""
    result = _check_with("1.1.0-rc3", "1.1.0-rc2")

    assert result.available is True
    assert result.manifest is not None
    assert result.manifest.version == "1.1.0-rc3"


def test_check_update_offers_the_release_over_its_own_prerelease() -> None:
    """The final 1.1.0 outranks 1.1.0-rc3 (PEP 440 pre-release ordering)."""
    result = _check_with("1.1.0", "1.1.0-rc3")

    assert result.available is True


def test_check_update_fails_closed_on_an_unparseable_version() -> None:
    """A version we cannot order is not offered as an update (fail-closed)."""
    result = _check_with("latest", "1.1.0-rc3")

    assert result.available is False
    assert result.error is not None


@patch("requests.get")
def test_check_update_http_failure_handled(mock_get: MagicMock, updater: Updater) -> None:
    """check_update() handles HTTP failure gracefully (no crash)."""
    resp = MagicMock()
    resp.ok = False
    resp.status_code = 500
    mock_get.return_value = resp

    result = updater.check_update()

    assert result.available is False
    assert result.error is not None


# ══════════════════════════════════════════════════════════════════════
# Signature verification — real Ed25519 (review H2)
# ══════════════════════════════════════════════════════════════════════

def test_verify_signature_accepts_a_real_signature(updater: Updater) -> None:
    """A genuine Ed25519 signature over the payload passes."""
    assert updater.verify_signature(b"payload", sign(b"payload")) is True


def test_verify_signature_rejects_tampered_payload(
    updater: Updater, archive: bytes
) -> None:
    """A valid signature over *other* bytes does not validate this payload."""
    assert updater.verify_signature(archive, sign(b"a-different-archive")) is False


def test_verify_signature_rejects_a_forged_key(archive: bytes) -> None:
    """A signature from an untrusted key is rejected (the whole point)."""
    impostor = Ed25519PrivateKey.generate()
    forged = impostor.sign(archive)
    import base64

    updater = Updater(
        update_url="https://updates.example.com/latest.json",
        current_version="1.0.0",
        public_key=_PUBLIC_B64,
    )

    assert updater.verify_signature(archive, base64.b64encode(forged).decode()) is False


def test_verify_signature_empty_rejected(updater: Updater) -> None:
    """An empty signature is always rejected."""
    assert updater.verify_signature("data", "") is False


def test_verify_signature_rejects_the_old_placeholder(updater: Updater) -> None:
    """Guard against regressing to the string-compare stub (review H2)."""
    assert updater.verify_signature("data", "valid-signature") is False


def test_verify_signature_rejects_garbage(updater: Updater) -> None:
    """Undecodable or wrong-length signatures are rejected, not crashed on."""
    assert updater.verify_signature("data", "not-base64!!") is False
    assert updater.verify_signature("data", "c2hvcnQ=") is False  # 5 bytes, not 64


def test_verify_signature_fails_closed_without_a_key() -> None:
    """No trust anchor configured ⇒ nothing can be verified ⇒ reject all."""
    updater = Updater(
        update_url="https://updates.example.com/latest.json",
        current_version="1.0.0",
    )
    assert updater.verify_signature("data", sign(b"data")) is False


def test_public_key_accepts_pem_and_raw_bytes() -> None:
    """Both PEM and raw 32-byte keys configure the same trust anchor."""
    pem = _PRIVATE_KEY.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    )
    payload = b"data"
    signature = sign(payload)

    assert Updater(
        update_url="u", current_version="1", public_key=pem.decode()
    ).verify_signature(payload, signature) is True
    assert Updater(
        update_url="u", current_version="1", public_key=_PUBLIC_B64
    ).verify_signature(payload, signature) is True


def test_malformed_public_key_is_loud() -> None:
    """A key of the wrong length is a deployment error, not a silent no-op."""
    with pytest.raises(ValueError):
        Updater(update_url="u", current_version="1", public_key="c2hvcnQ=")


# ══════════════════════════════════════════════════════════════════════
# Apply / rollback lifecycle
# ══════════════════════════════════════════════════════════════════════

@patch("requests.get")
def test_apply_update_downloads_and_verifies(
    mock_get: MagicMock, updater: Updater, valid_manifest: UpdateManifest, archive: bytes
) -> None:
    """apply_update() downloads the archive, verifies, and applies."""
    resp = MagicMock()
    resp.ok = True
    resp.content = archive
    mock_get.return_value = resp

    result = updater.apply_update(valid_manifest)

    assert result.ok is True
    assert updater.pending_update is valid_manifest  # staged for the restart swap


@patch("requests.get")
def test_apply_update_rejects_unsigned_archive(
    mock_get: MagicMock, updater: Updater, archive: bytes
) -> None:
    """A tampered archive whose signature does not match is never staged."""
    resp = MagicMock()
    resp.ok = True
    resp.content = archive
    mock_get.return_value = resp

    tampered = UpdateManifest(
        version="1.1.0",
        url="https://updates.example.com/releases/v1.1.0.tar.gz",
        signature=sign(b"some-other-archive"),
        checksum_sha256=hashlib.sha256(archive).hexdigest(),
    )

    result = updater.apply_update(tampered)

    assert result.ok is False
    assert "signature" in (result.error or "").lower()
    assert updater.pending_update is None


@patch("requests.get")
def test_apply_update_fails_on_download_error(
    mock_get: MagicMock, updater: Updater, valid_manifest: UpdateManifest
) -> None:
    """apply_update() returns failure when the download fails."""
    resp = MagicMock()
    resp.ok = False
    resp.status_code = 404
    mock_get.return_value = resp

    result = updater.apply_update(valid_manifest)

    assert result.ok is False
    assert result.error is not None


def test_apply_update_rejects_manifest_without_signature(
    updater: Updater, valid_manifest: UpdateManifest
) -> None:
    """apply_update() rejects a manifest with an empty signature."""
    unsigned = UpdateManifest(
        version=valid_manifest.version,
        url=valid_manifest.url,
        signature="",
        checksum_sha256=valid_manifest.checksum_sha256,
    )
    result = updater.apply_update(unsigned)

    assert result.ok is False
    assert "signature" in (result.error or "").lower()


def test_rollback_records_previous_version(updater: Updater) -> None:
    """rollback() restores the previous version record."""
    result = updater.rollback()

    assert result.ok is True
    assert updater.current_version == "1.0.0"
