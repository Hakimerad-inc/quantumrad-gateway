"""Unit tests for the pydantic configuration models (PRD §5.5)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mercure_gateway.config import (
    DICOMDestination,
    GatewayConfig,
    SFTPDestination,
    apply_env_overrides,
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


# ── MERCURE_GATEWAY_* 12-factor env overrides ────────────────────────────


def test_env_overrides_scalar_fields_recursively() -> None:
    cfg = apply_env_overrides(
        default_config(),
        environ={
            "MERCURE_GATEWAY_GENERAL_APPLIANCE_NAME": "Deploy-1",
            "MERCURE_GATEWAY_RECEIVER_PORT": "11114",
            "MERCURE_GATEWAY_RECEIVER_AE_TITLE": "DEPLOY",
            "MERCURE_GATEWAY_WEB_UI_AUTH_ENABLED": "true",
            "MERCURE_GATEWAY_STORAGE_DISK_FULL_WARNING_PCT": "85",
            "MERCURE_GATEWAY_GENERAL_LOG_LEVEL": "DEBUG",
        },
    )
    assert cfg.general.appliance_name == "Deploy-1"
    assert cfg.general.log_level == "DEBUG"
    assert cfg.receiver.port == 11114
    assert cfg.receiver.ae_title == "DEPLOY"
    assert cfg.web_ui.auth_enabled is True
    assert cfg.storage.disk_full_warning_pct == 85


def test_env_secret_injection_for_hub_api_key() -> None:
    """Secrets can be injected without ever touching the config file."""
    cfg = apply_env_overrides(
        default_config(),
        environ={
            "MERCURE_GATEWAY_AUDIT_HUB_REPORTING_ENABLED": "true",
            "MERCURE_GATEWAY_AUDIT_HUB_REPORTING_BOOKKEEPER_URL": "https://hub.example.com",
            "MERCURE_GATEWAY_AUDIT_HUB_REPORTING_API_KEY": "change-me-now",
        },
    )
    assert cfg.audit.hub_reporting.enabled is True
    assert cfg.audit.hub_reporting.bookkeeper_url == "https://hub.example.com"
    assert cfg.audit.hub_reporting.api_key == "change-me-now"


def test_unset_env_vars_leave_fields_unchanged() -> None:
    cfg = apply_env_overrides(default_config(), environ={})
    assert cfg == default_config()


def test_env_collection_fields_are_skipped() -> None:
    """destinations[] is too complex for env vars — the config file owns it."""
    cfg = apply_env_overrides(
        default_config(),
        environ={"MERCURE_GATEWAY_DESTINATIONS": '{"bogus": "json"} {"n": 1}'},
    )
    assert cfg.destinations == []


def test_env_invalid_value_raises_value_error() -> None:
    with pytest.raises(ValueError):
        apply_env_overrides(
            default_config(),
            environ={"MERCURE_GATEWAY_WEB_UI_PORT": "not-a-port"},
        )


def test_env_boolean_value_false() -> None:
    cfg = apply_env_overrides(
        default_config(),
        environ={"MERCURE_GATEWAY_WEB_UI_AUTH_ENABLED": "false"},
    )
    assert cfg.web_ui.auth_enabled is False
