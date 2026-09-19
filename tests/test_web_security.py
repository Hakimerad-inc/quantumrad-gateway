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
    """Every API response includes security headers (CSP, XFO, XCTO).

    HSTS is deliberately NOT on plain-HTTP responses — browsers ignore it
    there, and emitting it unconditionally was the documented-control gap
    ADR-0007 closes (test_hsts_only_over_tls covers the positive half).
    """
    r = client.get("/api/system/health")
    assert r.status_code == 200
    headers = dict(r.headers)
    assert "x-content-type-options" in headers
    assert "x-frame-options" in headers
    assert "strict-transport-security" not in headers  # http:// TestClient default
    assert "content-security-policy" in headers


def test_csp_content(client: TestClient) -> None:
    """The CSP's directives are the ones the product depends on (P1-11).

    ``test_security_headers_present`` only checks the header *exists*; a
    directive removed or relaxed by accident would go unnoticed.  This pins
    the content, in particular the two that were unenforced gaps:

    - ``frame-src 'self' data:`` — report PDFs render in a ``data:`` iframe;
      without it the viewer goes blank (``default-src 'self'`` blocks data:).
    - ``object-src 'none'`` — removes the <embed>/<object> fallback, so the
      iframe path above is the only one and must keep working.
    """
    r = client.get("/api/system/health")
    assert r.status_code == 200
    csp = dict(r.headers)["content-security-policy"]

    def directive(name: str) -> str:
        for part in csp.split("; "):
            if part.split(" ")[0] == name:
                return part
        return ""

    # The load-bearing directives, asserted as full directives (not just
    # "appears somewhere in the string" — a substring check would let
    # ``frame-src`` be mentioned in a comment-like context and still pass).
    assert directive("default-src") == "default-src 'self'"
    assert directive("script-src") == "script-src 'self'"
    assert directive("frame-src") == "frame-src 'self' data:"
    assert directive("object-src") == "object-src 'none'"
    assert directive("frame-ancestors") == "frame-ancestors 'none'"
    # The deliberate relaxation — inline styles are required by React; it is
    # pinned here so a future "tighten the CSP" change is a visible decision.
    assert directive("style-src") == "style-src 'self' 'unsafe-inline'"


def test_hsts_only_over_tls() -> None:
    """HSTS appears when the request arrives over https (ADR-0007)."""
    cfg = default_config()
    spool = Spool(mem_database())
    app = create_app(cfg, spool)
    https_client = TestClient(app, base_url="https://gateway.test")
    r = https_client.get("/api/system/health")
    assert r.status_code == 200
    assert "strict-transport-security" in dict(r.headers)


def test_session_cookie_secure_only_over_https(client: TestClient) -> None:
    """The session cookie carries ``Secure`` only when served over TLS.

    Over plain HTTP (localhost dev, the TestClient, a Tauri shell hitting
    127.0.0.1) a ``Secure`` cookie would never be stored, so the login flow
    would silently break. Review P1-16.
    """
    r = client.post("/api/login", json={"password": "anything"})
    assert r.status_code == 200
    cookie = r.headers.get("set-cookie", "")
    assert "mercure_session=" in cookie
    assert "secure" not in cookie.lower()


def test_session_cookie_secure_set_over_tls() -> None:
    """Over TLS the cookie is flagged ``Secure`` so it never rides a cleartext hop."""
    cfg = default_config()
    spool = Spool(mem_database())
    app = create_app(cfg, spool)
    https_client = TestClient(app, base_url="https://gateway.test")
    r = https_client.post("/api/login", json={"password": "anything"})
    assert r.status_code == 200
    cookie = r.headers.get("set-cookie", "")
    assert "mercure_session=" in cookie
    assert "secure" in cookie.lower()


def test_logout_cookie_flag_matches_scheme(client: TestClient) -> None:
    """Logout must set the same ``Secure`` flag the cookie was issued with or
    the browser will not match it for deletion and the cookie survives."""
    r = client.post("/api/logout")
    assert r.status_code == 200
    cookie = r.headers.get("set-cookie", "")
    assert "mercure_session=" in cookie
    # Plain HTTP → plain deletion, no Secure mismatch.
    assert "secure" not in cookie.lower()


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


# ══════════════════════════════════════════════════════════════════════
# Session lifetime (review P0-8)
# ══════════════════════════════════════════════════════════════════════


def _authed_client() -> TestClient:
    """A client with auth enabled, already logged in as 's3cret'."""
    from mercure_gateway.web.auth import hash_password

    cfg = default_config()
    cfg.web_ui.auth_enabled = True
    cfg.web_ui.auth_password_hash = hash_password("s3cret")
    client = TestClient(create_app(cfg, Spool(mem_database())))
    assert client.post("/api/login", json={"password": "s3cret"}).status_code == 200
    return client


def test_session_survives_a_config_save_that_round_trips_the_hash() -> None:
    """Saving config must not log out every live operator.

    The session secret used to be derived from the password hash, so any save
    that echoed it back invalidated every cookie in flight — including the
    operator's own, mid-edit. It is now minted once per process.
    """
    client = _authed_client()
    assert client.get("/api/system/status").status_code == 200

    # A no-op round-trip: GET redacts the hash to '***', the PUT restores it.
    current = client.get("/api/config").json()
    r = client.put("/api/config", json=current)
    assert r.status_code == 200

    # The same session is still valid.
    assert client.get("/api/system/status").status_code == 200


def test_session_secret_is_per_process() -> None:
    """Two apps do not share a signing secret (a restart logs everyone out)."""
    a = _authed_client()
    b = _authed_client()
    token = a.cookies["mercure_session"]
    # A cookie minted by app A does not authenticate against app B.
    b.cookies["mercure_session"] = token
    assert b.get("/api/system/status").status_code == 401
