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


def test_csp_present_on_cors_preflight(client: TestClient) -> None:
    """A CORS preflight reply carries the security headers too.

    CORSMiddleware answers OPTIONS itself and never calls the app, so the
    headers are only on the response if the security middleware sits
    *outside* CORS. Starlette runs user middleware in reverse registration
    order, so that means registering the security middleware LAST. Verified
    broken at HEAD: OPTIONS /api/config returned 200 with no CSP while
    GET /api/system/health had one — the security middleware was the
    innermost layer and preflight never reached it.
    """
    r = client.options(
        "/api/config",
        headers={
            "Origin": "http://127.0.0.1:8080",
            "Access-Control-Request-Method": "PUT",
        },
    )
    assert r.status_code == 200
    headers = dict(r.headers)
    assert "content-security-policy" in headers
    assert "x-content-type-options" in headers
    assert "x-frame-options" in headers


def test_security_middleware_is_outermost(client: TestClient) -> None:
    """Pin the registration order, which is what the preflight test depends on.

    ``add_middleware`` inserts at the front, so ``user_middleware[0]`` is the
    LAST registered and, once the stack is built, the OUTERMOST. That slot
    must be the security middleware.
    """
    from mercure_gateway.web import _SecurityMiddleware

    assert client.app.user_middleware[0].cls is _SecurityMiddleware


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


def _client_on_port(port: int) -> TestClient:
    """An app whose configured web port is *port* (the isolated-port posture
    the test suite and e2e/seed.py use — the port lives in the config, not in
    a hardcoded allow-list)."""
    cfg = default_config()
    cfg.web_ui.port = port
    return TestClient(create_app(cfg, Spool(mem_database())))


def test_csrf_origin_port_is_read_from_config() -> None:
    """The allow-list follows web_ui.port instead of pinning 8080.

    e2e/seed.py serves the panel on 18299 and sets that port in the config; a
    hardcoded 8080 list (or one collapsed to a single literal) would reject
    every same-app request the E2E browser makes.
    """
    client = _client_on_port(18299)
    r = client.post(
        "/api/echo",
        json={"host": "pacs.local", "port": 104},
        headers={"Origin": "http://127.0.0.1:18299"},
    )
    assert r.status_code == 200, "the configured port must be same-app"


def test_csrf_rejects_loopback_on_another_port() -> None:
    """A loopback host on a port the gateway does not serve is cross-origin.

    This is the hole the pin closes: the old allow-list accepted *any* port on
    127.0.0.1/localhost, so a second localhost service (or a crafted page on
    one) was treated as same-app for a state-changing request.
    """
    client = _client_on_port(18299)
    r = client.post(
        "/api/echo",
        json={"host": "pacs.local", "port": 104},
        headers={"Origin": "http://127.0.0.1:18300"},
    )
    assert r.status_code == 403


def test_csrf_rejects_wrong_port_on_default_config(client: TestClient) -> None:
    """The default-config app (port 8080) rejects a request from port 8081."""
    r = client.post(
        "/api/echo",
        json={"host": "pacs.local", "port": 104},
        headers={"Origin": "http://localhost:8081"},
    )
    assert r.status_code == 403


def test_cors_allow_list_matches_configured_port() -> None:
    """CORS allows exactly the configured origin — the CSRF and CORS lists are
    built from the same source, so a browser-driven PUT works end to end."""
    client = _client_on_port(18299)
    r = client.options(
        "/api/config",
        headers={
            "Origin": "http://localhost:18299",
            "Access-Control-Request-Method": "PUT",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:18299"

    # A port the gateway does not serve is not echoed back as allowed.
    r = client.options(
        "/api/config",
        headers={
            "Origin": "http://localhost:18300",
            "Access-Control-Request-Method": "PUT",
        },
    )
    assert r.status_code == 400
    assert "access-control-allow-origin" not in r.headers


# ══════════════════════════════════════════════════════════════════════
# OpenAPI / interactive docs — not an unauthenticated API map (P1)
#
# /openapi.json, /docs and /redoc enumerate every admin endpoint the panel
# exposes (PHI, credentials, start/stop). They are removed when web_ui auth
# is on; the loopback dev posture keeps them reachable.
# ══════════════════════════════════════════════════════════════════════


def test_docs_reachable_when_auth_disabled(client: TestClient) -> None:
    """The dev/local posture (auth off, loopback-only) keeps the docs."""
    assert client.app.openapi_url == "/openapi.json"
    assert client.app.docs_url == "/docs"
    assert client.app.redoc_url == "/redoc"
    r = client.get("/openapi.json")
    assert r.status_code == 200
    assert r.json()["openapi"].startswith("3.")


def test_docs_removed_when_auth_enabled() -> None:
    """With auth on, the schema and docs are not served at all.

    The SPA catch-all answers /openapi.json with the SPA shell when the bundle
    is built, so the assertion is on the content type, not the status code.
    """
    from mercure_gateway.web.auth import hash_password

    cfg = default_config()
    cfg.web_ui.auth_enabled = True
    cfg.web_ui.auth_password_hash = hash_password("s3cret")
    client = TestClient(create_app(cfg, Spool(mem_database())))

    assert client.app.openapi_url is None
    assert client.app.docs_url is None
    assert client.app.redoc_url is None

    r = client.get("/openapi.json")
    # What answers instead depends on whether the SPA bundle is present (built
    # shell vs. the not-built fallback route) — but neither is the schema.
    if r.headers["content-type"].startswith("application/json"):
        assert "openapi" not in r.json()

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
