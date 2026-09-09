"""Windows service backend (PRD §13 Q3, S07-T9).

Concrete :class:`~mercure_gateway.service_controller.ServiceBackend` for
Windows, implemented with ``pywin32``.  Importing this module on non-Windows
platforms is harmless — pywin32 is loaded lazily via ``importlib``.

The service runs the same composition root as ``mercure-gateway --web``
(``main.main(["--web"])``) so the tray app and the service share one entry
point; only the process-launch mechanism differs.
"""

from __future__ import annotations

import importlib
import sys
from typing import Any

from mercure_gateway.service_controller import ServiceBackend

__all__ = ["WindowsServiceBackend"]

_SERVICE_NAME = "mercure-gateway"


def _win32() -> Any:
    """Lazily import ``win32serviceutil``; only usable on Windows."""
    return importlib.import_module("win32serviceutil")


def _service_exe_args() -> str:
    """Arguments the SCM passes to the service executable.

    Source installs run ``<python> -m mercure_gateway --web``-equivalent via
    ``exeArgs="--web"`` against the interpreter; a PyInstaller-frozen sidecar
    (``sys.frozen``, packaging pipeline) is a standalone executable that takes
    ``--web`` directly.
    """
    if getattr(sys, "frozen", False):
        return "--web"
    return "--web"


class WindowsServiceBackend(ServiceBackend):
    """Start/stop/install/uninstall the gateway Windows service."""

    def __init__(self, service_name: str = _SERVICE_NAME) -> None:
        self._service_name = service_name

    def _require_windows(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Windows service mode is only available on Windows")

    def start(self) -> None:
        self._require_windows()
        _win32().StartService(self._service_name)

    def stop(self) -> None:
        self._require_windows()
        _win32().StopService(self._service_name)

    def install(self) -> None:
        """Register the service with the SCM (auto-start on boot)."""
        self._require_windows()
        util = _win32()
        exe = sys.argv[0] if getattr(sys, "frozen", False) else sys.executable
        util.InstallService(
            pythonClassString="",  # unused when exeClassString path is taken
            serviceName=self._service_name,
            displayName="mercure-gateway",
            exeName=exe,
            exeArgs=_service_exe_args(),
            autoStart=True,
        )

    def uninstall(self) -> None:
        """Deregister the service from the SCM."""
        self._require_windows()
        _win32().RemoveService(self._service_name)

    def installed(self) -> bool:
        """True when the SCM currently has the service registered."""
        self._require_windows()
        try:
            _win32().QueryServiceStatus(self._service_name)
        except Exception:  # noqa: BLE001 — boundary: any SCM error means "absent"
            return False
        return True
