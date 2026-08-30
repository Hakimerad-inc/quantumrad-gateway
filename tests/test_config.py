"""Unit tests for the pydantic configuration models (PRD §5.5)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mercure_gateway.config import (
    DICOMDestination,
    GatewayConfig,
    SFTPDestination,
    default_config,
    load_config,
    save_config,
)


def test_default_config_valid() -> None:
    cfg = default_config()
    assert cfg.receiver.ae_title == "GATEWAY"
    assert cfg.receiver.port == 11112
    assert cfg.general.appliance_name == "Gateway-CLI-01"
    assert cfg.storage.max_spool_gb == 20
    assert cfg.audit.encrypt is True
    assert cfg.destinations == []
    assert cfg.forwarding_rules == []
    assert cfg.reports.enabled is False


def test_default_config_roundtrips_to_json(tmp_path) -> None:
    path = tmp_path / "mercure-gateway.json"
    save_config(default_config(), path)
    assert path.exists()
    reloaded = load_config(path)
    assert reloaded.model_dump() == default_config().model_dump()


def test_invalid_receiver_port_rejected() -> None:
    # Negative and >65535 are always invalid (0 = ephemeral, tests only).
    for bad_port in (-1, 65536, 70000):
        with pytest.raises(ValidationError):
            GatewayConfig.model_validate({"receiver": {"port": bad_port}})


def test_invalid_log_level_rejected() -> None:
    with pytest.raises(ValidationError):
        GatewayConfig.model_validate({"general": {"log_level": "LOUD"}})


def test_invalid_target_type_rejected() -> None:
    with pytest.raises(ValidationError):
        GatewayConfig.model_validate(
            {"destinations": [{"name": "x", "type": "carrier-pigeon"}]}
        )


def test_dicom_destination_discriminated() -> None:
    cfg = GatewayConfig.model_validate(
        {
            "destinations": [
                {
                    "name": "hub",
                    "type": "dicom",
                    "host": "mercure.example.org",
                    "port": 11112,
                    "aet_target": "MERCURE",
                    "aet_source": "GATEWAY",
                }
            ]
        }
    )
    assert isinstance(cfg.destinations[0], DICOMDestination)
    assert cfg.destinations[0].enabled is True


def test_sftp_destination_optional_fields() -> None:
    cfg = GatewayConfig.model_validate(
        {
            "destinations": [
                {
                    "name": "backup",
                    "type": "sftp",
                    "host": "nas.local",
                    "username": "mercure",
                }
            ]
        }
    )
    assert isinstance(cfg.destinations[0], SFTPDestination)
    dest = cfg.destinations[0]
    assert dest.port == 22
    assert dest.remote_path == "/"
    assert dest.password is None


def test_all_target_types_parse() -> None:
    sample = {
        "destinations": [
            {"name": "d", "type": "dicom", "host": "h", "port": 104, "aet_target": "T"},
            {"name": "t", "type": "dicom_tls", "host": "h", "port": 2762, "aet_target": "T"},
            {"name": "w", "type": "dicomweb", "url": "https://pacs/dicomweb"},
            {"name": "s", "type": "sftp", "host": "h", "username": "u"},
            {"name": "r", "type": "rsync", "host": "h", "username": "u", "remote_path": "/"},
            {"name": "s3", "type": "s3", "bucket": "b"},
            {"name": "f", "type": "folder", "path": "/tmp/out"},
            {"name": "x", "type": "xnat", "url": "https://xnat", "username": "u",
             "password": "p", "project": "P"},
        ]
    }
    cfg = GatewayConfig.model_validate(sample)
    assert len(cfg.destinations) == 8


def test_missing_destination_name_rejected() -> None:
    with pytest.raises(ValidationError):
        GatewayConfig.model_validate(
            {"destinations": [{"type": "folder", "path": "/tmp"}]}
        )


def test_report_on_retrieval_enum() -> None:
    with pytest.raises(ValidationError):
        GatewayConfig.model_validate({"reports": {"on_retrieval": "burn"}})


def test_unknown_top_level_key_ignored_by_default() -> None:
    # Extra keys are ignored (model_config default). Config is a superset-friendly JSON.
    cfg = GatewayConfig.model_validate({"general": {"appliance_name": "Unit"}, "bogus": 1})
    assert cfg.general.appliance_name == "Unit"
