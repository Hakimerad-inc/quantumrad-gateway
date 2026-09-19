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
from mercure_gateway.config import encryption as enc
from mercure_gateway.config.encryption import (
    ENC_PLACEHOLDER,
    ConfigEncryptionError,
    load_master_password,
)
from mercure_gateway.keyring_store import KeyringCredentialStore
from mercure_gateway.web.auth import hash_password


@pytest.fixture(autouse=True)
def _no_os_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the OS keyring path so these tests always exercise the vault or
    sidecar fallback — no test should ever create a real keyring entry."""
    monkeypatch.setattr(
        KeyringCredentialStore,
        "available",
        property(lambda _self: False),
    )
    monkeypatch.setattr(enc, "_keyring_available", lambda: False)


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


def test_plaintext_is_an_explicit_opt_in(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """With MERCURE_GATEWAY_ALLOW_PLAINTEXT_SECRETS=1 the legacy cleartext
    round-trip is preserved exactly (dev, tests, a sealed read-only appliance)."""
    monkeypatch.setenv("MERCURE_GATEWAY_ALLOW_PLAINTEXT_SECRETS", "1")
    cfg = _secret_config()
    path = tmp_path / "gw.json"

    save_config(cfg, path)
    raw = path.read_text(encoding="utf-8")
    assert "TOPSECRET" in raw
    assert '"entries": {}' in raw or '"entries":{}' in raw
    # No sidecar is written when plaintext is deliberately chosen.
    assert not (tmp_path / "gw.json.master-password").exists()

    loaded = load_config(path)
    assert loaded.destinations[0].password == "TOPSECRET"
    assert loaded.audit.hub_reporting.api_key == "HUBKEY"


def test_encryption_off_in_config_stays_plaintext(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """credentials.encrypted=false is the config-level opt-out."""
    monkeypatch.delenv("MERCURE_GATEWAY_ALLOW_PLAINTEXT_SECRETS", raising=False)
    cfg = _secret_config()
    cfg.credentials.encrypted = False
    path = tmp_path / "gw.json"

    save_config(cfg, path)
    assert "TOPSECRET" in path.read_text(encoding="utf-8")


def test_appliance_generates_a_sidecar_key_on_first_boot(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A headless box (no keyring) still never writes a cleartext secret.

    A key is generated on first boot and persisted to a 0600 file beside the
    config so the *next* boot can read its own config back.
    """
    monkeypatch.delenv("MERCURE_GATEWAY_ALLOW_PLAINTEXT_SECRETS", raising=False)
    monkeypatch.delenv("MERCURE_MASTER_PASSWORD", raising=False)
    monkeypatch.delenv("MERCURE_MASTER_PASSWORD_FILE", raising=False)

    cfg = _secret_config()
    path = tmp_path / "gw.json"

    save_config(cfg, path)

    raw = path.read_text(encoding="utf-8")
    assert "TOPSECRET" not in raw
    assert "XNATPASS" not in raw
    assert ENC_PLACEHOLDER in raw
    assert "AES256GCM:" in raw

    sidecar = tmp_path / "gw.json.master-password"
    assert sidecar.exists()
    assert oct(sidecar.stat().st_mode)[-3:] == "600"
    key = sidecar.read_text(encoding="utf-8").strip()
    assert key

    # The boot after this one reads the config back with no env var set.
    loaded = load_config(path)
    assert loaded.destinations[0].password == "TOPSECRET"
    assert loaded.destinations[0].private_key == "KEYBLOB"
    assert loaded.destinations[1].password == "XNATPASS"
    assert loaded.audit.hub_reporting.api_key == "HUBKEY"


def test_sidecar_key_is_reused_across_boots(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A second boot picks up the existing sidecar rather than regenerating —
    regenerating would orphan every secret already on disk."""
    monkeypatch.delenv("MERCURE_GATEWAY_ALLOW_PLAINTEXT_SECRETS", raising=False)
    monkeypatch.delenv("MERCURE_MASTER_PASSWORD", raising=False)
    monkeypatch.delenv("MERCURE_MASTER_PASSWORD_FILE", raising=False)

    cfg = _secret_config()
    path = tmp_path / "gw.json"
    save_config(cfg, path)
    sidecar = tmp_path / "gw.json.master-password"
    first = sidecar.read_text(encoding="utf-8").strip()

    save_config(_secret_config(), path)
    assert sidecar.read_text(encoding="utf-8").strip() == first


def test_keyring_supplies_the_key_when_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A desktop with a usable keyring stores the master password there — no
    sidecar file, and no secret on disk."""
    monkeypatch.delenv("MERCURE_GATEWAY_ALLOW_PLAINTEXT_SECRETS", raising=False)
    monkeypatch.delenv("MERCURE_MASTER_PASSWORD", raising=False)
    monkeypatch.delenv("MERCURE_MASTER_PASSWORD_FILE", raising=False)

    store: dict[str, str] = {}

    def _get_password(service: str, name: str) -> str | None:
        return store.get(f"{service}:{name}")

    def _set_password(service: str, name: str, value: str) -> None:
        store[f"{service}:{name}"] = value

    monkeypatch.setattr(enc, "_keyring_available", lambda: True)
    import keyring as keyring_mod

    monkeypatch.setattr(keyring_mod, "get_password", _get_password)
    monkeypatch.setattr(keyring_mod, "set_password", _set_password)
    from mercure_gateway.keyring_store import _SERVICE

    cfg = _secret_config()
    path = tmp_path / "gw.json"
    save_config(cfg, path)

    assert "TOPSECRET" not in path.read_text(encoding="utf-8")
    assert not (tmp_path / "gw.json.master-password").exists()
    key = store.get(f"{_SERVICE}:config-master-password")
    assert key is not None

    # A fresh process with the same keyring reads the config back.
    loaded = load_config(path)
    assert loaded.destinations[0].password == "TOPSECRET"


def test_no_config_path_and_no_key_falls_back_to_plaintext_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """In-memory config with no path to write a sidecar to warns rather than
    raising — but it still says so out loud."""
    monkeypatch.delenv("MERCURE_GATEWAY_ALLOW_PLAINTEXT_SECRETS", raising=False)
    monkeypatch.delenv("MERCURE_MASTER_PASSWORD", raising=False)
    monkeypatch.delenv("MERCURE_MASTER_PASSWORD_FILE", raising=False)

    with caplog.at_level("WARNING"):
        from mercure_gateway.config.encryption import resolve_or_create_master_password

        assert resolve_or_create_master_password(None, encryption_enabled=True) is None
    assert "cleartext" in caplog.text.lower()


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
