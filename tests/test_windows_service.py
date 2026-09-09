"""S07-T9 (RED): Windows service controller — lifecycle abstraction.

The gateway can run as a Windows service (pywin32/NSSM) for always-on headless
operation, with the tray app as an alternative.  This module provides a
platform-neutral service controller whose lifecycle is testable with fakes;
the Windows backend (pywin32/NSSM) is wired at the composition root.

Behaviors:
1. start → running; stop → stopped; restart round-trips through both
2. is_running reflects the current state
3. start when already running is a no-op (idempotent)
4. stop when already stopped is a no-op (idempotent)
5. install/uninstall (web-admin service management, review follow-up):
   install is idempotent, uninstall stops a running service first,
   uninstall when not installed is a no-op, status() reports installed
"""

from __future__ import annotations

import pytest

from mercure_gateway.service_controller import ServiceController, ServiceState


class FakeBackend:
    """In-memory backend recording start/stop/install/uninstall calls."""

    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.installed_count = 0
        self.uninstalled_count = 0
        self.running = False
        self.installed = False

    def start(self) -> None:
        self.started += 1
        self.running = True

    def stop(self) -> None:
        self.stopped += 1
        self.running = False

    def install(self) -> None:
        self.installed_count += 1
        self.installed = True

    def uninstall(self) -> None:
        self.uninstalled_count += 1
        self.installed = False


@pytest.fixture()
def controller() -> tuple[ServiceController, FakeBackend]:
    backend = FakeBackend()
    return ServiceController(backend), backend


def test_initial_state(controller: tuple[ServiceController, FakeBackend]) -> None:
    svc, _ = controller
    assert svc.state() == ServiceState.STOPPED


def test_start_runs_backend(controller: tuple[ServiceController, FakeBackend]) -> None:
    svc, backend = controller
    svc.start()
    assert svc.state() == ServiceState.RUNNING
    assert backend.started == 1


def test_stop_runs_backend(controller: tuple[ServiceController, FakeBackend]) -> None:
    svc, backend = controller
    svc.start()
    svc.stop()
    assert svc.state() == ServiceState.STOPPED
    assert backend.stopped == 1


def test_restart_round_trips(controller: tuple[ServiceController, FakeBackend]) -> None:
    svc, backend = controller
    svc.start()
    svc.restart()
    assert backend.stopped == 1
    assert backend.started == 2
    assert svc.state() == ServiceState.RUNNING


def test_start_idempotent(controller: tuple[ServiceController, FakeBackend]) -> None:
    svc, backend = controller
    svc.start()
    svc.start()
    svc.start()
    assert backend.started == 1


def test_stop_idempotent(controller: tuple[ServiceController, FakeBackend]) -> None:
    svc, backend = controller
    svc.stop()
    svc.stop()
    assert backend.stopped == 0  # never started → nothing to stop


# ── install / uninstall / status (web-admin service management) ─────────


def test_install_installs_backend_once(
    controller: tuple[ServiceController, FakeBackend],
) -> None:
    svc, backend = controller
    svc.install()
    svc.install()
    svc.install()
    assert backend.installed_count == 1  # idempotent
    assert backend.installed is True


def test_uninstall_stops_running_service_first(
    controller: tuple[ServiceController, FakeBackend],
) -> None:
    svc, backend = controller
    svc.install()
    svc.start()
    svc.uninstall()
    assert backend.stopped == 1
    assert backend.uninstalled_count == 1
    assert backend.installed is False


def test_uninstall_when_not_installed_is_noop(
    controller: tuple[ServiceController, FakeBackend],
) -> None:
    svc, backend = controller
    svc.uninstall()
    assert backend.uninstalled_count == 0


def test_uninstall_idempotent(
    controller: tuple[ServiceController, FakeBackend],
) -> None:
    svc, backend = controller
    svc.install()
    svc.uninstall()
    svc.uninstall()
    assert backend.uninstalled_count == 1


def test_status_reports_installed_flag(
    controller: tuple[ServiceController, FakeBackend],
) -> None:
    svc, _ = controller
    status = svc.status()
    assert status.state == ServiceState.STOPPED
    assert status.installed is False
    svc.install()
    status = svc.status()
    assert status.installed is True
    assert status.state == ServiceState.STOPPED
    svc.start()
    assert svc.status().state == ServiceState.RUNNING
