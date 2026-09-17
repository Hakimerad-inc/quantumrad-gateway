"""TDD: RED tests for the web admin REST API (product refinement §7).

All 17 endpoints are tested against the FastAPI app using TestClient.
The tests create in-memory spool databases and exercise every endpoint
with real data.

Endpoints tested (§7):
  System:   GET /status, GET /health, POST /start, POST /stop
  Queue:    GET /queue/stats, GET /studies, GET /studies/{id},
            GET /studies/{id}/routes, POST /studies/{id}/retry
  Config:   GET /config, PUT /config, GET /config/export, POST /config/import
  Reports:  GET /reports, GET /reports/{id}, POST /reports/{id}/refresh,
            GET /reports/{id}/content
  Audit:    GET /audit, GET /audit/verify, GET /audit/export
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Shared fakes from conftest.py
from conftest import FakeForwarder, FakeReceiver
from fastapi.testclient import TestClient

from mercure_gateway.config import DICOMDestination, default_config
from mercure_gateway.spool import Spool, StudyState
from mercure_gateway.spool.db import mem_database
from mercure_gateway.web import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def spool() -> Spool:
    # The app fixture below builds its config from the same default; the
    # spool must carry it too — endpoints like /enqueue route via
    # spool._config.destinations, not app.state.config.
    return Spool(mem_database(), default_config())


@pytest.fixture()
def app(spool: Spool, fake_receiver: FakeReceiver, fake_forwarder: FakeForwarder):
    """Create a FastAPI app with in-memory spool and fake receiver/forwarder."""
    cfg = spool._config or default_config()
    application = create_app(cfg, spool)
    application.state.receiver = fake_receiver
    application.state.forwarder = fake_forwarder
    return application


class _NoopRetriever:
    """Minimal report retriever stub returning 'retrieved' (S06-T2)."""

    def retrieve(self, report_id: int) -> str:
        return "retrieved"


@pytest.fixture()
def app_with_retriever(app, spool: Spool):
    """App with a stub report retriever wired for refresh/request endpoints."""
    app.state.report_retriever = _NoopRetriever()
    return app


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app)


# ══════════════════════════════════════════════════════════════════════
# System endpoints  (§7.5)
# ══════════════════════════════════════════════════════════════════════


def test_system_status(client: TestClient) -> None:
    r = client.get("/api/system/status")
    assert r.status_code == 200
    data = r.json()
    assert data["version"]
    assert "uptime_sec" in data
    assert data["receiver"] in ("running", "stopped")
    assert "hub_registered" in data
    assert "hub_streaming" in data


def test_system_status_hub_fields_populated(app, client: TestClient) -> None:
    """Hub registration/streaming status is surfaced when wired (S08-T8)."""
    app.state.hub_status = {"registered": True, "streaming": True}
    r = client.get("/api/system/status")
    data = r.json()
    assert data["hub_registered"] is True
    assert data["hub_streaming"] is True

    app.state.hub_status = {"registered": False, "streaming": False}
    r = client.get("/api/system/status")
    data = r.json()
    assert data["hub_registered"] is False
    assert data["hub_streaming"] is False


def test_system_status_reports_pending_restart(client: TestClient) -> None:
    """A saved config that the running components do not use yet is flagged.

    The client-side "restart required" flag resets on page reload; without a
    server-side signal an operator can reload, see no banner, and believe a
    saved change is live. The startup snapshot is the config the receiver/
    forwarder were built with, so a differing saved config is the truth.
    """
    from mercure_gateway.config import DICOMDestination

    r = client.get("/api/system/status")
    assert r.json()["config_pending_restart"] is False

    # Save a change. app.state.config is refreshed, the startup snapshot is not.
    cfg = default_config()
    cfg.destinations = [DICOMDestination(name="pacs", host="h", port=104, aet_target="A")]
    client.put("/api/config", json=cfg.model_dump(mode="json"))

    r = client.get("/api/system/status")
    assert r.json()["config_pending_restart"] is True


def test_system_status_no_pending_restart_after_unchanged_save(client: TestClient) -> None:
    """Saving the same config back does not raise a false pending flag."""
    r = client.get("/api/system/status")
    assert r.json()["config_pending_restart"] is False

    body = client.get("/api/config").json()
    client.put("/api/config", json=body)

    r = client.get("/api/system/status")
    assert r.json()["config_pending_restart"] is False


def test_system_health(client: TestClient) -> None:
    r = client.get("/api/system/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_system_start_stop(client: TestClient, app) -> None:
    # Stop first (might be running from previous test)
    client.post("/api/system/stop")
    r = client.post("/api/system/start")
    assert r.status_code == 200
    assert r.json()["status"] == "started"
    assert app.state.receiver.is_running

    r = client.post("/api/system/stop")
    assert r.status_code == 200
    assert r.json()["status"] == "stopped"
    assert not app.state.receiver.is_running


# ══════════════════════════════════════════════════════════════════════
# Queue / Studies endpoints  (§7.2)
# ══════════════════════════════════════════════════════════════════════


def test_queue_stats_empty(client: TestClient) -> None:
    r = client.get("/api/queue/stats")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 0
    assert data["queued"] == 0


def test_queue_stats_with_studies(
    client: TestClient, spool: Spool, target_hub: DICOMDestination
) -> None:
    s1 = spool.receive("1.1.1")
    spool.enqueue(s1, [target_hub])
    s2 = spool.receive("2.2.2")
    spool.enqueue(s2, [target_hub])
    r = client.get("/api/queue/stats")
    assert r.status_code == 200
    assert r.json()["total"] == 2


def test_list_studies_empty(client: TestClient) -> None:
    r = client.get("/api/studies")
    assert r.status_code == 200
    data = r.json()
    assert data["items"] == []
    assert data["total"] == 0


def test_list_studies_with_data(client: TestClient, spool: Spool) -> None:
    spool.receive("1.1.1", accession="A001", modality="CT")
    spool.receive("2.2.2", accession="A002", modality="MR")
    r = client.get("/api/studies")
    assert r.status_code == 200
    studies = r.json()["items"]
    assert len(studies) == 2
    uids = {s["study_uid"] for s in studies}
    assert uids == {"1.1.1", "2.2.2"}


def test_list_studies_filter_by_state(client: TestClient, spool: Spool) -> None:
    spool.receive("1.1.1")
    spool.receive("2.2.2")
    r = client.get("/api/studies?state=RECEIVED")
    assert r.status_code == 200
    assert len(r.json()["items"]) == 2
    r = client.get("/api/studies?state=SENT")
    assert r.status_code == 200
    assert len(r.json()["items"]) == 0


def test_list_studies_filter_by_modality(client: TestClient, spool: Spool) -> None:
    spool.receive("1.1.1", modality="CT")
    spool.receive("2.2.2", modality="MR")
    r = client.get("/api/studies?modality=CT")
    assert r.status_code == 200
    assert len(r.json()["items"]) == 1
    assert r.json()["items"][0]["modality"] == "CT"


def test_get_study(client: TestClient, spool: Spool, target_hub: DICOMDestination) -> None:
    study_id = spool.receive("1.1.1", accession="A001")
    spool.enqueue(study_id, [target_hub])
    r = client.get(f"/api/studies/{study_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["study_uid"] == "1.1.1"
    assert data["accession"] == "A001"
    assert "routes" in data
    assert len(data["routes"]) == 1


def test_get_study_not_found(client: TestClient) -> None:
    r = client.get("/api/studies/99999")
    assert r.status_code == 404


def test_get_study_routes(client: TestClient, spool: Spool, target_hub: DICOMDestination) -> None:
    study_id = spool.receive("1.1.1")
    spool.enqueue(study_id, [target_hub])
    r = client.get(f"/api/studies/{study_id}/routes")
    assert r.status_code == 200
    routes = r.json()
    assert len(routes) == 1
    assert routes[0]["target_name"] == "hub"


def test_get_study_routes_not_found(client: TestClient) -> None:
    r = client.get("/api/studies/99999/routes")
    assert r.status_code == 404


def test_retry_study(client: TestClient, spool: Spool, target_hub: DICOMDestination) -> None:
    study_id = spool.receive("1.1.1")
    spool.enqueue(study_id, [target_hub])
    # Force to FAILED
    spool.claim_next(limit=1)
    spool.fail(study_id, "hub", "error", max_attempts=1)
    assert spool.state(study_id) == StudyState.FAILED

    r = client.post(f"/api/studies/{study_id}/retry")
    assert r.status_code == 200
    assert r.json()["status"] == "queued"
    assert spool.state(study_id) == StudyState.QUEUED


def test_retry_study_not_failed(client: TestClient, spool: Spool) -> None:
    study_id = spool.receive("1.1.1")
    r = client.post(f"/api/studies/{study_id}/retry")
    assert r.status_code == 400


def test_retry_study_not_found(client: TestClient) -> None:
    r = client.post("/api/studies/99999/retry")
    assert r.status_code == 404


# ── /enqueue: rescue a RECEIVED study that never got routes (E1 dry run) ──


def test_enqueue_rescues_routeless_study(
    client: TestClient, app, spool: Spool, target_hub: DICOMDestination
) -> None:
    """A stranded study with zero routes becomes QUEUED via the panel.

    Mirrors the E1 dry-run condition exactly: instances arrived while no
    destination was enabled, so no route was ever created.
    """
    spool._config.destinations = [target_hub]  # destinations added post-receipt
    app.state.config.destinations = [target_hub]
    study_id = spool.receive("1.1.2")
    assert spool.get_routes(study_id) == []  # the stranded condition

    r = client.post(f"/api/studies/{study_id}/enqueue")
    assert r.status_code == 200
    assert r.json()["status"] == "queued"
    assert [t["target_name"] for t in spool.get_routes(study_id)] == ["hub"]


def test_enqueue_is_idempotent(
    client: TestClient, app, spool: Spool, target_hub: DICOMDestination
) -> None:
    """A second call reports already-queued, never duplicates routes."""
    spool._config.destinations = [target_hub]
    app.state.config.destinations = [target_hub]
    study_id = spool.receive("1.1.3")
    assert client.post(f"/api/studies/{study_id}/enqueue").status_code == 200

    r = client.post(f"/api/studies/{study_id}/enqueue")
    assert r.status_code == 200
    assert r.json()["status"] == "already-queued"
    assert len(spool.get_routes(study_id)) == 1


def test_enqueue_no_destination_is_config_error(client: TestClient, spool: Spool) -> None:
    """Enqueueing with no enabled destination surfaces as a client error,
    not a silent 200 — hiding a config error would strand the study again."""
    study_id = spool.receive("1.1.5")

    r = client.post(f"/api/studies/{study_id}/enqueue")
    assert r.status_code == 409
    assert "no enabled destination" in r.json()["detail"]


def test_enqueue_sent_study_refused(
    client: TestClient, app, spool: Spool, target_hub: DICOMDestination
) -> None:
    """Terminal studies must not be re-routed (re-delivery is not idempotent)."""
    spool._config.destinations = [target_hub]
    app.state.config.destinations = [target_hub]
    study_id = spool.receive("1.1.4")
    spool._db.set_study_state(study_id, StudyState.SENT.value)

    r = client.post(f"/api/studies/{study_id}/enqueue")
    assert r.status_code == 409
    assert "terminal" in r.json()["detail"]


def test_enqueue_not_found(client: TestClient) -> None:
    r = client.post("/api/studies/99999/enqueue")
    assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════
# Config endpoints  (§7.1)
# ══════════════════════════════════════════════════════════════════════


def test_get_config(client: TestClient) -> None:
    r = client.get("/api/config")
    assert r.status_code == 200
    data = r.json()
    assert "general" in data
    assert "receiver" in data
    assert "destinations" in data
    assert "forwarding" in data
    assert "storage" in data
    assert "web_ui" in data
    assert "usb_mode" in data


def test_get_config_redacts_credentials(client: TestClient) -> None:
    r = client.get("/api/config")
    data = r.json()
    # Credentials entries should be redacted
    entries = data.get("credentials", {}).get("entries", {})
    for entry in entries.values():
        if entry.get("password_encrypted"):
            assert entry["password_encrypted"] == "***"


def test_update_config(client: TestClient) -> None:
    """PUT /config validates and applies a full config payload (S06-T2)."""
    cfg = default_config()
    payload = cfg.model_dump(mode="json")
    r = client.put("/api/config", json=payload)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_put_config_roundtrip_preserves_secrets(
    client: TestClient, app, spool: Spool, tmp_path
) -> None:
    """UI round-trip (review F4): GET /config redacts secrets to '***'; a
    subsequent PUT of that redacted body must restore the *previous* secret
    values, not persist the '***' sentinels."""
    import json as _json

    from mercure_gateway.config import load_config

    config_path = tmp_path / "gw.json"
    app.state.config_path = str(config_path)

    # Real config: a destination password and a hub api key.
    real = default_config()
    real.destinations = [
        DICOMDestination(name="hub", type="dicom", host="h", port=1, aet_target="H")
    ]
    real.destinations[0].type = "sftp"  # type: ignore[union-attr]
    from mercure_gateway.config import SFTPDestination

    real.destinations = [
        SFTPDestination(
            name="nas", type="sftp", host="nas", port=22, username="u", password="REAL-PW"
        )
    ]
    real.audit.hub_reporting.api_key = "REAL-KEY"
    app.state.config = real

    redacted = client.get("/api/config").json()
    assert redacted["destinations"][0]["password"] == "***"
    assert redacted["audit"]["hub_reporting"]["api_key"] == "***"

    # Operator edits something else and saves the whole body back.
    redacted["general"]["appliance_name"] = "Renamed"
    r = client.put("/api/config", json=redacted)
    assert r.status_code == 200

    saved = _json.loads(config_path.read_text())
    assert saved["destinations"][0]["password"] == "REAL-PW"
    assert saved["audit"]["hub_reporting"]["api_key"] == "REAL-KEY"
    assert saved["general"]["appliance_name"] == "Renamed"
    # And the in-memory config keeps the real secret too (load path parity).
    assert load_config(config_path).destinations[0].password == "REAL-PW"  # type: ignore[union-attr]


def test_put_config_rename_preserves_secrets(
    client: TestClient, app, spool: Spool, tmp_path
) -> None:
    """Renaming a destination must not destroy its stored credential.

    The Destinations page exposes rename as a first-class action. The secret
    field still carries the '***' sentinel (the operator never touched it), but
    under a name the by-name restore lookup cannot match — so the sentinel would
    be persisted as the literal password and the destination would break after
    the next restart with no indication why.
    """
    import json as _json

    from mercure_gateway.config import SFTPDestination, load_config

    config_path = tmp_path / "gw.json"
    app.state.config_path = str(config_path)

    real = default_config()
    real.destinations = [
        SFTPDestination(
            name="nas", type="sftp", host="nas", port=22, username="u", password="REAL-PW"
        )
    ]
    app.state.config = real

    redacted = client.get("/api/config").json()
    # Rename in place; the password is still the untouched sentinel.
    redacted["destinations"][0]["name"] = "nas-2"

    r = client.put("/api/config", json=redacted)
    assert r.status_code == 200

    saved = _json.loads(config_path.read_text())
    assert saved["destinations"][0]["name"] == "nas-2"
    assert saved["destinations"][0]["password"] == "REAL-PW"
    assert load_config(config_path).destinations[0].password == "REAL-PW"  # type: ignore[union-attr]


def test_update_config_rejects_invalid(client: TestClient) -> None:
    """A malformed config body returns 400, not a silent success."""
    r = client.put("/api/config", json={"general": {"log_level": "BOGUS"}})
    assert r.status_code == 400


def test_export_config(client: TestClient) -> None:
    r = client.get("/api/config/export")
    assert r.status_code == 200
    assert "application/json" in r.headers["content-type"]
    assert "attachment" in r.headers.get("content-disposition", "")
    data = r.json()
    assert "general" in data


def test_import_config(client: TestClient, tmp_path) -> None:
    """POST /config/import with valid file upload."""
    config_path = tmp_path / "gw.json"
    from mercure_gateway.config import save_config

    save_config(default_config(), config_path)
    client.app.state.config_path = str(config_path)

    import_json = default_config().model_dump_json()
    files = {"file": ("mercure-gateway.json", import_json, "application/json")}
    r = client.post("/api/config/import", files=files)

    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ══════════════════════════════════════════════════════════════════════
# Auth endpoints  (review F5 — login must issue the session cookie)
# ══════════════════════════════════════════════════════════════════════


def test_login_disabled_auth_still_sets_cookie(client: TestClient) -> None:
    """POST /api/login issues the mercure_session cookie (with auth disabled
    it accepts any password but still establishes a session — the SPA can
    then call authenticated endpoints unchanged)."""
    r = client.post("/api/login", json={"password": "whatever"})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert "mercure_session" in client.cookies


def test_login_with_auth_enabled_issues_working_session() -> None:
    """With auth_enabled + a sha256 password hash: wrong password → 401,
    correct password → 200 + cookie that unlocks the admin API (review F5)."""
    from mercure_gateway.web.auth import _COOKIE_NAME

    spool = Spool(mem_database())
    cfg = default_config()
    salt = "abcdef01"
    import hashlib

    cfg.web_ui.auth_enabled = True
    cfg.web_ui.auth_password_hash = (
        f"sha256${salt}${hashlib.sha256((salt + 's3cret').encode()).hexdigest()}"
    )
    app = create_app(cfg, spool)
    client = TestClient(app)

    # No session: admin API is locked (401).
    assert client.get("/api/system/status").status_code == 401

    # Wrong password rejected, no cookie.
    r = client.post("/api/login", json={"password": "wrong"})
    assert r.status_code == 401
    assert not client.cookies.get(_COOKIE_NAME)

    # Correct password → cookie → admin API reachable.
    r = client.post("/api/login", json={"password": "s3cret"})
    assert r.status_code == 200
    assert client.cookies.get(_COOKIE_NAME)
    assert client.get("/api/system/status").status_code == 200

    # Logout clears the session.
    r = client.post("/api/logout")
    assert r.status_code == 200
    assert client.get("/api/system/status").status_code == 401


def test_login_missing_password_rejected(client: TestClient) -> None:
    """A body without a password is a validation error (422) or 401 —
    never a silent session grant."""
    r = client.post("/api/login", json={})
    assert r.status_code == 422


# ══════════════════════════════════════════════════════════════════════
# Reports endpoints  (§7.3)
# ══════════════════════════════════════════════════════════════════════


def test_list_reports_empty(client: TestClient) -> None:
    r = client.get("/api/reports")
    assert r.status_code == 200
    assert r.json() == []


def test_list_reports_with_data(client: TestClient, spool: Spool) -> None:
    study_id = spool.receive("1.1.1")
    spool._db.insert_report(study_id, "1.1.1", "sr", status="retrieved")
    spool._db.insert_report(study_id, "1.1.1", "pdf", status="pending")
    r = client.get("/api/reports")
    assert r.status_code == 200
    assert len(r.json()) == 2


def test_list_reports_filter_by_type(client: TestClient, spool: Spool) -> None:
    study_id = spool.receive("1.1.1")
    spool._db.insert_report(study_id, "1.1.1", "sr")
    spool._db.insert_report(study_id, "1.1.1", "pdf")
    r = client.get("/api/reports?report_type=sr")
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["report_type"] == "sr"


def test_get_report(client: TestClient, spool: Spool) -> None:
    study_id = spool.receive("1.1.1")
    report_id = spool._db.insert_report(study_id, "1.1.1", "sr", status="retrieved")
    r = client.get(f"/api/reports/{report_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["report_type"] == "sr"
    assert data["status"] == "retrieved"


def test_get_report_not_found(client: TestClient) -> None:
    r = client.get("/api/reports/99999")
    assert r.status_code == 404


def test_refresh_report(client: TestClient, spool: Spool) -> None:
    study_id = spool.receive("1.1.1")
    report_id = spool._db.insert_report(study_id, "1.1.1", "sr")
    r = client.post(f"/api/reports/{report_id}/refresh")
    assert r.status_code == 503  # no retriever wired in this fixture


def test_refresh_report_with_retriever(app_with_retriever, spool: Spool) -> None:
    client = TestClient(app_with_retriever)
    study_id = spool.receive("1.1.1")
    report_id = spool._db.insert_report(study_id, "1.1.1", "sr")
    r = client.post(f"/api/reports/{report_id}/refresh")
    assert r.status_code == 200
    assert r.json()["status"] == "retrieved"


def test_refresh_report_not_found(client: TestClient) -> None:
    r = client.post("/api/reports/99999/refresh")
    assert r.status_code == 404


def test_get_report_content(client: TestClient, spool: Spool) -> None:
    study_id = spool.receive("1.1.1")
    report_id = spool._db.insert_report(study_id, "1.1.1", "sr", file_path="/tmp/report.dcm")
    r = client.get(f"/api/reports/{report_id}/content")
    assert r.status_code == 200
    data = r.json()
    assert data["report_type"] == "sr"
    assert data["file_path"] == "/tmp/report.dcm"


def test_get_report_content_not_found(client: TestClient) -> None:
    r = client.get("/api/reports/99999/content")
    assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════
# Audit endpoints  (§7.4)
# ══════════════════════════════════════════════════════════════════════


def test_list_audit_empty(client: TestClient) -> None:
    r = client.get("/api/audit")
    assert r.status_code == 200
    assert r.json() == []


def test_list_audit_with_events(client: TestClient, spool: Spool) -> None:
    from mercure_gateway.audit import AuditLog

    audit = AuditLog(spool._db)
    audit.append("TEST_EVENT", {"key": "value"})
    audit.append("ANOTHER_EVENT")
    r = client.get("/api/audit")
    assert r.status_code == 200
    events = r.json()
    assert len(events) == 2
    assert events[0]["event"] == "ANOTHER_EVENT"  # newest first


def test_list_audit_filter_by_event(client: TestClient, spool: Spool) -> None:
    from mercure_gateway.audit import AuditLog

    audit = AuditLog(spool._db)
    audit.append("EVENT_A")
    audit.append("EVENT_B")
    audit.append("EVENT_A")
    r = client.get("/api/audit?event=EVENT_A")
    assert r.status_code == 200
    assert len(r.json()) == 2


def test_verify_audit(client: TestClient, spool: Spool) -> None:
    from mercure_gateway.audit import AuditLog

    audit = AuditLog(spool._db)
    audit.append("TEST")
    r = client.get("/api/audit/verify")
    assert r.status_code == 200
    data = r.json()
    assert data["valid"] is True
    assert data["errors"] == []


def test_export_audit(client: TestClient, spool: Spool) -> None:
    from mercure_gateway.audit import AuditLog

    audit = AuditLog(spool._db)
    audit.append("TEST")
    r = client.get("/api/audit/export")
    assert r.status_code == 200
    data = r.json()
    assert "events" in data
    assert data["count"] >= 1
    assert "attachment" in r.headers.get("content-disposition", "")


def test_export_audit_includes_hash(client: TestClient, spool: Spool) -> None:
    """Exports must include the chain hash so they can be cross-checked
    offline against head_hash (offline tamper verification)."""
    from mercure_gateway.audit import AuditLog

    audit = AuditLog(spool._db)
    audit.append("TEST")
    r = client.get("/api/audit/export")
    data = r.json()
    for event in data["events"]:
        assert event.get("hash"), "audit export missing chain hash"


def test_export_audit_redacts_phi_under_minimal_scope(client: TestClient, spool: Spool) -> None:
    """PHI fields are stripped from audit export when phi_scope=minimal (M5)."""
    from mercure_gateway.audit import AuditLog

    audit = AuditLog(spool._db)
    audit.append(
        "STUDY_RECEIVED",
        {"study_uid": "1.2.3", "patient_name": "John Doe", "mrn": "12345"},
    )
    r = client.get("/api/audit/export")
    data = r.json()
    event = data["events"][0]
    detail = event["detail"]
    assert "patient_name" not in detail
    assert "mrn" not in detail
    assert detail.get("study_uid") == "1.2.3"


def test_console_dashboard(client: TestClient, spool: Spool) -> None:
    """Operator console v0 aggregates queue + audit into one dashboard response."""
    from mercure_gateway.audit import AuditLog

    audit = AuditLog(spool._db)
    audit.append("STUDY_RECEIVED", {"study_uid": "1.2.3"})
    r = client.get("/api/console/dashboard")
    assert r.status_code == 200
    data = r.json()
    assert "queue" in data
    assert "recent_events" in data
    assert "recent_errors" in data
    assert len(data["head_hash"]) == 64
    assert "text_log_tail" in data


# ══════════════════════════════════════════════════════════════════════
# Metrics endpoint (D1 — Prometheus text format for headless monitoring)
# ══════════════════════════════════════════════════════════════════════


def test_metrics_endpoint_prometheus_format(client: TestClient) -> None:
    """GET /api/system/metrics returns Prometheus text exposition."""
    r = client.get("/api/system/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    # Every metric carries HELP/TYPE metadata (exposition format convention).
    for name in (
        "mercure_gateway_up",
        "mercure_gateway_uptime_seconds",
        "mercure_gateway_receiver_running",
        "mercure_gateway_forwarder_running",
        "mercure_gateway_queue_depth",
        "mercure_gateway_disk_usage_percent",
    ):
        assert f"# HELP {name} " in body, f"missing HELP for {name}"
        assert f"# TYPE {name} " in body, f"missing TYPE for {name}"


def test_metrics_queue_gauges_reflect_state(
    client: TestClient, spool: Spool, target_hub: DICOMDestination
) -> None:
    """queue_depth{state=...} series match /queue/stats after real transitions."""
    s1 = spool.receive("1.1.1")
    spool.enqueue(s1, [target_hub])  # QUEUED
    counts = spool.count_states()
    body = client.get("/api/system/metrics").text
    assert f'mercure_gateway_queue_depth{{state="QUEUED"}} {counts["QUEUED"]}' in body


def test_metrics_component_gauges(client: TestClient, app) -> None:
    """receiver_running/forwarder_running mirror the components' is_running."""
    app.state.receiver.start()
    body = client.get("/api/system/metrics").text
    assert "mercure_gateway_receiver_running 1" in body
    assert "mercure_gateway_forwarder_running 0" in body


def test_metrics_hub_series_present_when_state_missing(client: TestClient) -> None:
    """No hub configured -> hub gauges report 0/unknown, still valid exposition."""
    body = client.get("/api/system/metrics").text
    assert "mercure_gateway_hub_streaming 0" in body
    assert "mercure_gateway_hub_registered 0" in body


def test_metrics_uptime_increases(client: TestClient) -> None:
    """uptime_seconds is a positive gauge (process clock, not wall)."""
    import time as _t

    body1 = client.get("/api/system/metrics").text
    line = [ln for ln in body1.splitlines() if ln.startswith("mercure_gateway_uptime_seconds ")][0]
    up1 = float(line.split()[-1])
    assert up1 > 0
    _t.sleep(0.02)
    body2 = client.get("/api/system/metrics").text
    line2 = [ln for ln in body2.splitlines() if ln.startswith("mercure_gateway_uptime_seconds ")][0]
    assert float(line2.split()[-1]) >= up1


def test_metrics_disk_gauge_has_no_phi(client: TestClient) -> None:
    """Disk gauge is numeric bytes/percent only — filesystem paths never appear."""
    body = client.get("/api/system/metrics").text
    assert "mercure_gateway_disk_total_bytes " in body
    # The gauge must not leak the spool path (default is under the home dir).
    assert "/home/" not in body
    assert "spool_dir" not in body


def test_metrics_disk_gauges_present_when_spool_dir_missing(
    app, fake_receiver: FakeReceiver, fake_forwarder: FakeForwarder, tmp_path: Path
) -> None:
    """A not-yet-created spool dir must not silently drop the disk series.

    CI caught this (first run on a fresh ubuntu runner): the dir is created
    lazily on first receipt, so a boot with zero studies had
    shutil.disk_usage raise FileNotFoundError, and the suppress() ate the
    whole disk block — the disk-full alert's own metric vanished precisely
    when it was most likely to matter.
    """
    missing = tmp_path / "never-created" / "spool"
    assert not missing.exists()

    # A spool pointed at a path nothing has created yet (fresh-boot shape).
    cfg = default_config()
    cfg.storage.spool_dir = str(missing)
    stranded_spool = Spool(mem_database(), cfg)
    app.state.spool = stranded_spool  # the endpoint reads app.state.spool
    client = TestClient(app)

    body = client.get("/api/system/metrics").text
    for name in (
        "mercure_gateway_disk_usage_percent",
        "mercure_gateway_disk_total_bytes",
        "mercure_gateway_disk_free_bytes",
        "mercure_gateway_disk_over_threshold",
    ):
        assert f"# HELP {name} " in body, f"missing HELP for {name}"
        assert f"# TYPE {name} " in body, f"missing TYPE for {name}"


# ══════════════════════════════════════════════════════════════════════
# Web UI TLS wiring (D3a — ADR-0007)
# ══════════════════════════════════════════════════════════════════════


def test_run_web_admin_passes_tls_to_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    """When web_ui TLS is configured, uvicorn.run gets ssl_certfile/ssl_keyfile."""
    import uvicorn as uvicorn_module

    from mercure_gateway.main import _run_web_admin

    captured: dict[str, object] = {}

    def fake_run(app: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn_module, "run", fake_run)
    cfg = default_config()
    cfg.web_ui.tls_cert_file = "/tmp/cert.pem"
    cfg.web_ui.tls_key_file = "/tmp/key.pem"
    _run_web_admin(
        cfg,
        Spool(mem_database(), spool_dir="/tmp/spool-test-tls"),
        receiver=None,  # type: ignore[arg-type]
        forwarder=None,  # type: ignore[arg-type]
        report_retriever=None,
        port=18099,
    )
    assert captured["ssl_certfile"] == "/tmp/cert.pem"
    assert captured["ssl_keyfile"] == "/tmp/key.pem"


def test_run_web_admin_plain_http_without_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    """No TLS configured -> no ssl kwargs -> plain HTTP (loopback default)."""
    import uvicorn as uvicorn_module

    from mercure_gateway.main import _run_web_admin

    captured: dict[str, object] = {}

    def fake_run(app: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn_module, "run", fake_run)
    cfg = default_config()
    _run_web_admin(
        cfg,
        Spool(mem_database(), spool_dir="/tmp/spool-test-tls2"),
        receiver=None,  # type: ignore[arg-type]
        forwarder=None,  # type: ignore[arg-type]
        report_retriever=None,
        port=18099,
    )
    assert "ssl_certfile" not in captured
    assert "ssl_keyfile" not in captured


# ══════════════════════════════════════════════════════════════════════
# Config warnings (refinement 2026-09-17) — lint surfaced to the panel
# ══════════════════════════════════════════════════════════════════════


def test_get_config_warnings_clean(client: TestClient) -> None:
    """A healthy running config reports no warnings."""
    from mercure_gateway.config import DICOMDestination

    cfg = default_config()
    cfg.destinations = [DICOMDestination(name="pacs", host="h", port=104, aet_target="A")]
    client.put("/api/config", json=cfg.model_dump(mode="json"))

    r = client.get("/api/config/warnings")
    assert r.status_code == 200
    assert r.json()["warnings"] == []


def test_get_config_warnings_reports_stale_rule(client: TestClient) -> None:
    """The stale-rule finding reaches the panel, not just the log."""
    from mercure_gateway.config import DICOMDestination, ForwardingRule

    cfg = default_config()
    cfg.destinations = [DICOMDestination(name="pacs-a", host="h", port=104, aet_target="A")]
    cfg.forwarding_rules = [
        ForwardingRule(rule="StudyDescription ~ 'CHEST'", targets=["pacs-a", "ghost"])
    ]
    client.put("/api/config", json=cfg.model_dump(mode="json"))

    r = client.get("/api/config/warnings")
    assert r.status_code == 200
    warnings = r.json()["warnings"]
    assert len(warnings) == 1
    assert warnings[0]["path"] == "forwarding_rules[0].targets"
    assert "ghost" in warnings[0]["message"]


def test_put_config_returns_warnings_for_the_saved_state(client: TestClient) -> None:
    """A save reports what the saved config will do, in the same response."""
    from mercure_gateway.config import DICOMDestination

    cfg = default_config()
    cfg.destinations = [
        DICOMDestination(name="pacs", host="h", port=104, aet_target="A", enabled=False)
    ]
    r = client.put("/api/config", json=cfg.model_dump(mode="json"))
    assert r.status_code == 200
    warnings = r.json()["warnings"]
    assert any(w["path"] == "destinations" and "disabled" in w["message"] for w in warnings)
