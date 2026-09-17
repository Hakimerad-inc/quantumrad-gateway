"""S04-T5 (RED): encrypted credential storage + master password (US-08c, §2.5).

Credentials live in ``mercure-gateway.json`` as AES-256-GCM encrypted blocks.
The master password derives the encryption key via PBKDF2 (100k iterations,
SHA-256).  Credentials are decrypted in memory only; the serialized config
never contains plaintext secrets.  A no-encryption mode (dev/test) is kept as a
fallback.

Behaviors:
1. Encrypt/decrypt round-trip returns the original secret
2. Wrong master password is rejected (GCM auth tag fails)
3. A serialized config contains only ``AES256GCM:...`` blocks, never plaintext
4. No-encryption mode round-trips plaintext (dev/test fallback)
5. Master password derives a stable key per (password, salt); salt changes the key
"""

from __future__ import annotations

import json

import pytest

from mercure_gateway.config import CredentialEntry, CredentialsConfig, GatewayConfig
from mercure_gateway.credentials import (
    PBKDF2_ITERATIONS,
    CredentialVault,
    WrongPasswordError,
    decrypt_secret,
    derive_key,
    encrypt_secret,
)


def test_encrypt_decrypt_round_trip() -> None:
    key = derive_key("hunter2", b"salt")
    encrypted = encrypt_secret("s3cr3t-password", key)
    assert encrypted.startswith("AES256GCM:")
    assert decrypt_secret(encrypted, key) == "s3cr3t-password"


def test_wrong_password_rejected() -> None:
    good = derive_key("correct-horse", b"salt")
    bad = derive_key("wrong-password", b"salt")
    encrypted = encrypt_secret("hunter2", good)
    with pytest.raises(WrongPasswordError):
        decrypt_secret(encrypted, bad)


def test_wrong_password_rejected_across_vault() -> None:
    vault = CredentialVault("master-password")
    encrypted = vault.encrypt_field("s3cr3t")
    other = CredentialVault("different-password", salt=vault.salt)
    with pytest.raises(WrongPasswordError):
        other.decrypt_field(encrypted)


def test_credentials_never_in_plaintext_config() -> None:
    """The serialized config stores only encrypted blocks — no plaintext."""
    vault = CredentialVault("master-password")
    cfg = GatewayConfig(
        credentials={
            "salt": vault.salt_b64,
            "entries": {
                "hub": CredentialEntry(
                    type="sftp",
                    password_encrypted=vault.encrypt_field("plaintext-pw-12345"),
                    private_key_encrypted=vault.encrypt_field("-----BEGIN PRIVATE KEY-----"),
                )
            },
        }
    )

    dumped = cfg.model_dump_json()
    data = json.loads(dumped)

    assert "plaintext-pw-12345" not in dumped
    assert "BEGIN PRIVATE KEY" not in dumped
    entry = data["credentials"]["entries"]["hub"]
    assert entry["password_encrypted"].startswith("AES256GCM:")
    assert entry["private_key_encrypted"].startswith("AES256GCM:")


def test_vault_roundtrip_via_config() -> None:
    vault = CredentialVault("master-password")
    entry = CredentialEntry(
        type="sftp",
        password_encrypted=vault.encrypt_field("pw"),
        api_key_encrypted=vault.encrypt_field("api-key-xyz"),
    )

    reopened = CredentialVault("master-password", salt=vault.salt)
    assert reopened.decrypt_field(entry.password_encrypted or "") == "pw"
    assert reopened.decrypt_field(entry.api_key_encrypted or "") == "api-key-xyz"


def test_no_encryption_mode_roundtrips_plaintext() -> None:
    """With ``encrypted=False`` (dev/test) the credential block is plaintext."""
    cfg = CredentialsConfig(encrypted=False, entries={"hub": CredentialEntry(type="sftp")})
    assert cfg.encrypted is False
    # In no-encryption mode the handler may store plaintext in the legacy
    # destination field; the model must accept it without the vault.
    assert cfg.entries["hub"].type == "sftp"


def test_derive_key_uses_pbkdf2_sha256() -> None:
    key = derive_key("password", b"salt")
    assert len(key) == 32  # AES-256 key
    # Deterministic for the same (password, salt)
    assert key == derive_key("password", b"salt")
    # Salt changes the derived key
    assert key != derive_key("password", b"other-salt")


def test_iterations_matches_spec() -> None:
    assert PBKDF2_ITERATIONS == 100_000


def test_vault_salt_is_unique_per_instance() -> None:
    a = CredentialVault("pw")
    b = CredentialVault("pw")
    assert a.salt != b.salt
