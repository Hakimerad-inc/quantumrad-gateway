"""S06-T7 (RED): Config import/export via file upload.

Behaviors:
1. POST /config/import accepts multipart file upload, validates JSON, applies config
2. Import restores redacted secrets from current config (round-trip parity with PUT /config)
3. Invalid config JSON returns 400 with details
4. Export includes config_version field for future migrations
5. Import respects config_version for migration compatibility
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mercure_gateway.config import SFTPDestination, default_config, load_config, save_config
from mercure_gateway.spool import Spool
from mercure_gateway.spool.db import mem_database
from mercure_gateway.web import create_app


@pytest.fixture()
def spool() -> Spool:
    return Spool(mem_database())


@pytest.fixture()
def app(spool: Spool):
    cfg = default_config()
    application = create_app(cfg, spool)
    return application


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app)


def test_import_config_file_upload(client: TestClient, tmp_path: Path) -> None:
    """POST /config/import accepts multipart file, validates, applies, and saves."""
    config_path = tmp_path / "gw.json"
    save_config(default_config(), config_path)
    client.app.state.config_path = str(config_path)

    # Create a valid config JSON file to upload
    import_config = default_config()
    import_config.general.appliance_name = "Imported-Gateway"
    import_config.receiver.port = 11113
    import_json = import_config.model_dump_json(indent=2)

    files = {"file": ("mercure-gateway.json", import_json, "application/json")}
    r = client.post("/api/config/import", files=files)

    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    # Verify config was saved to disk
    saved = load_config(config_path)
    assert saved.general.appliance_name == "Imported-Gateway"
    assert saved.receiver.port == 11113


def test_import_config_validates_schema(client: TestClient, tmp_path: Path) -> None:
    """Invalid config JSON returns 400 with validation details."""
    config_path = tmp_path / "gw.json"
    save_config(default_config(), config_path)
    client.app.state.config_path = str(config_path)

    # Invalid config: bad log level
    bad_json = json.dumps({"general": {"log_level": "INVALID"}})
    files = {"file": ("bad.json", bad_json, "application/json")}
    r = client.post("/api/config/import", files=files)

    assert r.status_code == 400
    assert "Invalid config" in r.json()["detail"]


def test_import_config_preserves_secrets(client: TestClient, tmp_path: Path) -> None:
    """Round-trip: import preserves redacted secrets from current config (like PUT /config)."""
    config_path = tmp_path / "gw.json"
    save_config(default_config(), config_path)
    client.app.state.config_path = str(config_path)

    # Set up current config with real secrets
    real = default_config()
    real.destinations = [
        SFTPDestination(
            name="backup",
            type="sftp",
            host="nas.local",
            username="mercure",
            password="REAL-PASSWORD",
        )
    ]
    real.web_ui.auth_password_hash = "sha256$salt$hash"
    client.app.state.config = real

    # Export current (redacted)
    r = client.get("/api/config/export")
    assert r.status_code == 200
    redacted = r.json()
    assert redacted["destinations"][0]["password"] == "***"

    # Modify a non-secret field and re-import
    redacted["general"]["appliance_name"] = "Updated-Via-Import"
    import_json = json.dumps(redacted)
    files = {"file": ("mercure-gateway.json", import_json, "application/json")}
    r = client.post("/api/config/import", files=files)

    assert r.status_code == 200

    # Verify secrets preserved in saved config
    saved = load_config(config_path)
    assert saved.general.appliance_name == "Updated-Via-Import"
    # The SFTP password should be restored from current config
    sftp_dest = [d for d in saved.destinations if d.name == "backup"][0]
    assert sftp_dest.password == "REAL-PASSWORD"  # type: ignore[union-attr]
    assert saved.web_ui.auth_password_hash == "sha256$salt$hash"


def test_export_config_has_version(client: TestClient) -> None:
    """Export includes config_version field for migration tracking."""
    r = client.get("/api/config/export")
    assert r.status_code == 200
    data = r.json()
    assert "config_version" in data
    assert isinstance(data["config_version"], str)
    assert data["config_version"] == "1.0"


def test_import_config_rejects_unknown_version(client: TestClient, tmp_path: Path) -> None:
    """Future config_version that we don't understand is rejected (safety)."""
    config_path = tmp_path / "gw.json"
    save_config(default_config(), config_path)
    client.app.state.config_path = str(config_path)

    # Config with unknown future version
    future_config = default_config().model_dump()
    future_config["config_version"] = "99.0"
    import_json = json.dumps(future_config)
    files = {"file": ("future.json", import_json, "application/json")}
    r = client.post("/api/config/import", files=files)

    assert r.status_code == 400
    assert "version" in r.json()["detail"].lower()


def test_import_config_accepts_supported_version(client: TestClient, tmp_path: Path) -> None:
    """Known config_version is accepted."""
    config_path = tmp_path / "gw.json"
    save_config(default_config(), config_path)
    client.app.state.config_path = str(config_path)

    # Config with current version
    current_config = default_config().model_dump()
    current_config["config_version"] = "1.0"
    import_json = json.dumps(current_config)
    files = {"file": ("current.json", import_json, "application/json")}
    r = client.post("/api/config/import", files=files)

    assert r.status_code == 200


def test_import_config_without_config_path_validates_only(client: TestClient) -> None:
    """Without config_path, import validates but doesn't persist to disk."""
    # No config_path set on app.state
    if hasattr(client.app.state, "config_path"):
        del client.app.state.config_path

    import_config = default_config()
    import_config.general.appliance_name = "Validated-Only"
    import_json = import_config.model_dump_json()

    files = {"file": ("mercure-gateway.json", import_json, "application/json")}
    r = client.post("/api/config/import", files=files)

    assert r.status_code == 200
    # In-memory config should be updated
    assert client.app.state.config.general.appliance_name == "Validated-Only"
