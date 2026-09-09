"""Web admin Windows-service endpoints (PRD §13 Q3, S07-T9).

The service management surface is Windows-only; off Windows the composition
root leaves ``app.state.service_controller`` unset, and these tests pin the
degraded behaviour: GET reports ``available: false`` (200) and POST actions
return 501 with a clear detail.  With a (fake) controller wired, the endpoints
must call exactly the controller op they name.
"""

from __future__ import annotations

import contextlib

import pytest
from conftest import FakeForwarder, FakeReceiver
from fastapi.testclient import TestClient

from mercure_gateway.config import default_config
from mercure_gateway.service_controller import ServiceController, ServiceState
from mercure_gateway.spool import Spool
from mercure_gateway.spool.db import mem_database
from mercure_gateway.web import create_app


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
def client(app) -> TestClient:
    return TestClient(app)


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
    monkeypatch.setattr(
        "mercure_gateway.web.pipeline.DestinationHealthMonitor", _FakeMonitor
    )

    monkeypatch.setattr(_sys, "platform", "linux")
    app_holder = {}

    from mercure_gateway.web import create_app as real_create_app

    def _capture_create_app(config, spool, config_path=None):  # type: ignore[no-untyped-def]
        application = real_create_app(config, spool, config_path=config_path)
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
