"""S07-T8 (RED): OS keyring credential storage (PRD §6.2, refinement §3.2).

Upgrades credential storage from the encrypted config file to the OS keyring
(Windows Credential Manager / SecretService).  Falls back to the encrypted
config vault when no keyring backend is available, and migrates existing
entries transparently.

Behaviors:
1. Set/get/delete round-trip works on a keyring backend
2. ``available`` is False when no backend is installed
3. Fallback path: get falls back to the encrypted config vault
4. Migration copies encrypted-config entries into the keyring
"""

from __future__ import annotations

import pytest

from mercure_gateway.credentials import CredentialVault
from mercure_gateway.keyring_store import KeyringCredentialStore


@pytest.fixture()
def vault() -> CredentialVault:
    return CredentialVault("master-pw")


def _set_broken_keyring() -> None:
    """Replace the global keyring with keyring's 'no backend' sentinel."""
    import keyring
    from keyring.backends.fail import Keyring as FailKeyring

    keyring.set_keyring(FailKeyring())


# ══════════════════════════════════════════════════════════════════════
# Round-trip on a real-ish keyring backend
# ══════════════════════════════════════════════════════════════════════

def test_set_get_delete_round_trip(vault: CredentialVault) -> None:
    import keyring
    from keyring.backends.fail import Keyring as FailKeyring

    if isinstance(keyring.get_keyring(), FailKeyring):
        pytest.skip("no keyring backend available in this environment")

    store = KeyringCredentialStore(vault)
    store.set_password("hub", "s3cr3t")
    assert store.get_password("hub") == "s3cr3t"
    store.delete_password("hub")
    assert store.get_password("hub") is None


# ══════════════════════════════════════════════════════════════════════
# Availability detection
# ══════════════════════════════════════════════════════════════════════

def test_available_detects_missing_backend(vault: CredentialVault) -> None:
    """With a backend that raises, ``available`` must be False."""
    _set_broken_keyring()
    store = KeyringCredentialStore(vault)
    assert store.available is False


# ══════════════════════════════════════════════════════════════════════
# Fallback to encrypted config vault
# ══════════════════════════════════════════════════════════════════════

def test_fallback_to_encrypted_config(vault: CredentialVault) -> None:
    """When the keyring is unavailable, get returns the encrypted-config
    value (transparent fallback per refinement §3.2)."""
    _set_broken_keyring()
    store = KeyringCredentialStore(vault)
    store.set_password("hub", "fallback-pw")
    assert store.get_password("hub") == "fallback-pw"


# ══════════════════════════════════════════════════════════════════════
# Migration from encrypted config
# ══════════════════════════════════════════════════════════════════════

def test_migrate_copies_encrypted_config_entries(vault: CredentialVault) -> None:
    """Migration copies each encrypted config entry into the keyring and
    returns how many were migrated."""
    _set_broken_keyring()

    # Build a config with encrypted entries (encrypted config store is the
    # migration source; the keyring store is the destination).
    store = KeyringCredentialStore(vault)
    migrated = store.migrate_from(
        {
            "hub": "hub-secret",
            "nas": "nas-secret",
        }
    )
    assert migrated == 2
    assert store.get_password("hub") == "hub-secret"
    assert store.get_password("nas") == "nas-secret"
