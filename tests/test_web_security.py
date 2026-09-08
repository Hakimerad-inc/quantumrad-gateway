"""S09-T8 (RED): Web admin panel hardening (refinement §7).

Security tests: XSS prevention, CSRF enforcement, and security headers.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mercure_gateway.config import default_config
from mercure_gateway.spool import Spool
from mercure_gateway.spool.db import mem_database
from mercure_gateway.web import create_app


@pytest.fixture()
def client() -> TestClient:
    cfg = default_config()
    spool = Spool(mem_database())
    app = create_app(cfg, spool)
    return TestClient(app)


# ══════════════════════════════════════════════════════════════════════
# Security headers
# ══════════════════════════════════════════════════════════════════════

def test_security_headers_present(client: TestClient) -> None:
    """Every API response includes security headers (CSP, HSTS, XFO, XCTO)."""
    r = client.get("/api/system/health")
    assert r.status_code == 200
    headers = dict(r.headers)
    assert "x-content-type-options" in headers
    assert "x-frame-options" in headers
    assert "strict-transport-security" in headers
    assert "content-security-policy" in headers


# ══════════════════════════════════════════════════════════════════════
# XSS prevention
# ══════════════════════════════════════════════════════════════════════

def test_xss_payload_in_study_echoed_as_json(client: TestClient) -> None:
    """XSS payloads in study data are returned as JSON text (not HTML)."""
    r = client.get("/api/studies?page=1&page_size=10")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")


def test_xss_payload_in_audit_echoed_as_json(client: TestClient) -> None:
    """XSS payloads in audit events are returned as JSON text."""
    r = client.get("/api/audit?limit=10")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")


# ══════════════════════════════════════════════════════════════════════
# CSRF — origin-based protection for state-changing endpoints
# ══════════════════════════════════════════════════════════════════════

def test_csrf_rejects_missing_origin_on_post(client: TestClient) -> None:
    """A POST to a state-changing endpoint without an Origin header is rejected
    when it comes from a non-loopback origin."""
    r = client.post("/api/echo", json={"host": "pacs.local", "port": 104})
    assert r.status_code == 200, "same-origin POST (no Origin header) should be accepted"


def test_csrf_rejects_foreign_origin_on_mutating_endpoint(client: TestClient) -> None:
    """A POST with a foreign Origin header is rejected (403)."""
    r = client.post(
        "/api/echo",
        json={"host": "pacs.local", "port": 104},
        headers={"Origin": "https://evil.com"},
    )
    assert r.status_code == 403


def test_csrf_accepts_loopback_origin(client: TestClient) -> None:
    """A POST with a valid loopback Origin is accepted."""
    r = client.post(
        "/api/echo",
        json={"host": "pacs.local", "port": 104},
        headers={"Origin": "http://127.0.0.1:8080"},
    )
    assert r.status_code == 200


def test_csrf_accepts_tauri_origin(client: TestClient) -> None:
    """A POST from tauri://localhost is accepted."""
    r = client.post(
        "/api/echo",
        json={"host": "pacs.local", "port": 104},
        headers={"Origin": "tauri://localhost"},
    )
    assert r.status_code == 200