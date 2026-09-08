"""Hub reporting wiring (S08): audit sink → event streamer, registration.

Behaviors:
1. ``AuditLog.set_sink`` forwards every committed append to the hub streamer.
2. A failing sink never breaks the audit log (US-10 isolation invariant).
3. ``_start_hub_reporting`` returns ``(None, None)`` when disabled / under-keyed.
4. When enabled it starts the streamer, wires the sink, kicks off background
   registration and exposes a live hub-status dict for the web admin.
5. Registration state (ok / error) is reflected back into the status dict.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from mercure_gateway.audit import AuditLog
from mercure_gateway.config import default_config
from mercure_gateway.hub_client import RegistrationResult
from mercure_gateway.hub_events import HubEventStreamer
from mercure_gateway.main import _register_hub_in_background, _start_hub_reporting
from mercure_gateway.spool.db import mem_database

UID = "1.2.826.0.1.3680043.10.150.99"


def _wait_until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met within timeout")


@pytest.fixture()
def ok_response() -> MagicMock:
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    return resp


def _collector(posts: list[tuple[Any, dict[str, Any]]], resp: MagicMock) -> Any:
    def _post(*args: Any, **kwargs: Any) -> MagicMock:
        posts.append((args, kwargs))
        return resp

    return _post


# ── Audit sink → hub streamer ─────────────────────────────────────────────


def test_sink_forwards_committed_appends_to_streamer(
    monkeypatch: pytest.MonkeyPatch, ok_response: MagicMock
) -> None:
    posts: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(
        "mercure_gateway.hub_events.requests.post",
        _collector(posts, ok_response),
    )
    audit = AuditLog(mem_database())
    streamer = HubEventStreamer("https://hub.test", "k", "GW-1")
    streamer.start()
    audit.set_sink(lambda event, detail, _user: streamer.feed(event, detail))

    audit.append("STUDY_RECEIVED", {"study_uid": UID})
    audit.append("FORWARD_START", {"study_uid": UID})
    streamer.flush(timeout=2.0)
    streamer.stop()

    body = posts[-1][1]["json"]
    assert body["gateway"] == "GW-1"
    assert [e["event"] for e in body["events"]] == ["STUDY_RECEIVED", "FORWARD_START"]


def test_sink_compatible_with_feed_signature(
    monkeypatch: pytest.MonkeyPatch, ok_response: MagicMock
) -> None:
    """set_sink's 3-arg callback drives feed(event, detail) — via main's adapter."""
    posts: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(
        "mercure_gateway.hub_events.requests.post",
        _collector(posts, ok_response),
    )
    audit = AuditLog(mem_database())
    streamer = HubEventStreamer("https://hub.test", "k", "GW-1")
    streamer.start()
    audit.set_sink(lambda event, detail, _user: streamer.feed(event, detail))

    audit.append("STUDY_RECEIVED", {"study_uid": UID, "user": "someone"})
    streamer.flush(timeout=2.0)
    streamer.stop()

    assert posts[-1][1]["json"]["events"][0]["event"] == "STUDY_RECEIVED"


def test_failing_sink_does_not_break_append() -> None:
    audit = AuditLog(mem_database())

    def _boom(event: str, detail: dict[str, Any], user: str | None) -> None:
        raise RuntimeError("hub boom")

    audit.set_sink(_boom)
    rowid = audit.append("STUDY_RECEIVED", {"study_uid": UID}, user="unit")

    assert rowid == 1
    ok, errors = audit.verify()
    assert ok and not errors


# ── _start_hub_reporting gating ───────────────────────────────────────────


def test_start_hub_reporting_disabled_returns_none() -> None:
    cfg = default_config()
    cfg.audit.hub_reporting.enabled = False
    status, streamer = _start_hub_reporting(cfg, AuditLog(mem_database()))
    assert status is None
    assert streamer is None


def test_start_hub_reporting_missing_api_key_is_safely_off() -> None:
    cfg = default_config()
    cfg.audit.hub_reporting.enabled = True
    cfg.audit.hub_reporting.bookkeeper_url = "https://hub.test"
    status, streamer = _start_hub_reporting(cfg, AuditLog(mem_database()))
    assert status is None
    assert streamer is None


# ── _start_hub_reporting enabled path ─────────────────────────────────────


def test_start_hub_reporting_wires_streamer_sink_and_registration(
    monkeypatch: pytest.MonkeyPatch, ok_response: MagicMock
) -> None:
    posts: list[tuple[Any, dict[str, Any]]] = []
    # hub_client and hub_events share the same `requests` module, so patch it
    # once and route by URL.
    monkeypatch.setattr("requests.post", _collector(posts, ok_response))

    cfg = default_config()
    cfg.general.appliance_name = "GW-1"
    cfg.audit.hub_reporting.enabled = True
    cfg.audit.hub_reporting.bookkeeper_url = "https://hub.test"
    cfg.audit.hub_reporting.api_key = "secret"
    audit = AuditLog(mem_database())

    status, streamer = _start_hub_reporting(cfg, audit)

    try:
        assert streamer is not None and streamer.is_running
        # Background registration completes and mutates the same status dict.
        _wait_until(lambda: status["registering"] is False)
        assert status["registered"] is True
        assert status["streaming"] is True
        assert status["bookkeeper_url"] == "https://hub.test"
        registration = [u for u, _ in posts if u[0].endswith("/register-gateway")]
        assert registration, "registration POST never issued"

        # A pipeline audit event is streamed to the bookkeeper untouched.
        audit.append("STUDY_RECEIVED", {"study_uid": UID})
        streamer.flush(timeout=2.0)
        streamed = [
            e["event"] for u, k in posts if u[0].endswith("/events") for e in k["json"]["events"]
        ]
        assert "STUDY_RECEIVED" in streamed
    finally:
        streamer.stop()


def test_start_hub_reporting_status_tracks_registration_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "mercure_gateway.hub_client.requests.post",
        _collector([], MagicMock(ok=False, status_code=403)),
    )
    cfg = default_config()
    cfg.audit.hub_reporting.enabled = True
    cfg.audit.hub_reporting.bookkeeper_url = "https://hub.test"
    cfg.audit.hub_reporting.api_key = "secret"

    status, streamer = _start_hub_reporting(cfg, AuditLog(mem_database()))

    try:
        _wait_until(lambda: status["registering"] is False)
        assert status["registered"] is False
    finally:
        streamer.stop()


# ── _register_hub_in_background ───────────────────────────────────────────


def test_registration_ok_updates_status() -> None:
    class _FakeClient:
        def register(self) -> RegistrationResult:
            return RegistrationResult(ok=True)

    status = {"registered": False, "registering": True}
    _register_hub_in_background(status, _FakeClient())
    _wait_until(lambda: status["registering"] is False)
    assert status["registered"] is True


def test_registration_exception_sets_error() -> None:
    class _BoomClient:
        def register(self) -> RegistrationResult:
            raise ConnectionError("bookkeeper unreachable")

    status = {"registered": False, "registering": True}
    _register_hub_in_background(status, _BoomClient())
    _wait_until(lambda: status["registering"] is False)
    assert status["registered"] is False
    assert "error" in status
