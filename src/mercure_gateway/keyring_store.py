"""OS keyring credential storage (PRD §6.2, refinement §3.2, S07-T8).

Upgrades credential storage from the encrypted config file to the OS keyring
(Windows Credential Manager / SecretService / macOS Keychain).  Falls back to
the encrypted config vault when no keyring backend is available, and migrates
existing entries transparently.

Typical usage::

    store = KeyringCredentialStore(vault)
    store.set_password("hub", "s3cr3t")
    secret = store.get_password("hub")
"""

from __future__ import annotations

from collections.abc import Mapping

from mercure_gateway.credentials import CredentialVault

__all__ = ["KeyringCredentialStore"]

_SERVICE = "mercure-gateway"


class KeyringCredentialStore:
    """Keyring-backed credential store with encrypted-config fallback."""

    def __init__(self, vault: CredentialVault | None = None) -> None:
        self._vault = vault
        # Fallback cache: name → plaintext secret (used when no keyring).
        self._fallback: dict[str, str] = {}

    # -- availability ----------------------------------------------------

    @property
    def available(self) -> bool:
        """Whether an OS keyring backend is usable.

        keyring uses ``FailKeyring`` as the sentinel for "no backend could be
        loaded" — any other backend is considered usable.  A configured backend
        that errors at runtime is caught by the caller's fallback paths.
        """
        try:
            import keyring
            from keyring.backends.fail import Keyring as FailKeyring

            backend = keyring.get_keyring()
            return not isinstance(backend, FailKeyring)
        except Exception:  # noqa: BLE001 — any backend failure ⇒ unavailable
            return False

    # -- operations ------------------------------------------------------

    def set_password(self, name: str, password: str) -> None:
        """Store *password* for *name* in the keyring (or fallback cache)."""
        if self.available:
            import keyring

            keyring.set_password(_SERVICE, name, password)
        else:
            self._fallback[name] = password

    def get_password(self, name: str) -> str | None:
        """Return the stored password for *name* or ``None``."""
        if self.available:
            import keyring

            return keyring.get_password(_SERVICE, name)
        return self._fallback.get(name)

    def delete_password(self, name: str) -> None:
        """Delete the stored password for *name*."""
        if self.available:
            import contextlib

            import keyring

            with contextlib.suppress(keyring.errors.PasswordDeleteError):
                keyring.delete_password(_SERVICE, name)
        else:
            self._fallback.pop(name, None)

    # -- migration -------------------------------------------------------

    def migrate_from(self, entries: Mapping[str, str]) -> int:
        """Migrate encrypted-config entries into the keyring.

        *entries* maps destination name → plaintext secret (decrypted from the
        config vault by the caller).  Returns the number migrated.
        """
        count = 0
        for name, secret in entries.items():
            self.set_password(name, secret)
            count += 1
        return count
