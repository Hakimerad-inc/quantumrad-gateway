"""Windows service controller (PRD §13 Q3, refinement §3.2, S07-T9).

The gateway can run as a Windows service (via pywin32/NSSM) for always-on
headless operation, with the tray app as the default alternative.  This module
provides a platform-neutral controller whose lifecycle is unit-testable; the
concrete backend (pywin32/NSSM) is supplied at the composition root on Windows.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol

__all__ = ["ServiceController", "ServiceState"]


class ServiceState(Enum):
    """Lifecycle state of the managed service."""

    STOPPED = "stopped"
    RUNNING = "running"


class ServiceBackend(Protocol):
    """Minimal interface implemented by the platform service wrapper."""

    def start(self) -> None: ...
    def stop(self) -> None: ...


class ServiceController:
    """Idempotent lifecycle controller around a :class:`ServiceBackend`."""

    def __init__(self, backend: ServiceBackend) -> None:
        self._backend = backend
        self._running = False

    def state(self) -> ServiceState:
        return ServiceState.RUNNING if self._running else ServiceState.STOPPED

    def start(self) -> None:
        if self._running:
            return
        self._backend.start()
        self._running = True

    def stop(self) -> None:
        if not self._running:
            return
        self._backend.stop()
        self._running = False

    def restart(self) -> None:
        self.stop()
        self.start()
