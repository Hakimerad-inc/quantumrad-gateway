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


class WindowsServiceBackend(ServiceBackend):
    """Start/stop the gateway Windows service via ``win32serviceutil``."""

    def __init__(self, service_name: str = _SERVICE_NAME) -> None:
        self._service_name = service_name

    def start(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Windows service mode is only available on Windows")
        _win32().StartService(self._service_name)

    def stop(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Windows service mode is only available on Windows")
        _win32().StopService(self._service_name)
