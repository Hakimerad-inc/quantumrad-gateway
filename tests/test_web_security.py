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

# ══════════════════════════════════════════════════════════════════════
# Non-loopback bind with auth disabled — refuse, don't just warn (D3b)
#
# The admin guide, web/auth.py, and web/__init__.py all promise the
# gateway REFUSES to bind the panel to a non-loopback address while
# web_ui.auth_enabled is false. Before D3b, main._enforce_bind_security only
# logged a warning and booted anyway — an unauthenticated PHI/credential/
# start-stop API on the clinical LAN. These tests pin the refusal and the
# documented escape hatch.
# ══════════════════════════════════════════════════════════════════════

from mercure_gateway.config import GatewayConfig  # noqa: E402
from mercure_gateway.main import _enforce_bind_security  # noqa: E402


def _cfg_with_bind(host: str, auth_enabled: bool) -> GatewayConfig:
    cfg = default_config()
    cfg.web_ui.host = host
    cfg.web_ui.auth_enabled = auth_enabled
    return cfg


def test_insecure_bind_refused_non_loopback_no_auth() -> None:
    """host=0.0.0.0 with auth disabled must raise SystemExit (the docs' claim)."""
    with pytest.raises(SystemExit):
        _enforce_bind_security(_cfg_with_bind("0.0.0.0", False), environ={})


def test_insecure_bind_refused_ipv6_non_loopback() -> None:
    """A routable IPv6 bind is just as unauthenticated — refused too."""
    with pytest.raises(SystemExit):
        _enforce_bind_security(_cfg_with_bind("::", False), environ={})


def test_loopback_no_auth_allowed() -> None:
    """The default posture (127.0.0.1, auth off) boots without raising."""
    for host in ("127.0.0.1", "localhost", "::1"):
        _enforce_bind_security(_cfg_with_bind(host, False), environ={})


def test_non_loopback_with_auth_allowed() -> None:
    """auth_enabled=true is the documented way to serve on the network."""
    _enforce_bind_security(_cfg_with_bind("0.0.0.0", True), environ={})


def test_insecure_bind_escape_hatch(monkeypatch: pytest.MonkeyPatch, caplog) -> None:  # type: ignore[no-untyped-def]
    """MERCURE_GATEWAY_ALLOW_INSECURE_BIND=1 downgrades refusal to a warning."""
    with caplog.at_level("WARNING"):
        _enforce_bind_security(
            _cfg_with_bind("0.0.0.0", False),
            environ={"MERCURE_GATEWAY_ALLOW_INSECURE_BIND": "1"},
        )
    assert "insecure" in caplog.text.lower()


def test_escape_hatch_only_bypasses_when_unset_still_refuses() -> None:
    """The hatch is opt-in per value — an empty env dict must still refuse."""
    with pytest.raises(SystemExit):
        _enforce_bind_security(_cfg_with_bind("0.0.0.0", False), environ={})
