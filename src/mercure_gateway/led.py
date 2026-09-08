"""LED status indicator — usb-dongle-gateway-spec §3.3 (S10-T6).

State machine:
  ⚫ off      — gateway stopped
  🔵 idle     — receiver/forwarder running, nothing queued
  🟢 active   — receiving or forwarding in progress
  🟡 warning  — retry/error present, or disk usage at/over the warning threshold
  🔴 critical — forwarder stopped while receiver up / disk over threshold with
                auto-purge disabled
  ⚪ safe     — safe to remove (shutdown sequence completed, marker written)

Design: a pure state machine (``LedColor`` / ``LedStatus`` /
``resolve_led_state``) plus a ``Led`` interface with two backends —
``SysfsLed`` (Linux GPIO, BCM pin from config ``led_pin``) and ``NoopLed``,
the graceful fallback when no LED hardware exists (most USB sticks have
none; config defaults ``led_enabled=False``). Blink cadence is a
presentation concern of the hardware layer; the state machine emits color
only.
"""

from __future__ import annotations

import enum
import threading
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

__all__ = [
    "LedColor",
    "LedStatus",
    "Led",
    "NoopLed",
    "SysfsLed",
    "gpio_number",
    "resolve_led_state",
]


class LedColor(enum.StrEnum):
    """LED colors from the spec §3.3 state machine."""

    OFF = "off"          # ⚫ gateway stopped
    BLUE = "blue"        # 🔵 idle
    GREEN = "green"      # 🟢 receiving/forwarding
    YELLOW = "yellow"    # 🟡 error/retry
    RED = "red"          # 🔴 critical
    WHITE = "white"      # ⚪ safe to remove


class LedStatus(BaseModel):
    """Snapshot of gateway state the LED mirrors."""

    receiver_running: bool = False
    forwarder_running: bool = False
    queued: int = 0
    sending: int = 0
    errors: int = 0
    disk_over_threshold: bool = False
    purge_armed: bool = True
    safe_to_remove: bool = False


def resolve_led_state(status: LedStatus) -> LedColor:
    """Map a status snapshot to an LED color (spec §3.3 precedence).

    Precedence (most severe wins): safe-to-remove → critical → warning →
    active → idle → off. Critical is a forwarder down while the receiver
    still accepts studies (studies pile up unsent), or disk over threshold
    with auto-purge disabled (space can only run out).
    """
    if status.safe_to_remove:
        return LedColor.WHITE
    if not status.forwarder_running and status.receiver_running:
        return LedColor.RED
    if status.disk_over_threshold and not status.purge_armed:
        return LedColor.RED
    if status.errors > 0 or status.disk_over_threshold:
        return LedColor.YELLOW
    if status.sending > 0 or status.queued > 0:
        return LedColor.GREEN
    if status.receiver_running and status.forwarder_running:
        return LedColor.BLUE
    return LedColor.OFF


class Led(Protocol):
    """A controllable status LED (hardware or no-op)."""

    def set(self, color: LedColor) -> None: ...

    def close(self) -> None: ...


class NoopLed:
    """Graceful fallback when no LED hardware exists (or led_enabled=False).

    Records the last color so tests (and a status endpoint) can assert
    state transitions; never touches hardware and never raises.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.color: LedColor = LedColor.OFF

    def set(self, color: LedColor) -> None:
        with self._lock:
            self.color = color

    def close(self) -> None:
        with self._lock:
            self.color = LedColor.OFF


class SysfsLed:
    """Linux-mode GPIO LED through a sysfs-like channel tree.

    ``base_dir`` holds one writable file per RGB channel (``red`` /
    ``green`` / ``blue``) whose content is ``0`` or ``1``. In production
    this is a GPIO tree (BCM pin parsed from config ``led_pin``, e.g.
    "GPIO18" → 18); tests point it at a ``tmp_path``. Missing channels
    raise at construction so the caller can fall back to
    :class:`NoopLed` — a broken LED must never take the gateway down.
    """

    _CHANNELS = ("red", "green", "blue")

    def __init__(self, base_dir: Path) -> None:
        self._base = base_dir
        for channel in self._CHANNELS:
            if not (base_dir / channel).is_file():
                raise OSError(f"LED channel {channel!r} not present at {base_dir}")

    def set(self, color: LedColor) -> None:
        levels: dict[LedColor, tuple[int, int, int]] = {
            LedColor.OFF: (0, 0, 0),
            LedColor.BLUE: (0, 0, 1),
            LedColor.GREEN: (0, 1, 0),
            LedColor.YELLOW: (1, 1, 0),
            LedColor.RED: (1, 0, 0),
            LedColor.WHITE: (1, 1, 1),
        }
        for channel, level in zip(self._CHANNELS, levels[color], strict=True):
            (self._base / channel).write_text(str(level), encoding="ascii")

    def close(self) -> None:
        self.set(LedColor.OFF)


def gpio_number(pin: str) -> int:
    """Parse the config ``led_pin`` form ("GPIO18") into a BCM number."""
    pin = pin.strip().upper()
    if not pin.startswith("GPIO") or not pin[4:].isdigit():
        raise ValueError(f"invalid led_pin {pin!r} — expected 'GPIO<N>' (BCM numbering)")
    return int(pin[4:])
