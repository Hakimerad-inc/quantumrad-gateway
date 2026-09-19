"""At-rest encryption for ``mercure-gateway.json`` (review H3).

The config model already declares ``credentials.encrypted`` (default ``True``) and
a ``credentials.entries`` map of per-destination :class:`CredentialEntry` blocks,
but ``save_config``/``load_config`` never used them — destination passwords, the
hub API key, and the web UI password hash were written to disk in cleartext.
This module closes that gap.

Secrets are encrypted with :class:`CredentialVault` (AES-256-GCM, PBKDF2 100k)
into ``credentials.entries``.  When an OS keyring backend is available the
decrypted secrets are also mirrored into it (per ``keyring_store`` semantics);
the encrypted-config vault remains the portable, air-gapped fallback.  On disk, a
secret field that has been encrypted is replaced by a non-secret placeholder so
the serialized JSON contains no cleartext credential.

The master password is sourced from the environment (``MERCURE_MASTER_PASSWORD``
or ``MERCURE_MASTER_PASSWORD_FILE``) so it never has to live in the config file.
"""

from __future__ import annotations

import os

from mercure_gateway.config import ENC_PLACEHOLDER, CredentialEntry, GatewayConfig
from mercure_gateway.credentials import CredentialVault, WrongPasswordError
from mercure_gateway.keyring_store import KeyringCredentialStore

__all__ = [
    "ConfigEncryptionError",
    "ENC_PLACEHOLDER",
    "decrypt_config_from_storage",
    "encrypt_config_for_storage",
    "load_master_password",
]

# Per-destination-type map of (model field, CredentialEntry encrypted slot).
_SECRET_FIELDS: dict[str, list[tuple[str, str]]] = {
    "sftp": [
        ("password", "password"),
        ("private_key", "private_key"),
        ("passphrase", "passphrase"),
    ],
    "s3": [("secret_access_key", "api_key")],
    "dicomweb": [("auth_token", "api_key")],
    "xnat": [("password", "password")],
}

_HUB_ENTRY = "__hub_reporting__"
_WEBUI_ENTRY = "__web_ui__"


class ConfigEncryptionError(Exception):
    """Raised when an encrypted config cannot be read without its master password."""


def load_master_password() -> str | None:
    """Resolve the config master password from the environment.

    Reads ``MERCURE_MASTER_PASSWORD`` directly, or ``MERCURE_MASTER_PASSWORD_FILE``
    (path to a file containing the password).  Returns ``None`` when neither is set,
    in which case config secrets are stored in cleartext (dev/test behaviour).
    """
    direct = os.environ.get("MERCURE_MASTER_PASSWORD")
    if direct:
        return direct
    path = os.environ.get("MERCURE_MASTER_PASSWORD_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return None
    return None


def _slot_attr(slot: str) -> str:
    return f"{slot}_encrypted"


def encrypt_config_for_storage(config: GatewayConfig, master_password: str) -> GatewayConfig:
    """Return a deep copy of *config* with secrets encrypted for on-disk storage.

    Secret fields are moved into ``credentials.entries`` as AES-256-GCM blocks and
    replaced on the model by :data:`ENC_PLACEHOLDER`.  The caller's *config* is
    never mutated.
    """
    copy = config.model_copy(deep=True)
    vault = CredentialVault(master_password, salt_b64=copy.credentials.salt)
    store = KeyringCredentialStore(vault)
    copy.credentials.salt = vault.salt_b64
    copy.credentials.encrypted = True
    entries: dict[str, CredentialEntry] = {}

    for dest in copy.destinations:
        mapping = _SECRET_FIELDS.get(dest.type, [])
        if not mapping:
            continue
        entry = CredentialEntry(type=dest.type)
        entry.username = getattr(dest, "username", None)
        dirty = False
        for field, slot in mapping:
            value = getattr(dest, field)
            if not value:
                continue
            setattr(entry, _slot_attr(slot), vault.encrypt_field(value))
            if store.available:
                store.set_password(f"{dest.name}:{field}", value)
            setattr(dest, field, ENC_PLACEHOLDER)
            dirty = True
        if dirty:
            entries[dest.name] = entry

    if copy.audit.hub_reporting.api_key:
        entries[_HUB_ENTRY] = CredentialEntry(
            type="hub",
            api_key_encrypted=vault.encrypt_field(copy.audit.hub_reporting.api_key),
        )
        if store.available:
            store.set_password(f"{_HUB_ENTRY}:api_key", copy.audit.hub_reporting.api_key)
        copy.audit.hub_reporting.api_key = ENC_PLACEHOLDER

    if copy.web_ui.auth_password_hash:
        entries[_WEBUI_ENTRY] = CredentialEntry(
            type="webui",
            api_key_encrypted=vault.encrypt_field(copy.web_ui.auth_password_hash),
        )
        if store.available:
            store.set_password(f"{_WEBUI_ENTRY}:api_key", copy.web_ui.auth_password_hash)
        copy.web_ui.auth_password_hash = ENC_PLACEHOLDER

    copy.credentials.entries = entries
    return copy


def decrypt_config_from_storage(
    config: GatewayConfig, master_password: str | None
) -> GatewayConfig:
    """Return a copy of *config* with encrypted secrets restored to plaintext.

    No-ops (returns *config* unchanged) when the config is not encrypted or has
    no stored entries/salt.  Raises :class:`ConfigEncryptionError` only when the
    config is genuinely encrypted (entries present) yet no master password is
    available — i.e. it cannot be read back.
    """
    if not config.credentials.encrypted:
        return config
    if not config.credentials.entries and not config.credentials.salt:
        return config
    if not master_password:
        raise ConfigEncryptionError(
            "configuration is encrypted but no master password was provided; "
            "set MERCURE_MASTER_PASSWORD or MERCURE_MASTER_PASSWORD_FILE"
        )
    vault = CredentialVault(master_password, salt_b64=config.credentials.salt)
    store = KeyringCredentialStore(vault)
    copy = config.model_copy(deep=True)

    for name, entry in list(copy.credentials.entries.items()):
        if name == _HUB_ENTRY:
            if entry.api_key_encrypted:
                copy.audit.hub_reporting.api_key = _resolve_secret(
                    store, f"{name}:api_key", entry.api_key_encrypted, vault
                )
            continue
        if name == _WEBUI_ENTRY:
            if entry.api_key_encrypted:
                copy.web_ui.auth_password_hash = _resolve_secret(
                    store, f"{name}:api_key", entry.api_key_encrypted, vault
                )
            continue
        dest = next((d for d in copy.destinations if d.name == name), None)
        if dest is None:
            continue
        for field, slot in _SECRET_FIELDS.get(entry.type or dest.type, []):
            enc = getattr(entry, _slot_attr(slot))
            if enc:
                setattr(
                    dest,
                    field,
                    _resolve_secret(store, f"{name}:{field}", enc, vault),
                )
    return copy


def _resolve_secret(
    store: KeyringCredentialStore, key: str, encrypted: str, vault: CredentialVault
) -> str:
    """Prefer the OS keyring; fall back to decrypting the config vault block."""
    if store.available:
        from_keyring = store.get_password(key)
        if from_keyring is not None:
            return from_keyring
    try:
        return vault.decrypt_field(encrypted)
    except WrongPasswordError as exc:
        raise ConfigEncryptionError(
            "invalid master password for encrypted configuration"
        ) from exc
