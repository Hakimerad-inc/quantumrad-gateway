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
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

logger = logging.getLogger(__name__)

__all__ = [
    "LedColor",
    "LedStatus",
    "Led",
    "NoopLed",
    "SysfsLed",
    "LedDriver",
    "build_led",
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


def build_led(
    usb_mode: object,
    *,
    base_dir: Path | None = None,
) -> Led:
    """Construct the configured LED, degrading to :class:`NoopLed` on any failure.

    This is the bridge that was missing: ``led.py`` was complete and tested but
    nothing in the composition root ever called it, so ``usb_mode.led_enabled``
    and ``usb_mode.led_pin`` were read by no code and an operator who enabled
    the indicator got silence.  Every failure path — disabled config, a typo'd
    pin, absent GPIO channels — lands on ``NoopLed``, because a broken or
    unpopulated indicator must never stop a medical appliance from booting.

    ``base_dir`` is the sysfs-style channel tree; production resolves it from
    the parsed BCM pin, tests point it at a ``tmp_path``.
    """
    enabled = bool(getattr(usb_mode, "led_enabled", False))
    if not enabled:
        return NoopLed()
    try:
        pin = gpio_number(str(getattr(usb_mode, "led_pin", "")))
    except ValueError as exc:
        logger.warning("LED disabled — %s; indicator will be a no-op", exc)
        return NoopLed()
    if base_dir is None:
        base_dir = Path(f"/sys/class/gpio/gpio{pin}")
    try:
        return SysfsLed(base_dir)
    except OSError as exc:
        logger.warning("LED disabled — %s; indicator will be a no-op", exc)
        return NoopLed()


class LedDriver:
    """Samples gateway state on a timer and pushes the resolved color.

    ``resolve_led_state`` is a pure function over a snapshot; something has to
    take the snapshot.  This is that thing — a daemon thread that reads the
    live gateway state, resolves it, and calls :meth:`Led.set`, so an operator
    watching a headless appliance sees the transition without polling.

    On stop it drives the safe-to-remove signal (white): the USB shutdown
    sequence is complete and the stick can be pulled.  A snapshot that raises
    is logged and skipped, never propagated — losing the thread would silently
    revert the LED to "stopped" and hide exactly the state it exists to show.
    """

    def __init__(
        self,
        status_fn: Callable[[], LedStatus],
        led: Led,
        *,
        interval_sec: float = 5.0,
        initial_delay_sec: float = 1.0,
    ) -> None:
        self._status_fn = status_fn
        self._led = led
        self._interval = interval_sec
        self._initial_delay = initial_delay_sec
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def interval_sec(self) -> float:
        return self._interval

    def start(self) -> None:
        """Idempotent: a second call is a no-op."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._work, name="led-driver", daemon=True
        )
        self._thread.start()

    def stop(self, *, join_timeout: float = 5.0) -> None:
        """Join the thread, then hold the safe-to-remove color.

        ``stop`` is the driver's part of the §7.2 shutdown sequence: the
        components are down and the marker is about to be written, so white is
        the last thing an operator sees — the stick can be pulled.  Emitted
        after the join so no late sampler can overwrite it.
        """
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=join_timeout)
        self._thread = None
        try:
            self._led.set(LedColor.WHITE)
        except Exception:  # noqa: BLE001 — best-effort signal on the way out
            logger.exception("could not drive the safe-to-remove LED color")

    def _work(self) -> None:
        self._stop_event.wait(self._initial_delay)
        while not self._stop_event.is_set():
            self.update_once()
            self._stop_event.wait(self._interval)

    def update_once(self) -> None:
        """Resolve one snapshot and push it. Never raises."""
        try:
            status = self._status_fn()
        except Exception:  # noqa: BLE001 — the indicator must outlive its sources
            logger.exception("LED status snapshot failed; keeping the last color")
            return
        try:
            self._led.set(resolve_led_state(status))
        except Exception:  # noqa: BLE001 — a dead LED must not kill the driver
            logger.exception("LED hardware rejected a color update")

    def close(self) -> None:
        """Drive the safe-to-remove signal, then release the hardware."""
        try:
            self._led.close()
        except Exception:  # noqa: BLE001 — best-effort on the way out
            logger.exception("LED close failed")
