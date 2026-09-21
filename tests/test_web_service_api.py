"""Web admin Windows-service endpoints (PRD §13 Q3, S07-T9).

The service management surface is Windows-only; off Windows the composition
root leaves ``app.state.service_controller`` unset, and these tests pin the
degraded behaviour: GET reports ``available: false`` (200) and POST actions
return 501 with a clear detail.  With a (fake) controller wired, the endpoints
must call exactly the controller op they name.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Iterator

import pytest
from conftest import FakeForwarder, FakeReceiver
from fastapi.testclient import TestClient

from mercure_gateway.config import default_config
from mercure_gateway.service_controller import ServiceController, ServiceState
from mercure_gateway.spool import Spool
from mercure_gateway.spool.db import mem_database
from mercure_gateway.web import _join_health_monitor, create_app
from mercure_gateway.web.pipeline import DestinationHealthMonitor


class FakeServiceBackend:
    """Records controller ops without touching any SCM."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.installed_flag = False

    def start(self) -> None:
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")

    def install(self) -> None:
        self.calls.append("install")
        self.installed_flag = True

    def uninstall(self) -> None:
        self.calls.append("uninstall")
        self.installed_flag = False

    def installed(self) -> bool:
        return self.installed_flag


@pytest.fixture()
def app(fake_receiver: FakeReceiver, fake_forwarder: FakeForwarder):
    cfg = default_config()
    application = create_app(cfg, Spool(mem_database()))
    application.state.receiver = fake_receiver
    application.state.forwarder = fake_forwarder
    return application


@pytest.fixture()
def client(app) -> Iterator[TestClient]:
    # Context-managed so the ASGI lifespan runs: the destination health monitor
    # starts on startup and is stopped (and joined) on shutdown inside it, and
    # a bare TestClient never enters the lifespan (starlette starts the portal
    # in __enter__ only).
    with TestClient(app) as context_client:
        yield context_client


@pytest.fixture()
def app_with_service(app):
    app.state.service_controller = ServiceController(FakeServiceBackend())
    return app


def test_service_status_returns_unsupported_without_controller(client: TestClient) -> None:
    res = client.get("/api/service")
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is False
    assert body["state"] == "unsupported"
    assert body["installed"] is False


def test_service_status_with_controller(app_with_service) -> None:
    client = TestClient(app_with_service)
    res = client.get("/api/service")
    assert res.status_code == 200
    body = res.json()
    assert body == {"available": True, "installed": False, "state": "stopped"}


def test_service_actions_call_controller(app_with_service) -> None:
    client = TestClient(app_with_service)
    for action in ("install", "start", "stop", "uninstall"):
        res = client.post(f"/api/service/{action}")
        assert res.status_code == 200
        assert res.json() == {"status": action}
    backend = app_with_service.state.service_controller._backend
    assert backend.calls == ["install", "start", "stop", "uninstall"]


def test_service_unknown_action_is_404(app_with_service) -> None:
    client = TestClient(app_with_service)
    res = client.post("/api/service/reboot")
    assert res.status_code == 404


def test_service_action_returns_501_without_controller(client: TestClient) -> None:
    res = client.post("/api/service/install")
    assert res.status_code == 501
    assert "only available on Windows" in res.json()["detail"]


def test_service_uninstall_stops_running_first(app_with_service) -> None:
    client = TestClient(app_with_service)
    assert client.post("/api/service/install").status_code == 200
    assert client.post("/api/service/start").status_code == 200
    backend = app_with_service.state.service_controller._backend
    backend.calls.clear()
    assert client.post("/api/service/uninstall").status_code == 200
    # stop is issued by the controller before the backend uninstall
    assert backend.calls == ["stop", "uninstall"]
    status = client.get("/api/service").json()
    assert status["installed"] is False
    assert status["state"] == ServiceState.STOPPED.value


def test_service_routes_require_auth(app_with_service) -> None:
    """Auth-disabled default config accepts the request (open API behaviour);
    with auth enabled and no session, the admin router must 401."""
    cfg = default_config()
    assert cfg.web_ui.auth_enabled is False  # default dev posture


def test_run_web_admin_wires_service_controller_platform_gated(
    fake_receiver: FakeReceiver, fake_forwarder: FakeForwarder, monkeypatch
) -> None:
    """Composition root: win32 gets a real controller, POSIX gets None."""
    import sys as _sys

    import mercure_gateway.main as main_mod

    cfg = default_config()
    spool = Spool(mem_database())

    # Prevent uvicorn from actually running: stub the server loop.
    class _FakeServer:
        def __init__(self, app, host, port, log_level) -> None:  # type: ignore[no-untyped-def]
            pass

        def run(self) -> None:
            pass

    class _FakeMonitor:
        def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    monkeypatch.setattr(main_mod, "_FakeUvicorn", _FakeServer, raising=False)

    import uvicorn as _uvicorn_mod

    class _StubUvicorn:
        @staticmethod
        def run(*a, **kw) -> None:  # type: ignore[no-untyped-def]
            raise KeyboardInterrupt  # break out of the blocking call cleanly

    monkeypatch.setattr(_uvicorn_mod, "run", _StubUvicorn.run)
    # The monitor is constructed inside create_app now (its lifespan owns the
    # lifecycle), and create_app resolves DestinationHealthMonitor by a
    # function-local import, so patching the module attribute still lands on
    # the construction site — and keeps _run_web_admin from ever building a
    # real probing thread here.
    monkeypatch.setattr(
        "mercure_gateway.web.pipeline.DestinationHealthMonitor", _FakeMonitor
    )

    monkeypatch.setattr(_sys, "platform", "linux")
    app_holder = {}

    from mercure_gateway.web import create_app as real_create_app

    def _capture_create_app(config, spool, **kwargs):  # type: ignore[no-untyped-def]
        # Signature follows create_app: config, spool, then keyword-only
        # optional wiring (config_path, disk_monitor). Forwarded wholesale so a
        # new optional wire does not have to be mirrored here to keep this
        # capture working.
        application = real_create_app(config, spool, **kwargs)
        app_holder["app"] = application
        return application

    monkeypatch.setattr(
        "mercure_gateway.main._run_web_admin_create_app",
        _capture_create_app,
        raising=False,
    )
    # _run_web_admin imports create_app inside the function body, so patch the
    # web package attribute the local import resolves against.
    import mercure_gateway.web as web_pkg

    monkeypatch.setattr(web_pkg, "create_app", _capture_create_app)
    with contextlib.suppress(KeyboardInterrupt):
        main_mod._run_web_admin(
            cfg,
            spool,
            fake_receiver,
            fake_forwarder,
            report_retriever=None,  # type: ignore[arg-type]
            port=18080,
        )
    app = app_holder["app"]
    assert app.state.service_controller is None  # linux → None
    # _run_web_admin must not construct the monitor itself: it moved into
    # create_app's lifespan, and the stubbed uvicorn above never enters a
    # lifespan, so nothing is constructed here. (If construction ever moves
    # back into _run_web_admin this assertion catches it — and the patch above
    # is what keeps that regression from starting a real probing thread.)
    assert not hasattr(app.state, "health_monitor")

    monkeypatch.setattr(_sys, "platform", "win32")
    constructed: dict[str, object] = {}

    # The imports inside _run_web_admin are local; patch the backend module.
    import mercure_gateway.service_backend as backend_mod
    import mercure_gateway.service_controller as controller_mod

    class _StubBackend:
        def installed(self) -> bool:
            return False

    monkeypatch.setattr(backend_mod, "WindowsServiceBackend", _StubBackend)
    monkeypatch.setattr(
        controller_mod.ServiceController,
        "__init__",
        lambda self, backend: constructed.update(backend=backend) or None,
    )
    with contextlib.suppress(KeyboardInterrupt):
        main_mod._run_web_admin(
            cfg,
            spool,
            fake_receiver,
            fake_forwarder,
            report_retriever=None,  # type: ignore[arg-type]
            port=18080,
        )
    assert "backend" in constructed, "win32 branch must construct the controller"
    assert isinstance(constructed["backend"], _StubBackend)


# ═══════════════════════════════════════════════════════════════════════
# Health-monitor lifespan (create_app owns the monitor's lifecycle)
# ═══════════════════════════════════════════════════════════════════════


class _RecordingMonitor:
    """Stand-in for ``DestinationHealthMonitor``.

    Records lifecycle calls and runs a real worker thread with the same
    loop shape (loop until flagged, parked on a wait between cycles), so the
    lifespan's start/stop *and* its join are both observable.
    """

    def __init__(self, config: object = None) -> None:  # noqa: ARG002
        self.start_calls = 0
        self.stop_calls = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._started = False

    def _run(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(0.02)

    def start(self) -> None:
        self.start_calls += 1
        if not self._started:
            self._started = True
            self._thread.start()

    def stop(self) -> None:
        self.stop_calls += 1
        self._stop.set()


class _BlockingMonitor:
    """A monitor whose worker parks inside a probe and cannot notice stop().

    ``stop()`` only flags the loop, so the worker only exits once the current
    probe returns — which is exactly the window the join exists to close.
    """

    def __init__(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.in_probe = threading.Event()
        self.release = threading.Event()

    def _run(self) -> None:
        self.in_probe.set()
        self.release.wait(5.0)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        pass


def test_lifespan_starts_and_stops_health_monitor(
    fake_receiver: FakeReceiver,
    fake_forwarder: FakeForwarder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entering the lifespan starts the monitor and constructs it on the way;
    leaving it stops the worker and joins, so no thread outlives the app."""
    constructed: list[_RecordingMonitor] = []
    monkeypatch.setattr(
        "mercure_gateway.web.pipeline.DestinationHealthMonitor",
        lambda config: constructed.append(_RecordingMonitor(config)) or constructed[-1],
    )
    app = create_app(default_config(), Spool(mem_database()))
    app.state.receiver = fake_receiver
    app.state.forwarder = fake_forwarder

    # Nothing is built until the lifespan runs — create_app does not eagerly
    # construct a monitor it cannot start.
    assert not hasattr(app.state, "health_monitor")
    assert constructed == []

    with TestClient(app) as client:
        assert len(constructed) == 1
        monitor = app.state.health_monitor
        assert monitor is constructed[0]
        assert monitor.start_calls == 1
        assert monitor._thread.is_alive()
        # The app serves requests while the monitor runs.
        assert client.get("/api/service").status_code == 200

    assert monitor.stop_calls == 1
    assert not monitor._thread.is_alive(), "shutdown must join the worker thread"


def test_lifespan_uses_a_preset_health_monitor_without_overwriting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A monitor already on ``app.state`` (the stub case in test_web_pipeline,
    or any caller that builds its own) is used, never replaced."""
    constructed: list[object] = []
    monkeypatch.setattr(
        "mercure_gateway.web.pipeline.DestinationHealthMonitor",
        lambda config: constructed.append(config),
    )
    app = create_app(default_config(), Spool(mem_database()))
    preset = _RecordingMonitor()
    app.state.health_monitor = preset

    with TestClient(app):
        assert app.state.health_monitor is preset
        assert constructed == [], "a preset monitor must short-circuit construction"

    assert app.state.health_monitor is preset
    assert preset.start_calls == 1
    assert preset.stop_calls == 1
    assert not preset._thread.is_alive()


def test_lifespan_starts_and_reaps_the_real_monitor_thread() -> None:
    """The real monitor, constructed by the lifespan, is started on enter and
    its worker is gone on exit — stop() alone only flags the loop."""
    # default_config has no destinations, so a probe cycle is a no-op and the
    # worker cannot touch the network.
    app = create_app(default_config(), Spool(mem_database()))
    with TestClient(app):
        monitor = app.state.health_monitor
        assert isinstance(monitor, DestinationHealthMonitor)
        assert monitor._thread is not None
        assert monitor._thread.is_alive()
    assert monitor is not None
    assert monitor._thread is not None
    assert not monitor._thread.is_alive()


def test_join_health_monitor_waits_for_the_worker_to_exit() -> None:
    """The join blocks until the worker actually leaves — a stop() that only
    flags the loop still yields a dead thread before shutdown returns."""
    monitor = _BlockingMonitor()
    monitor.start()
    assert monitor.in_probe.wait(2.0), "worker should be parked in a probe"

    released_at = time.monotonic()
    threading.Timer(0.25, monitor.release.set).start()
    _join_health_monitor(monitor)
    waited = time.monotonic() - released_at
    assert waited >= 0.2, f"join returned without waiting (waited {waited:.3f}s)"
    assert not monitor._thread.is_alive()


def test_join_health_monitor_is_bounded_and_logs_when_it_times_out(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A worker that never exits cannot hold shutdown hostage: the join gives
    up after the timeout, logs, and leaves the daemon thread behind."""
    import mercure_gateway.web as web_mod

    monitor = _BlockingMonitor()
    monitor.start()
    assert monitor.in_probe.wait(2.0)

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger=web_mod.__name__):
        _join_health_monitor(monitor, timeout=0.2)
    elapsed = time.monotonic() - started
    assert 0.2 <= elapsed < 2.0
    assert monitor._thread.is_alive(), "the survivor is left, not killed"
    assert any(
        "did not exit within" in record.message for record in caplog.records
    ), "a surviving thread must be logged"
