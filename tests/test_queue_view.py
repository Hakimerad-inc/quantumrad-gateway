"""S06-T3 (RED): Web admin queue view — pagination service (product refinement §7.2).

The queue view needs a paginated studies endpoint with stable filtering and
metadata (total, page, page_size) so the SPA can render page controls.  The
PRD §5.6 gate requires a 10k-row list to render within the latency budget.

Behaviors:
1. ``GET /api/studies`` returns pagination metadata + items
2. page_size is honored; page offset advances correctly
3. state/modality filters stay consistent across pages
4. total reflects the filtered count
5. a 10k-row list renders within the §5.6 latency budget (500 ms)
"""

from __future__ import annotations

import time

import pytest
from conftest import FakeForwarder, FakeReceiver
from fastapi.testclient import TestClient

from mercure_gateway.config import default_config
from mercure_gateway.spool import Spool
from mercure_gateway.spool.db import mem_database
from mercure_gateway.web import create_app


@pytest.fixture()
def spool() -> Spool:
    return Spool(mem_database())


@pytest.fixture()
def app(spool: Spool, fake_receiver: FakeReceiver, fake_forwarder: FakeForwarder):
    cfg = default_config()
    application = create_app(cfg, spool)
    application.state.receiver = fake_receiver
    application.state.forwarder = fake_forwarder
    return application


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app)


# ══════════════════════════════════════════════════════════════════════
# Pagination metadata  (§7.2)
# ══════════════════════════════════════════════════════════════════════

def test_studies_response_has_pagination_metadata(client: TestClient, spool: Spool) -> None:
    spool.receive("1.2.840.1")
    spool.receive("1.2.840.2")
    r = client.get("/api/studies")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 2
    assert data["page"] == 1
    assert data["page_size"] == 50
    assert len(data["items"]) == 2


def test_studies_pagination_page_size_honored(client: TestClient, spool: Spool) -> None:
    for i in range(5):
        spool.receive(f"1.2.840.{i}")
    r = client.get("/api/studies?page_size=2")
    data = r.json()
    assert len(data["items"]) == 2
    assert data["total"] == 5

    r2 = client.get("/api/studies?page=2&page_size=2")
    data2 = r2.json()
    assert len(data2["items"]) == 2
    assert data2["page"] == 2
    # page 3 holds the final row
    r3 = client.get("/api/studies?page=3&page_size=2")
    assert len(r3.json()["items"]) == 1


def test_studies_filter_consistent_across_pages(client: TestClient, spool: Spool) -> None:
    for i in range(6):
        spool.receive(f"1.2.840.{i}", modality="CT" if i % 2 == 0 else "MR")
    r = client.get("/api/studies?modality=CT&page_size=2")
    data = r.json()
    assert data["total"] == 3
    assert len(data["items"]) == 2
    assert all(s["modality"] == "CT" for s in data["items"])


def test_studies_filter_by_state_metadata(client: TestClient, spool: Spool) -> None:
    spool.receive("1.2.840.1")
    spool.receive("1.2.840.2")
    r = client.get("/api/studies?state=RECEIVED")
    data = r.json()
    assert data["total"] == 2
    assert all(s["state"] == "RECEIVED" for s in data["items"])


def test_studies_empty_result_metadata(client: TestClient) -> None:
    r = client.get("/api/studies")
    data = r.json()
    assert data["total"] == 0
    assert data["items"] == []


# ══════════════════════════════════════════════════════════════════════
# Latency budget  (§5.6 — 10k rows ≤ 500 ms)
# ══════════════════════════════════════════════════════════════════════

def test_ten_k_studies_list_within_latency_budget(client: TestClient, spool: Spool) -> None:
    """§5.6: a 10k-row queue must list within the 500 ms budget."""
    with spool._db._lock:
        conn = spool._db._conn
        conn.executemany(
            "INSERT INTO studies (study_uid, patient_name, modality, state, created_at) "
            "VALUES (?, ?, ?, 'RECEIVED', datetime('now'))",
            [(f"1.2.840.1.{i}", f"P{i:05d}", "CT" if i % 2 == 0 else "MR") for i in range(10000)],
        )
    start = time.perf_counter()
    r = client.get("/api/studies?page_size=50")
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 10000
    assert len(data["items"]) == 50
    assert elapsed_ms < 500, f"§5.6 budget exceeded: {elapsed_ms:.1f} ms"
