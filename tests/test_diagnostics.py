"""S09-T5 (RED): Diagnostics bundle export (PRD §7, §2.3).

One-click support bundle from the web admin panel: redacted config +
structured logs + spool summary, with secrets redacted (reuses the S04-T2
redaction).  Bundles are served as downloadable JSON.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from mercure_gateway.audit import AuditLog
from mercure_gateway.config import SFTPDestination, default_config
from mercure_gateway.spool import Spool
from mercure_gateway.spool.db import mem_database
from mercure_gateway.web import create_app


@pytest.fixture()
def spool() -> Spool:
    return Spool(mem_database())


@pytest.fixture()
def client(spool: Spool) -> TestClient:
    cfg = default_config()
    cfg.destinations.append(
        SFTPDestination(
            name="sftp",
            host="sftp.local",
            port=22,
            username="user",
            password="s3cret",
        )
    )
    application = create_app(cfg, spool)
    application.state.text_log_path = None
    return TestClient(application)


# ══════════════════════════════════════════════════════════════════════
# Bundle endpoint
# ══════════════════════════════════════════════════════════════════════

def test_diagnostics_endpoint_returns_json(client: TestClient) -> None:
    """GET /api/diagnostics/export returns a downloadable JSON bundle."""
    r = client.get("/api/diagnostics/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert "attachment" in r.headers.get("content-disposition", "")


def test_diagnostics_bundle_has_all_sections(client: TestClient) -> None:
    """The bundle contains config, audit and spool summary sections."""
    r = client.get("/api/diagnostics/export")
    bundle = r.json()
    assert "config" in bundle
    assert "audit" in bundle
    assert "spool" in bundle
    assert "generated_at" in bundle


def test_diagnostics_bundle_config_secrets_redacted(client: TestClient) -> None:
    """Destination secrets are redacted (never appear in the bundle)."""
    r = client.get("/api/diagnostics/export")
    bundle = r.json()
    serialized = json.dumps(bundle)
    assert "s3cret" not in serialized
    for dest in bundle["config"].get("destinations", []):
        for key in ("password", "private_key", "passphrase", "auth_token"):
            if dest.get(key):
                assert dest[key] == "***"


def test_diagnostics_bundle_includes_audit_events(client: TestClient, spool: Spool) -> None:
    """Audit events (structured) are included for support triage."""
    AuditLog(spool._db).append("STUDY_RECEIVED", {"study_uid": "1.2.3.4"})
    r = client.get("/api/diagnostics/export")
    bundle = r.json()
    assert bundle["audit"]["count"] >= 1
    assert bundle["audit"]["head_hash"]


def test_diagnostics_bundle_includes_spool_summary(client: TestClient, spool: Spool) -> None:
    """The spool section summarises the queue by state."""
    spool.receive("1.2.3.4", accession="ACC-001", modality="CT")
    r = client.get("/api/diagnostics/export")
    bundle = r.json()
    spool_summary = bundle["spool"]
    assert "total" in spool_summary
    assert spool_summary["total"] >= 1
    assert "states" in spool_summary