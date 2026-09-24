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
from mercure_gateway.spool import Spool, StudyState
from mercure_gateway.spool.db import mem_database

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


# ── Construction from config (the wiring gap) ────────────────────────────
#
# led.py was complete and fully tested but nothing in the composition root
# ever constructed it, so usb_mode.led_enabled / led_pin were read by no code
# and an operator who enabled the LED got silence.  These pin the bridge.


def test_disabled_config_yields_the_noop_led() -> None:
    """led_enabled=False (the default) must not touch hardware."""
    from mercure_gateway.led import build_led

    led = build_led(USBModeConfig())

    assert isinstance(led, NoopLed)


def test_enabled_config_builds_the_sysfs_led_when_channels_exist(
    tmp_path: Path,
) -> None:
    """An enabled config with a real channel tree drives the GPIO LED."""
    from mercure_gateway.led import build_led

    for ch in ("red", "green", "blue"):
        (tmp_path / ch).write_text("0")

    led = build_led(
        USBModeConfig(led_enabled=True, led_pin="GPIO18"), base_dir=tmp_path
    )

    assert isinstance(led, SysfsLed)


def test_enabled_config_falls_back_when_hardware_is_absent(
    tmp_path: Path,
) -> None:
    """The whole point of NoopLed: a missing channel must not crash the gateway.

    SysfsLed raises at construction; build_led catches it and degrades, because
    a broken indicator is never a reason to lose a medical appliance.
    """
    from mercure_gateway.led import build_led

    led = build_led(
        USBModeConfig(led_enabled=True, led_pin="GPIO18"), base_dir=tmp_path
    )

    assert isinstance(led, NoopLed)


def test_an_invalid_pin_falls_back_rather_than_raising() -> None:
    """A typo'd led_pin is an operator error, not a boot failure."""
    from mercure_gateway.led import build_led

    led = build_led(USBModeConfig(led_enabled=True, led_pin="PIN18"))

    assert isinstance(led, NoopLed)


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


# ── The periodic driver ─────────────────────────────────────────────────
#
# build_led alone is not enough — something has to sample gateway state on a
# cadence and call set().  These pin that loop and its failure isolation.


def _wait_for_color(led: NoopLed, color: LedColor, timeout: float = 5.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if led.color is color:
            return
        time.sleep(0.02)
    raise AssertionError(f"LED never reached {color}; still {led.color}")


def test_the_driver_reflects_a_state_change_on_its_cadence() -> None:
    """The finding: an operator enabled the LED and nothing happened.

    The driver samples the snapshot on a timer and pushes the resolved color,
    so a gateway that goes idle turns blue without anyone polling for it.
    """
    from mercure_gateway.led import LedDriver

    led = NoopLed()
    status = LedStatus(receiver_running=True, forwarder_running=True)
    driver = LedDriver(lambda: status, led, interval_sec=0.05, initial_delay_sec=0.0)
    driver.start()
    try:
        _wait_for_color(led, LedColor.BLUE)

        status.queued = 3  # type: ignore[misc]
        _wait_for_color(led, LedColor.GREEN)
    finally:
        driver.stop()

    # Stop drives the safe-to-remove signal: the operator can unplug.
    assert led.color is LedColor.WHITE


def test_a_snapshot_that_raises_does_not_kill_the_driver() -> None:
    """A broken status source must not turn the LED off for good.

    The LED is the only at-a-glance signal on a headless appliance; losing the
    thread over a transient read error would silently revert it to "stopped".
    """
    from mercure_gateway.led import LedDriver

    led = NoopLed()
    calls: list[int] = []

    def flaky() -> LedStatus:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("spool read failed")
        return LedStatus(receiver_running=True, forwarder_running=True)

    driver = LedDriver(flaky, led, interval_sec=0.05, initial_delay_sec=0.0)
    driver.start()
    try:
        _wait_for_color(led, LedColor.BLUE)
        assert len(calls) >= 2  # survived the first failure and kept sampling
    finally:
        driver.stop()


# ── Composition-root wiring ─────────────────────────────────────────────


def test_the_composition_root_constructs_the_indicator() -> None:
    """The gap this whole change closes: main() must build the LED.

    Before this, ``led.py`` was complete and tested and nothing in main()
    ever imported it — so ``usb_mode.led_enabled`` was read by no code and an
    operator who enabled it got silence.  Probes the real composition-root
    function with USB mode forced on.
    """
    import mercure_gateway.main as main_mod
    from mercure_gateway.led import LedDriver

    cfg = default_config()
    cfg.usb_mode.led_enabled = True
    # No GPIO tree exists in a test environment → NoopLed, still a driver.
    driver = main_mod._start_led_indicator(
        cfg,
        Spool(mem_database(), cfg),
        _FakeComponent(running=True),
        _FakeComponent(running=True),
        _FakeDiskMonitor(),
    )
    try:
        assert isinstance(driver, LedDriver)
    finally:
        driver.stop()
        driver.close()


def test_the_indicator_is_not_started_when_disabled() -> None:
    """A stock install (led_enabled=False) gets no thread and no hardware."""
    import mercure_gateway.main as main_mod

    cfg = default_config()
    driver = main_mod._start_led_indicator(
        cfg,
        Spool(mem_database(), cfg),
        _FakeComponent(running=True),
        _FakeComponent(running=True),
        _FakeDiskMonitor(),
    )

    assert driver is None


def test_the_snapshot_counts_a_study_still_being_received() -> None:
    """A mid-ingest study is GREEN, not BLUE (spec §3.3: GREEN = receiving).

    The state machine's ``sending`` field is "a transfer is in progress", and a
    C-STORE association that has not committed yet sits in RECEIVING — the one
    state a snapshot built only from QUEUED/SENDING would miss, so the LED would
    read "idle" while a large study streamed in.
    """
    import mercure_gateway.main as main_mod
    from mercure_gateway.led import LedColor

    cfg = default_config()
    cfg.usb_mode.led_enabled = True  # no GPIO tree → NoopLed, still a real driver
    spool = Spool(mem_database(), cfg)
    study_id = spool.receive("1.2.826.0.1.3680043.10.150.9")
    spool._db.set_study_state(study_id, StudyState.RECEIVING.value)

    driver = main_mod._start_led_indicator(
        cfg,
        spool,
        _FakeComponent(running=True),
        _FakeComponent(running=True),
        _FakeDiskMonitor(),
    )
    try:
        assert driver is not None
        driver.update_once()
        assert driver._led.color is LedColor.GREEN
    finally:
        if driver is not None:
            driver.stop()
            driver.close()


class _FakeComponent:
    def __init__(self, *, running: bool) -> None:
        self._running = running

    @property
    def is_running(self) -> bool:
        return self._running


class _FakeDiskMonitor:
    last_reading = None
