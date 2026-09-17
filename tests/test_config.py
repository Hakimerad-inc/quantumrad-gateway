"""Unit tests for the pydantic configuration models (PRD §5.5)."""

from __future__ import annotations

import sys

import pytest
from pydantic import ValidationError

from mercure_gateway.config import (
    DICOMDestination,
    GatewayConfig,
    SFTPDestination,
    apply_env_overrides,
    apply_usb_defaults,
    default_config,
    detect_usb_mode,
    is_removable_volume,
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


# ── TD-08: secret inventory & env-var secret injection ───────────────────


def test_env_secret_injection_for_web_ui_auth_password_hash() -> None:
    """The web-UI password hash can be supplied by env, never touching disk."""
    cfg = apply_env_overrides(
        default_config(),
        environ={
            "MERCURE_GATEWAY_WEB_UI_AUTH_ENABLED": "true",
            "MERCURE_GATEWAY_WEB_UI_AUTH_PASSWORD_HASH": "sha256$salt$hash",
        },
    )
    assert cfg.web_ui.auth_enabled is True
    assert cfg.web_ui.auth_password_hash == "sha256$salt$hash"


def test_env_secrets_override_config_file_values() -> None:
    """Env vars win over values already on disk (ops injects at deploy time)."""
    cfg = GatewayConfig.model_validate(
        {
            "web_ui": {
                "auth_enabled": True,
                "auth_password_hash": "sha256$old$onDisk",
            },
            "audit": {
                "hub_reporting": {
                    "enabled": True,
                    "bookkeeper_url": "https://hub.example.com",
                    "api_key": "on-disk-key",
                }
            },
        }
    )
    cfg = apply_env_overrides(
        cfg,
        environ={
            "MERCURE_GATEWAY_AUDIT_HUB_REPORTING_API_KEY": "env-injected-key",
            "MERCURE_GATEWAY_WEB_UI_AUTH_PASSWORD_HASH": "sha256$new$fromEnv",
        },
    )
    assert cfg.audit.hub_reporting.api_key == "env-injected-key"
    assert cfg.web_ui.auth_password_hash == "sha256$new$fromEnv"


def test_env_overrides_leave_other_secrets_intact() -> None:
    """Overriding one secret never clobbers the unrelated credential stores."""
    cfg = GatewayConfig.model_validate(
        {
            "web_ui": {"auth_enabled": True, "auth_password_hash": "sha256$keep$me"},
            "audit": {
                "hub_reporting": {
                    "enabled": True,
                    "bookkeeper_url": "https://hub.example.com",
                    "api_key": "keep-key",
                }
            },
        }
    )
    cfg = apply_env_overrides(
        cfg, environ={"MERCURE_GATEWAY_AUDIT_HUB_REPORTING_API_KEY": "new-key"}
    )
    assert cfg.audit.hub_reporting.api_key == "new-key"
    assert cfg.web_ui.auth_password_hash == "sha256$keep$me"


# ── USB variant: config defaults + auto-detection (S10-T5) ───────────────


def test_usb_mode_defaults() -> None:
    """Default usb_mode matches usb-dongle-gateway-spec §5.3."""
    usb = default_config().usb_mode
    assert usb.enabled is False
    assert usb.storage_budget_gb == 18
    assert usb.retention_delivered_hours == 24
    assert usb.hot_unplug_safe is True
    assert usb.led_enabled is False


def test_apply_usb_defaults_noop_when_disabled() -> None:
    """USB profile is inert unless usb_mode.enabled — a non-USB config keeps
    its administered storage settings (S10-T5)."""
    cfg = default_config()
    cfg.storage.purge_on_disk_full = False
    cfg.storage.disk_full_warning_pct = 80
    cfg.storage.max_spool_gb = 40

    result = apply_usb_defaults(cfg)

    assert result is cfg
    assert cfg.storage.purge_on_disk_full is False
    assert cfg.storage.disk_full_warning_pct == 80
    assert cfg.storage.max_spool_gb == 40


def test_apply_usb_defaults_profile_when_enabled() -> None:
    """Enabling usb_mode adopts the aggressive storage defaults (S10-T5/T7):
    purge on disk-full, 90% warning threshold, spool budgeted to the USB
    data partition (usb_mode.storage_budget_gb)."""
    cfg = default_config()
    cfg.usb_mode.enabled = True
    cfg.usb_mode.storage_budget_gb = 18
    cfg.storage.max_spool_gb = 40  # administered value, overridden for USB

    result = apply_usb_defaults(cfg)

    assert result.storage.purge_on_disk_full is True
    assert result.storage.disk_full_warning_pct == 90
    assert result.storage.max_spool_gb == 18


def test_apply_usb_defaults_is_idempotent() -> None:
    cfg = default_config()
    cfg.usb_mode.enabled = True
    first = apply_usb_defaults(cfg)
    second = apply_usb_defaults(cfg)
    assert first.storage.model_dump() == second.storage.model_dump()


def test_detect_usb_mode_delegates(monkeypatch) -> None:
    monkeypatch.setattr(
        "mercure_gateway.config.is_removable_volume",
        lambda path: str(path).endswith("usb"),
    )
    assert detect_usb_mode("/mnt/usb") is True
    assert detect_usb_mode("/home/local/spool") is False


@pytest.mark.skipif(sys.platform != "linux", reason="Linux _linux_mounts dispatch path")
def test_is_removable_volume_linux_removable_device(monkeypatch) -> None:
    """A path on a removable block device resolves to True."""
    monkeypatch.setattr(
        "mercure_gateway.config._linux_mounts",
        lambda: [("/mnt/usb", "/dev/sdb1")],
    )
    monkeypatch.setattr("mercure_gateway.config._read_removable", lambda dev: dev == "/dev/sdb1")
    assert is_removable_volume("/mnt/usb/spool") is True


@pytest.mark.skipif(sys.platform != "linux", reason="Linux _linux_mounts dispatch path")
def test_is_removable_volume_linux_uses_deepest_mount(monkeypatch) -> None:
    """A nested USB mount under a non-removable parent is found by longest
    mount-point matching."""
    monkeypatch.setattr(
        "mercure_gateway.config._linux_mounts",
        lambda: [
            ("/mnt", "/dev/sda1"),
            ("/mnt/usb", "/dev/sdb1"),
        ],
    )
    monkeypatch.setattr(
        "mercure_gateway.config._read_removable",
        lambda dev: dev == "/dev/sdb1",
    )
    assert is_removable_volume("/mnt/usb/mercure/spool") is True


def test_is_removable_volume_false_when_device_not_removable(monkeypatch) -> None:
    monkeypatch.setattr(
        "mercure_gateway.config._linux_mounts",
        lambda: [("/local", "/dev/sda3")],
    )
    monkeypatch.setattr("mercure_gateway.config._read_removable", lambda dev: False)
    assert is_removable_volume("/local/spool") is False


def test_is_removable_volume_windows_removable(monkeypatch) -> None:
    monkeypatch.setattr("sys.platform", "win32", raising=False)
    monkeypatch.setattr("mercure_gateway.config._windows_removable", lambda path: True)
    assert is_removable_volume(r"C:\mercure-gateway\spool") is True


def test_block_base_maps_partitions_to_media_device() -> None:
    from mercure_gateway.config import _block_base

    assert _block_base("/dev/sdb1") == "sdb"
    assert _block_base("/dev/sdb") == "sdb"
    assert _block_base("/dev/vda2") == "vda"
    assert _block_base("/dev/mmcblk0p1") == "mmcblk0"
    assert _block_base("/dev/nvme0n1p3") == "nvme0n1"
    assert _block_base("/dev/sr0") == "sr0"
    assert _block_base("/dev/mapper/luks-root") is None
    assert _block_base("/tmp/not-a-device") is None


# ══════════════════════════════════════════════════════════════════════
# Web UI TLS configuration (D3a — ADR-0007 transport posture)
# ══════════════════════════════════════════════════════════════════════

def test_webui_tls_fields_default_empty() -> None:
    """TLS off by default: no cert/key set means plain HTTP (loopback default)."""
    cfg = default_config()
    assert cfg.web_ui.tls_cert_file == ""
    assert cfg.web_ui.tls_key_file == ""


def test_webui_tls_requires_both_files() -> None:
    """Cert without key (or vice versa) is a config error, not a silent downgrade."""
    from pydantic import ValidationError

    from mercure_gateway.config import WebUIConfig

    with pytest.raises(ValidationError):
        WebUIConfig(tls_cert_file="/tmp/c.pem")
    with pytest.raises(ValidationError):
        WebUIConfig(tls_key_file="/tmp/k.pem")
    ok = WebUIConfig(tls_cert_file="/tmp/c.pem", tls_key_file="/tmp/k.pem")
    assert ok.tls_cert_file == "/tmp/c.pem"


# ══════════════════════════════════════════════════════════════════════
# Forwarding-rule target hygiene (E1 follow-up: stale targets must not
# strand a study — f8c3250 fixed the runtime path; this is the load-time warning)
# ══════════════════════════════════════════════════════════════════════

def test_stale_forwarding_rule_target_warns_but_loads(caplog) -> None:
    """A rule naming a removed destination warns at load, not at first study."""
    from mercure_gateway.config import DICOMDestination, ForwardingRule

    cfg = GatewayConfig(
        destinations=[DICOMDestination(name="pacs-a", host="h", port=104, aet_target="A")],
        forwarding_rules=[
            ForwardingRule(rule="StudyDescription ~ 'CHEST'", targets=["pacs-a", "pacs-gone"])
        ],
    )
    assert cfg.forwarding_rules[0].targets == ["pacs-a", "pacs-gone"]  # loaded, not truncated
    stale = [r for r in caplog.records if "pacs-gone" in r.getMessage()]
    assert len(stale) == 1 and stale[0].levelname == "WARNING"
    assert "pacs-a" in stale[0].getMessage()  # the known set is quoted for the operator


def test_known_forwarding_rule_targets_are_silent(caplog) -> None:
    """Rules that resolve to configured destinations emit no warning."""
    from mercure_gateway.config import DICOMDestination, ForwardingRule

    GatewayConfig(
        destinations=[DICOMDestination(name="pacs-a", host="h", port=104, aet_target="A")],
        forwarding_rules=[ForwardingRule(rule="true", targets=["pacs-a"])],
    )
    assert not [r for r in caplog.records if "unknown destination" in r.getMessage()]
