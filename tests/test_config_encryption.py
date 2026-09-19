"""H3 — config at-rest encryption wiring (review H3).

Covers: master-password resolution from env, encrypt-on-save / decrypt-on-load
round-trip, that no cleartext secret hits disk, the caller's config is not
mutated, the legacy no-key path stays plaintext (backward compatible), and
fail-closed behaviour when an encrypted file is opened without the key.
"""

import pytest

from mercure_gateway.config import (
    SFTPDestination,
    XNATDestination,
    default_config,
    load_config,
    save_config,
)
from mercure_gateway.config.encryption import (
    ENC_PLACEHOLDER,
    ConfigEncryptionError,
    load_master_password,
)
from mercure_gateway.keyring_store import KeyringCredentialStore
from mercure_gateway.web.auth import hash_password


@pytest.fixture(autouse=True)
def _no_os_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the OS keyring path so these tests always exercise the vault fallback."""
    monkeypatch.setattr(
        KeyringCredentialStore,
        "available",
        property(lambda _self: False),
    )


def _secret_config() -> object:
    cfg = default_config()
    cfg.destinations = [
        SFTPDestination(
            name="nas",
            type="sftp",
            host="h",
            port=22,
            username="u",
            password="TOPSECRET",
            private_key="KEYBLOB",
            passphrase="PHRASE",
        ),
        XNATDestination(
            name="x",
            type="xnat",
            url="http://x",
            username="u",
            password="XNATPASS",
            project="p",
        ),
    ]
    cfg.audit.hub_reporting.api_key = "HUBKEY"
    cfg.web_ui.auth_password_hash = hash_password("s3cret")
    return cfg


def test_master_password_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MERCURE_MASTER_PASSWORD", "envpw")
    monkeypatch.delenv("MERCURE_MASTER_PASSWORD_FILE", raising=False)
    assert load_master_password() == "envpw"


def test_master_password_from_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    pw_file = tmp_path / "pw.txt"
    pw_file.write_text("filepw\n", encoding="utf-8")
    monkeypatch.delenv("MERCURE_MASTER_PASSWORD", raising=False)
    monkeypatch.setenv("MERCURE_MASTER_PASSWORD_FILE", str(pw_file))
    assert load_master_password() == "filepw"


def test_master_password_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MERCURE_MASTER_PASSWORD", raising=False)
    monkeypatch.delenv("MERCURE_MASTER_PASSWORD_FILE", raising=False)
    assert load_master_password() is None


def test_encrypt_roundtrip_store_and_restore(tmp_path) -> None:
    cfg = _secret_config()
    path = tmp_path / "gw.json"

    save_config(cfg, path, master_password="pw")

    # Caller's config must not be mutated by the deep copy in save_config.
    assert cfg.destinations[0].password == "TOPSECRET"

    raw = path.read_text(encoding="utf-8")
    # No cleartext secret on disk.
    assert "TOPSECRET" not in raw
    assert "XNATPASS" not in raw
    assert "HUBKEY" not in raw
    assert "s3cret" not in raw
    # Placeholders stand in for the secret fields.
    assert ENC_PLACEHOLDER in raw
    # Encrypted blocks are stored under credentials.entries.
    assert '"entries"' in raw and "AES256GCM:" in raw

    loaded = load_config(path, master_password="pw")
    assert loaded.destinations[0].password == "TOPSECRET"
    assert loaded.destinations[0].private_key == "KEYBLOB"
    assert loaded.destinations[0].passphrase == "PHRASE"
    assert loaded.destinations[1].password == "XNATPASS"
    assert loaded.audit.hub_reporting.api_key == "HUBKEY"
    assert loaded.web_ui.auth_password_hash == cfg.web_ui.auth_password_hash


def test_no_key_path_stays_plaintext_backward_compatible(tmp_path) -> None:
    cfg = _secret_config()
    path = tmp_path / "gw.json"

    # No master password: behaves exactly as before (cleartext, no entries).
    save_config(cfg, path)
    raw = path.read_text(encoding="utf-8")
    assert "TOPSECRET" in raw
    assert '"entries": {}' in raw or '"entries":{}' in raw

    loaded = load_config(path)
    assert loaded.destinations[0].password == "TOPSECRET"
    assert loaded.audit.hub_reporting.api_key == "HUBKEY"


def test_encrypted_file_without_key_raises(tmp_path) -> None:
    cfg = _secret_config()
    path = tmp_path / "gw.json"
    save_config(cfg, path, master_password="pw")

    with pytest.raises(ConfigEncryptionError):
        load_config(path)  # no master password


def test_wrong_key_raises(tmp_path) -> None:
    cfg = _secret_config()
    path = tmp_path / "gw.json"
    save_config(cfg, path, master_password="pw")

    with pytest.raises(ConfigEncryptionError):
        load_config(path, master_password="wrong-pw")
