"""S09-T4 (RED): Auto-update implementation (PRD §2.3, Q5, ADR-0006).

Tauri Updater flow: fetch update manifest, verify Ed25519 signature,
download update, apply, and rollback on failure.  The Python side
handles signature verification and update lifecycle state.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from mercure_gateway.update import UpdateManifest, Updater


@pytest.fixture()
def updater() -> Updater:
    return Updater(
        update_url="https://updates.example.com/latest.json",
        current_version="1.0.0",
    )


@pytest.fixture()
def valid_manifest() -> UpdateManifest:
    return UpdateManifest(
        version="1.1.0",
        url="https://updates.example.com/releases/v1.1.0.tar.gz",
        signature="dGVzdC1zaWduYXR1cmU=",  # base64 = "test-signature"
        checksum_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
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
# Signature verification
# ══════════════════════════════════════════════════════════════════════

def test_verify_signature_valid(updater: Updater) -> None:
    """A valid signature passes verification."""
    assert updater.verify_signature("data", "valid-signature") is True


def test_verify_signature_invalid(updater: Updater) -> None:
    """An invalid signature is rejected."""
    assert updater.verify_signature("data", "invalid-signature") is False


def test_verify_signature_empty_rejected(updater: Updater) -> None:
    """An empty signature is always rejected."""
    assert updater.verify_signature("data", "") is False


# ══════════════════════════════════════════════════════════════════════
# Apply / rollback lifecycle
# ══════════════════════════════════════════════════════════════════════

@patch("requests.get")
def test_apply_update_downloads_and_verifies(
    mock_get: MagicMock, updater: Updater, valid_manifest: UpdateManifest
) -> None:
    """apply_update() downloads the archive, verifies, and applies."""
    import hashlib

    content = b"mock-update-archive"
    resp = MagicMock()
    resp.ok = True
    resp.content = content
    mock_get.return_value = resp

    manifest = UpdateManifest(
        version=valid_manifest.version,
        url=valid_manifest.url,
        signature=valid_manifest.signature,
        checksum_sha256=hashlib.sha256(content).hexdigest(),
    )

    result = updater.apply_update(manifest)

    assert result.ok is True
    assert updater.pending_update is manifest  # staged for the restart swap


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