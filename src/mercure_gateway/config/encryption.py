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

import logging
import os
import secrets
from pathlib import Path

from mercure_gateway.config import ENC_PLACEHOLDER, CredentialEntry, GatewayConfig
from mercure_gateway.credentials import CredentialVault, WrongPasswordError
from mercure_gateway.keyring_store import KeyringCredentialStore

logger = logging.getLogger(__name__)

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

# The master password entry name in the OS keyring.
_KEYRING_ENTRY = "config-master-password"

# Set MERCURE_GATEWAY_ALLOW_PLAINTEXT_SECRETS=1 to opt out of encryption at
# rest entirely (dev, tests, a sealed read-only appliance). Not a secret.
_ALLOW_PLAINTEXT_ENV = "MERCURE_GATEWAY_ALLOW_PLAINTEXT_SECRETS"

# Fallback for a host with no usable keyring backend (headless, no DBus/secret
# service): a 0600 file beside the config. Worse than the keyring — it is
# recoverable by anyone who can read the appliance's filesystem, which is
# already game-over for the secrets in the config — still not cleartext.
_SIDECAR_SUFFIX = ".master-password"


class ConfigEncryptionError(Exception):
    """Raised when an encrypted config cannot be read without its master password."""


def _keyring_available() -> bool:
    """Whether an OS keyring backend is usable (mirrors KeyringCredentialStore)."""
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring

        return not isinstance(keyring.get_keyring(), FailKeyring)
    except Exception:  # noqa: BLE001 — any backend failure ⇒ unavailable
        return False


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


def resolve_or_create_master_password(
    config_path: str | Path | None, *, encryption_enabled: bool
) -> str | None:
    """Return the master password, generating one if the appliance has none.

    On a default install nothing sets ``MERCURE_MASTER_PASSWORD`` — the env var
    is a deployment-time choice nobody made — so every destination password and
    the admin hash were written to disk in cleartext, on a USB appliance that
    gets handed around a hospital (review P1-1).

    An appliance that wants encryption at rest (the default) therefore gets a
    key on first boot: generated, and persisted so the *next* boot can still
    read its own config. Order of preference:

    1. an explicit master password (env, env file, or caller);
    2. the keyring entry written by a previous boot;
    3. a freshly generated entry in the OS keyring;
    4. a freshly generated 0600 sidecar file beside the config, when no
       keyring backend exists (logged by path — the operator may need it to
       move the config to another host).

    Cleartext is now an explicit opt-in: ``MERCURE_GATEWAY_ALLOW_PLAINTEXT_SECRETS=1``,
    warned about at startup and by the config linter. ``encryption_enabled``
    false (``credentials.encrypted`` off) is the config-level opt-out and also
    returns None.
    """
    explicit = load_master_password()
    if explicit:
        return explicit
    if not encryption_enabled or os.environ.get(_ALLOW_PLAINTEXT_ENV, "") == "1":
        return None

    if _keyring_available():
        import keyring

        from mercure_gateway.keyring_store import _SERVICE

        try:
            existing = keyring.get_password(_SERVICE, _KEYRING_ENTRY)
            if existing:
                return existing
            generated = secrets.token_urlsafe(32)
            keyring.set_password(_SERVICE, _KEYRING_ENTRY, generated)
            logger.info(
                "no config master password was configured; generated one and "
                "stored it in the OS keyring (%s:%s) so secrets are not written "
                "in cleartext. Set MERCURE_MASTER_PASSWORD to choose your own.",
                _SERVICE,
                _KEYRING_ENTRY,
            )
            return generated
        except Exception:  # noqa: BLE001 — a broken keyring falls back to the file
            logger.warning("the OS keyring is unusable; falling back to a sidecar file")

    if config_path is None:
        logger.warning(
            "no config master password and no config path to store one in — "
            "secrets will be written in cleartext. Set %s or MERCURE_MASTER_PASSWORD.",
            _ALLOW_PLAINTEXT_ENV,
        )
        return None

    import errno

    sidecar = Path(str(config_path)).with_name(
        Path(str(config_path)).name + _SIDECAR_SUFFIX
    )
    try:
        if sidecar.exists():
            existing = sidecar.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        generated = secrets.token_urlsafe(32)
        # Write with O_EXCL so a race with another process cannot truncate an
        # existing file; 0600 because the content is the key to every secret.
        fd = os.open(
            sidecar, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(generated)
        logger.warning(
            "no OS keyring is available; wrote the config master password to "
            "%s (mode 0600). Keep this file with the config — without it the "
            "stored secrets cannot be read back.",
            sidecar,
        )
        return generated
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            # Created concurrently between the exists() check and the open.
            existing = sidecar.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        logger.warning(
            "could not persist a config master password (%s); secrets will be "
            "written in cleartext",
            exc,
        )
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
    """Resolve a stored secret, preferring the config vault over the keyring.

    Both hold the value when the keyring was writable at save time; the vault
    is authoritative because it is written unconditionally (the keyring write
    is gated on ``store.available``). Preferring the keyring would let a stale
    entry win: save with the keyring down and only the vault carries the new
    value, then boot with it back up and the keyring still holds the *old* one
    — an operator who rotated a compromised credential would find the
    appliance still using the revoked one, audit-recorded as a success
    (review P1-2, credential cross-wiring in a new place).
    """
    try:
        return vault.decrypt_field(encrypted)
    except WrongPasswordError:
        pass
    if store.available:
        from_keyring = store.get_password(key)
        if from_keyring is not None:
            return from_keyring
    raise ConfigEncryptionError(
        "invalid master password for encrypted configuration"
    )
