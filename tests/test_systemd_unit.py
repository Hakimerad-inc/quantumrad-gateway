"""TD-09 (GREEN): the systemd user unit stays runnable and hardened.

Stage-15 packaging rule: the unit must supervise the gateway (restart, health
check, env-file secrets) while its hardening never breaks the two features that
force-fail over-tight sandboxing — the spool under $HOME and USB-dongle /dev
access. The check is a static lint: it reads the unit file so any future edit
that regresses these invariants fails CI.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_UNIT = (_REPO_ROOT / "systemd" / "mercure-gateway.service").read_text()


def test_supervision_directives_present() -> None:
    assert "Restart=always" in _UNIT
    assert "RestartSec=" in _UNIT
    assert "TimeoutStopSec=" in _UNIT
    assert "[Install]" in _UNIT
    assert "WantedBy=default.target" in _UNIT


def test_environment_file_and_deploy_vars() -> None:
    assert "EnvironmentFile=" in _UNIT
    assert "${MERCURE_GATEWAY_CONFIG}" in _UNIT
    assert "${MERCURE_GATEWAY_PORT}" in _UNIT


def test_health_check_line_present() -> None:
    assert "ExecStartPost=" in _UNIT
    assert "/api/system/health" in _UNIT


def test_gateway_entry_point_documented() -> None:
    assert "mercure-gateway" in _UNIT
    assert "--web" in _UNIT


def test_sandbox_keeps_spool_writable() -> None:
    """ProtectHome/ProtectSystem=strict would block the $HOME spool."""

    def _value(directive: str) -> str | None:
        for line in _UNIT.splitlines():
            if line.startswith(f"{directive}="):
                return line.split("=", 1)[1].strip()
        return None

    assert _value("ProtectHome") is None, "ProtectHome would break the spool"
    protect_system = _value("ProtectSystem")
    assert protect_system not in {"strict", "full"}, (
        f"ProtectSystem={protect_system} would block spool/outbox writes"
    )


def test_sandbox_keeps_usb_dongle_devices() -> None:
    """PrivateDevices would hide /dev from USB-dongle mode."""
    assert "PrivateDevices=" not in _UNIT
    assert "DevicePolicy=" not in _UNIT


def test_sandbox_keeps_keyring_dbus() -> None:
    assert "RestrictAddressFamilies=" in _UNIT
    assert "AF_UNIX" in _UNIT, "session D-Bus (keyring) needs AF_UNIX"


def test_sandbox_hardening_present() -> None:
    for directive in (
        "NoNewPrivileges=yes",
        "RestrictRealtime=yes",
        "LockPersonality=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectControlGroups=yes",
    ):
        assert directive in _UNIT, f"expected {directive}"


def test_secrets_env_example_commit_safe() -> None:
    """The env example must ship placeholder values, never real secrets."""
    example = (_REPO_ROOT / "systemd" / "gateway.env.example").read_text()
    assert "change-me" in example or "REPLACE" in example
    # No real-looking secrets.
    for banned in ("sha256$salt$hash$", "BEGIN PRIVATE KEY"):
        assert banned not in example