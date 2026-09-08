"""TDD (S10-T6, RED): LED status indicator — usb-dongle-gateway-spec §3.3.

Behaviors:
1. State machine precedence: safe → critical → warning → active → idle →
   off (DoD: "LED state changes per gateway status").
2. GPIO pin configurable: "GPIO18" → BCM 18; invalid forms rejected.
3. Graceful fallback: NoopLed never raises and records the color; SysfsLed
   raises at construction when channels are missing so callers fall back.
4. Config defaults: led_enabled=False (§6) — off-the-shelf USB sticks have
   no LED.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from mercure_gateway.config import USBModeConfig, default_config
from mercure_gateway.led import (
    LedColor,
    LedStatus,
    NoopLed,
    SysfsLed,
    gpio_number,
    resolve_led_state,
)

# ── State machine precedence ─────────────────────────────────────────


def base(**over: bool) -> LedStatus:
    kwargs: dict = {"receiver_running": True, "forwarder_running": True}
    kwargs.update(over)
    return LedStatus(**kwargs)


def test_off_when_gateway_stopped() -> None:
    assert resolve_led_state(LedStatus()) is LedColor.OFF


def test_blue_idle_when_both_running() -> None:
    assert resolve_led_state(base()) is LedColor.BLUE


def test_green_while_sending_or_queued() -> None:
    assert resolve_led_state(base(queued=2)) is LedColor.GREEN
    assert resolve_led_state(base(sending=1)) is LedColor.GREEN


def test_yellow_on_errors() -> None:
    assert resolve_led_state(base(errors=1)) is LedColor.YELLOW


def test_yellow_on_disk_over_threshold_with_purge_armed() -> None:
    assert resolve_led_state(base(disk_over_threshold=True, purge_armed=True)) is LedColor.YELLOW


def test_red_when_forwarder_down_while_receiver_up() -> None:
    assert resolve_led_state(base(forwarder_running=False)) is LedColor.RED


def test_red_when_disk_over_threshold_without_purge() -> None:
    assert resolve_led_state(base(disk_over_threshold=True, purge_armed=False)) is LedColor.RED


def test_white_safe_to_remove_wins_over_everything() -> None:
    worst = base(
        forwarder_running=False,
        errors=5,
        disk_over_threshold=True,
        purge_armed=False,
        safe_to_remove=True,
    )
    assert resolve_led_state(worst) is LedColor.WHITE


# ── Configurable GPIO pin ────────────────────────────────────────────


def test_gpio_pin_parsed_from_config_form() -> None:
    assert gpio_number("GPIO18") == 18
    assert gpio_number("gpio27") == 27
    assert gpio_number(" GPIO5 ") == 5


def test_gpio_pin_rejects_invalid() -> None:
    for bad in ("18", "GPIOX", "PIN18", "GPIO", ""):
        with pytest.raises(ValueError):
            gpio_number(bad)


# ── Graceful fallback (no LED hardware) ──────────────────────────────


def test_noop_led_never_raises_and_records_color() -> None:
    led = NoopLed()
    led.set(LedColor.GREEN)
    assert led.color is LedColor.GREEN
    led.set(LedColor.RED)
    assert led.color is LedColor.RED
    led.close()
    assert led.color is LedColor.OFF


def test_sysfs_led_writes_channels(tmp_path: Path) -> None:
    for ch in ("red", "green", "blue"):
        (tmp_path / ch).write_text("0")
    led = SysfsLed(tmp_path)

    led.set(LedColor.GREEN)
    assert (tmp_path / "red").read_text() == "0"
    assert (tmp_path / "green").read_text() == "1"
    assert (tmp_path / "blue").read_text() == "0"

    led.set(LedColor.RED)
    assert (tmp_path / "red").read_text() == "1"
    assert (tmp_path / "green").read_text() == "0"

    led.close()
    assert (tmp_path / "red").read_text() == "0"


def test_sysfs_led_missing_hardware_raises_at_construction(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        SysfsLed(tmp_path)  # no channel files — fall back to NoopLed


# ── Config defaults (§6) ─────────────────────────────────────────────


def test_led_disabled_by_default_in_usb_profile() -> None:
    cfg = default_config()
    assert cfg.usb_mode.led_enabled is False
    assert cfg.usb_mode.led_pin == "GPIO18"


def test_led_pin_field_accepts_only_valid_shape() -> None:
    cfg = default_config()
    cfg.usb_mode.led_pin = "GPIO26"  # arbitrary valid BCM form
    assert cfg.usb_mode.led_pin == "GPIO26"
    with pytest.raises(ValidationError):
        USBModeConfig(led_pin=42)  # not a string pin
