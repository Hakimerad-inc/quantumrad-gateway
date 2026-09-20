"""Tests for the pipeline visualization endpoints (flow view).

Follows the fixture style of test_web_api.py: in-memory spool + fake
receiver/forwarder + FastAPI TestClient. The destination health monitor is
never started here — tests inject a canned snapshot via a stub monitor.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from mercure_gateway.config import DICOMDestination, default_config
from mercure_gateway.spool import Spool
from mercure_gateway.spool.db import mem_database
from mercure_gateway.web import create_app
from mercure_gateway.web.pipeline import RouteRollupCache, _rollup_cache
from tests.conftest import FakeForwarder, FakeReceiver


class _StubMonitor:
    """Stub health monitor returning a canned snapshot."""

    def __init__(self, snapshot: dict[str, dict[str, Any]]) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return self._snapshot


@pytest.fixture()
def spool() -> Spool:
    return Spool(mem_database())


@pytest.fixture()
def app(spool: Spool, fake_receiver: FakeReceiver, fake_forwarder: FakeForwarder):
    cfg = default_config()
    cfg.destinations = [
        DICOMDestination(
            name="hub", type="dicom", host="hub.local", port=11112, aet_target="MERCURE"
        )
    ]
    application = create_app(cfg, spool)
    application.state.receiver = fake_receiver
    application.state.forwarder = fake_forwarder
    application.state.health_monitor = _StubMonitor(
        {"hub": {"status": "ok", "checked_at": "2026-08-31T00:00:00+00:00",
                 "latency_ms": 12}}
    )
    return application


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app)


def test_pipeline_snapshot_shape(spool: Spool, app, client: TestClient) -> None:
    app.state.receiver.start()
    app.state.forwarder.start()
    study_id = spool.receive("1.2.3.4", accession="A1", modality="CT")
    spool.enqueue(study_id, [app.state.config.destinations[0]])
    spool.claim_next(limit=1)

    resp = client.get("/api/pipeline")
    assert resp.status_code == 200
    body = resp.json()
    assert body["components"]["receiver"] is True
    assert body["components"]["forwarder"] is True
    assert body["queue"]["sending"] == 1
    assert body["receiver_counts"]["received_last_hour"] >= 1
    assert len(body["destinations"]) == 1
    dest = body["destinations"][0]
    assert dest["name"] == "hub"
    assert dest["host"] == "hub.local"
    assert dest["aet"] == "MERCURE"
    assert dest["routes"]["sending"] == 1
    assert dest["health"]["status"] == "ok"


def test_pipeline_rollup_is_memoized_but_stays_correct(
    spool: Spool, app, client: TestClient
) -> None:
    """The route rollup GROUP BY runs once per TTL window, not once per poll."""
    study_id = spool.receive("1.2.3.4")
    hub = app.state.config.destinations[0]

    def hub_routes() -> dict[str, int]:
        return client.get("/api/pipeline").json()["destinations"][0]["routes"]

    assert hub_routes()["waiting"] == 0

    spool.enqueue(study_id, [hub])
    # Inside the memo window the freshly-enqueued route is not yet visible…
    assert hub_routes()["waiting"] == 0
    # …and shows up as soon as the memo is dropped.
    _rollup_cache(spool.database).invalidate()
    assert hub_routes()["waiting"] == 1


def test_rollup_cache_reuses_within_ttl() -> None:
    cache = RouteRollupCache(ttl_sec=5.0)
    loads: list[list[dict[str, Any]]] = []

    def loader() -> list[dict[str, Any]]:
        rows = [{"target_name": f"t{len(loads)}"}]
        loads.append(rows)
        return rows

    first = cache.get(loader)
    second = cache.get(loader)
    assert first is second
    assert len(loads) == 1

    cache.invalidate()
    third = cache.get(loader)
    assert third is not first
    assert len(loads) == 2


def test_rollup_cache_expires_after_ttl() -> None:
    cache = RouteRollupCache(ttl_sec=0.0)
    loads: list[list[dict[str, Any]]] = []

    def loader() -> list[dict[str, Any]]:
        rows = [{"target_name": f"t{len(loads)}"}]
        loads.append(rows)
        return rows

    assert cache.get(loader) is not cache.get(loader)
    assert len(loads) == 2


def test_rollup_cache_is_per_database(spool: Spool) -> None:
    """Two spools in one process must not share a rollup memo."""
    other = Spool(mem_database())
    assert _rollup_cache(spool.database) is _rollup_cache(spool.database)
    assert _rollup_cache(spool.database) is not _rollup_cache(other.database)


def test_destinations_summary(client: TestClient) -> None:
    resp = client.get("/api/destinations")
    assert resp.status_code == 200
    dests = resp.json()
    assert dests == [
        {"name": "hub", "type": "dicom", "enabled": True,
         "host": "hub.local", "port": 11112, "aet": "MERCURE"}
    ]


def test_destination_studies(client: TestClient, spool: Spool, app) -> None:
    study_id = spool.receive("1.2.3.4", accession="A1", modality="CT")
    spool.enqueue(study_id, [app.state.config.destinations[0]])
    resp = client.get("/api/destinations/hub/studies")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["accession"] == "A1"
    assert rows[0]["study_uid"] == "1.2.3.4"
    assert client.get("/api/destinations/nope/studies").json() == []


def test_study_detail_next_retry_computed(client: TestClient, spool: Spool, app) -> None:
    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [app.state.config.destinations[0]])
    spool.claim_next(limit=1)
    spool.fail(study_id, "hub", error="boom")

    resp = client.get(f"/api/studies/{study_id}/detail")
    assert resp.status_code == 200
    body = resp.json()
    route = body["routes"][0]
    assert route["status"] == "error"
    assert route["attempts"] >= 1
    # 5 * 2**(attempt-1) — first failure → 5s backoff
    assert route["next_retry_sec"] == 5.0 * (2 ** (route["attempts"] - 1))


def test_study_detail_404(client: TestClient) -> None:
    assert client.get("/api/studies/9999/detail").status_code == 404


def test_study_timeline(client: TestClient, spool: Spool, app) -> None:
    from mercure_gateway.audit import AuditLog

    spool._audit = AuditLog(spool._db)
    study_id = spool.receive("1.2.840.10008.99.1")
    spool.enqueue(study_id, [app.state.config.destinations[0]])

    resp = client.get(f"/api/studies/{study_id}/timeline")
    assert resp.status_code == 200
    events = resp.json()
    names = {e["event"] for e in events}
    assert "STUDY_RECEIVED" in names
    assert "STUDY_QUEUED" in names
    # detail arrives parsed as an object, not a JSON string
    received = next(e for e in events if e["event"] == "STUDY_RECEIVED")
    assert isinstance(received["detail"], dict)


def test_study_timeline_404(client: TestClient) -> None:
    assert client.get("/api/studies/9999/timeline").status_code == 404
