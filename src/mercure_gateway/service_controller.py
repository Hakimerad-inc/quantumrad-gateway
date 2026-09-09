"""Windows service controller (PRD §13 Q3, refinement §3.2, S07-T9).

The gateway can run as a Windows service (via pywin32/NSSM) for always-on
headless operation, with the tray app as the default alternative.  This module
provides a platform-neutral controller whose lifecycle is unit-testable; the
concrete backend (pywin32/NSSM) is supplied at the composition root on Windows.

Service management (install/uninstall/status) is exposed to the web admin
panel (S07-T9 DoD: "started/stopped from web admin panel").
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

__all__ = ["ServiceBackend", "ServiceController", "ServiceState", "ServiceStatus"]


class ServiceState(Enum):
    """Lifecycle state of the managed service."""

    STOPPED = "stopped"
    RUNNING = "running"


@dataclass(frozen=True)
class ServiceStatus:
    """Point-in-time service status: controller-local state + install flag."""

    state: ServiceState
    installed: bool


class ServiceBackend(Protocol):
    """Minimal interface implemented by the platform service wrapper."""

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def install(self) -> None: ...
    def uninstall(self) -> None: ...
    def installed(self) -> bool: ...


class ServiceController:
    """Idempotent lifecycle controller around a :class:`ServiceBackend`.

    ``state()`` is controller-local: it reflects operations issued by *this*
    process and resets on restart.  A process-restart-accurate state needs
    SCM queries in the backend — a documented follow-up, not needed for the
    web admin's start/stop/install/uninstall surface.
    """

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

    # ── install / uninstall / status (S07-T9 web admin surface) ─────────

    def _is_installed(self) -> bool:
        """Backend install state, tolerating plain-attribute fakes."""
        installed = self._backend.installed
        # Legacy/fake backends may expose a plain ``installed`` attribute
        # instead of the Protocol's ``installed()`` method.
        return bool(installed()) if callable(installed) else bool(installed)

    def install(self) -> None:
        """Register the service (idempotent: no-op when already installed)."""
        if self._is_installed():
            return
        self._backend.install()

    def uninstall(self) -> None:
        """Stop the service if running, then deregister (no-op when absent)."""
        if not self._is_installed():
            return
        self.stop()
        self._backend.uninstall()

    def status(self) -> ServiceStatus:
        return ServiceStatus(state=self.state(), installed=self._is_installed())
