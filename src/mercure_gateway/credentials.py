"""Encrypted credential storage for air-gapped sites (refinement spec §2.5).

Credentials are stored in ``mercure-gateway.json`` as AES-256-GCM encrypted
blocks.  The master password derives the 256-bit key via PBKDF2-HMAC-SHA256
(100,000 iterations).  Credentials are decrypted **in memory only**; the
serialized config never contains plaintext secrets.

Format: each encrypted value is ``AES256GCM:<base64(nonce | ciphertext | tag)>``
with a fresh 96-bit random nonce per encryption, so identical plaintexts
produce different ciphertexts.  Decryption fails with :class:`WrongPasswordError`
when the GCM authentication tag does not verify (i.e. the master password was
wrong or the block was tampered with).

OS keyring integration is deferred to v1.1 (Sprint 07); this portable file
approach works on air-gapped machines where a keyring may be unavailable.
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = [
    "PBKDF2_ITERATIONS",
    "CredentialVault",
    "WrongPasswordError",
    "decrypt_secret",
    "derive_key",
    "encrypt_secret",
]

# Refinement §2.5: PBKDF2 with 100k iterations, SHA-256.
PBKDF2_ITERATIONS = 100_000

_PREFIX = "AES256GCM:"
_SALT_BYTES = 16
_NONCE_BYTES = 12


class WrongPasswordError(ValueError):
    """Raised when a secret cannot be decrypted with the given master password."""


def derive_key(master_password: str, salt: bytes) -> bytes:
    """Derive a 256-bit AES key from *master_password* via PBKDF2-HMAC-SHA256."""
    from hashlib import pbkdf2_hmac

    return pbkdf2_hmac(
        "sha256",
        master_password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
        dklen=32,
    )


def encrypt_secret(plaintext: str, key: bytes) -> str:
    """Encrypt *plaintext* with AES-256-GCM and return ``AES256GCM:...``."""
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return _PREFIX + base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt_secret(encrypted: str, key: bytes) -> str:
    """Decrypt an ``AES256GCM:...`` value; raise :class:`WrongPasswordError` on failure."""
    if not encrypted.startswith(_PREFIX):
        raise WrongPasswordError("not an AES256GCM credential block")
    raw = base64.b64decode(encrypted[len(_PREFIX):])
    nonce, ciphertext = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
    except Exception:
        raise WrongPasswordError("invalid master password") from None
    return plaintext.decode("utf-8")


class CredentialVault:
    """Encrypts/decrypts credential blocks under a master password.

    Typical usage::

        vault = CredentialVault(master_password)          # fresh salt
        entry = CredentialEntry(type="sftp",
                                password_encrypted=vault.encrypt_field("pw"))
        cfg.credentials.salt = vault.salt_b64             # persist the salt
        cfg.credentials.entries["hub"] = entry
        save_config(cfg, path)

        # On the next start, unlock with the same password + stored salt:
        vault = CredentialVault(master_password, salt_b64=cfg.credentials.salt)
        secret = vault.decrypt_field(entry.password_encrypted)
    """

    def __init__(
        self,
        master_password: str,
        salt_b64: str | None = None,
        salt: bytes | None = None,
    ) -> None:
        if salt_b64 is not None:
            self._salt = base64.b64decode(salt_b64)
        elif salt is not None:
            self._salt = salt
        else:
            self._salt = os.urandom(_SALT_BYTES)
        self._key = derive_key(master_password, self._salt)

    @property
    def salt(self) -> bytes:
        """The raw PBKDF2 salt bytes."""
        return self._salt

    @property
    def salt_b64(self) -> str:
        """The salt as base64, for persistence in ``credentials.salt``."""
        return base64.b64encode(self._salt).decode("ascii")

    def encrypt_field(self, plaintext: str) -> str:
        """Encrypt one credential value (password/key/token) as ``AES256GCM:...``."""
        return encrypt_secret(plaintext, self._key)

    def decrypt_field(self, encrypted: str) -> str:
        """Decrypt one credential block; raise :class:`WrongPasswordError` on failure."""
        return decrypt_secret(encrypted, self._key)