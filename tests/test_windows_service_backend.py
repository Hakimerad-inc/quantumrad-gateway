"""S07-T9 (RED): Windows service backend — pywin32 integration.

The concrete WindowsServiceBackend uses pywin32 to control the Windows service.
These tests are skipped on non-Windows platforms since pywin32 is not available.
On Windows, they verify the backend correctly calls win32serviceutil.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

# Skip all tests in this module on non-Windows
pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows service backend tests require Windows (pywin32)",
)


def test_windows_service_backend_import() -> None:
    """WindowsServiceBackend can be imported on Windows."""
    from mercure_gateway.service_backend import WindowsServiceBackend

    assert WindowsServiceBackend is not None


def test_windows_service_backend_start_stop_mocked() -> None:
    """Backend calls win32serviceutil.StartService/StopService."""
    with patch("mercure_gateway.service_backend._win32") as mock_win32:
        mock_module = MagicMock()
        mock_win32.return_value = mock_module

        from mercure_gateway.service_backend import WindowsServiceBackend

        backend = WindowsServiceBackend("test-service")
        backend.start()
        backend.stop()

        mock_module.StartService.assert_called_once_with("test-service")
        mock_module.StopService.assert_called_once_with("test-service")


def test_windows_service_backend_rejects_non_windows() -> None:
    """Backend raises RuntimeError when used on non-Windows (defense in depth)."""
    with patch("sys.platform", "linux"):
        # Re-import to trigger platform check
        import importlib

        import mercure_gateway.service_backend as sb

        importlib.reload(sb)

        from mercure_gateway.service_backend import WindowsServiceBackend

        backend = WindowsServiceBackend("test-service")
        with pytest.raises(RuntimeError, match="only available on Windows"):
            backend.start()
        with pytest.raises(RuntimeError, match="only available on Windows"):
            backend.stop()


def test_service_controller_with_windows_backend() -> None:
    """ServiceController works with real WindowsServiceBackend (mocked)."""
    with patch("mercure_gateway.service_backend._win32") as mock_win32:
        mock_module = MagicMock()
        mock_win32.return_value = mock_module

        from mercure_gateway.service_backend import WindowsServiceBackend
        from mercure_gateway.service_controller import ServiceController, ServiceState

        backend = WindowsServiceBackend("test-service")
        controller = ServiceController(backend)

        assert controller.state() == ServiceState.STOPPED
        controller.start()
        assert controller.state() == ServiceState.RUNNING
        controller.stop()
        assert controller.state() == ServiceState.STOPPED

        mock_module.StartService.assert_called_once()
        mock_module.StopService.assert_called_once()
