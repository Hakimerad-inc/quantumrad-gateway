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

import threading
import time
from pathlib import Path

import pytest

# Shared fakes from conftest.py
from conftest import FakeForwarder, FakeReceiver
from fastapi.testclient import TestClient

from mercure_gateway.config import DICOMDestination, default_config
from mercure_gateway.spool import Spool, StudyState
from mercure_gateway.spool.db import mem_database
from mercure_gateway.web import create_app
from mercure_gateway.web.auth import verify_password

_SR_SOP = "1.2.840.10008.5.1.4.1.1.88.33"


def _write_minimal_sr(path: str, study_uid: str = "1.1.1") -> str:
    """Write a minimal SR DICOM file — a report the content endpoint can load."""
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian

    ds = Dataset()
    ds.SOPClassUID = _SR_SOP
    ds.SOPInstanceUID = "1.2.3.4.5.6.7.1"
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = "1.2.3.4.5.6.100"
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = _SR_SOP
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    ds.save_as(path, enforce_file_format=True)
    return path


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


def test_system_status_reports_usb_mode(app, client: TestClient) -> None:
    """The USB dongle variant is surfaced so a tray can hint safe removal (B10).

    Read from the live config rather than a boot snapshot or a device probe:
    usb_mode.enabled is what boot decided, there is no app.state key for it,
    and hotplug surfaces removal events rather than current state.
    """
    assert client.get("/api/system/status").json()["usb_mode"] is False

    cfg = client.get("/api/config").json()
    cfg["usb_mode"]["enabled"] = True
    r = client.put("/api/config", json=cfg)
    assert r.status_code == 200

    data = client.get("/api/system/status").json()
    assert data["usb_mode"] is True


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


def test_put_config_rename_with_sentinel_is_rejected(
    client: TestClient, app, spool: Spool, tmp_path
) -> None:
    """Renaming a destination while its secret is still the '***' sentinel is
    a 400 naming the destination (review P1-2).

    The Destinations page exposes rename as a first-class action. Position used
    to restore the secret, but position is not identity — any UI sort or filter
    reorders the saved body, so the same payload could inherit the *wrong*
    stored credential. Now the operator re-enters the credential; the literal
    sentinel is never persisted.
    """
    from mercure_gateway.config import SFTPDestination

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
    redacted["destinations"][0]["name"] = "nas-2"

    r = client.put("/api/config", json=redacted)
    assert r.status_code == 400
    assert "nas-2" in r.json()["detail"]
    assert "re-enter" in r.json()["detail"]

    # Nothing was persisted, and the running config is untouched.
    assert not config_path.exists()
    assert _config_dest_password(app.state.config, 0) == "REAL-PW"


def test_put_config_rename_with_reentered_secret_succeeds(
    client: TestClient, app, spool: Spool, tmp_path
) -> None:
    """The documented resolution: rename and re-type the credential."""
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
    redacted["destinations"][0]["name"] = "nas-2"
    redacted["destinations"][0]["password"] = "REAL-PW"

    r = client.put("/api/config", json=redacted)
    assert r.status_code == 200

    saved = _json.loads(config_path.read_text())
    assert saved["destinations"][0]["name"] == "nas-2"
    assert load_config(config_path).destinations[0].password == "REAL-PW"  # type: ignore[union-attr]


def _config_dest_password(config: object, index: int) -> str:
    return getattr(config.destinations[index], "password", "")  # type: ignore[no-any-return]


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


def test_import_config_rejects_an_insecure_bind(client: TestClient, tmp_path) -> None:
    """An imported file that would open the admin panel to the LAN is 409.

    Import is a full config write and persisted to disk, so without this
    check it could save a file boot would then refuse — the one config write
    path that used to apply it without a peep (review P0-3).
    """
    config_path = tmp_path / "gw.json"
    from mercure_gateway.config import save_config

    save_config(default_config(), config_path)
    client.app.state.config_path = str(config_path)

    imported = default_config()
    imported.web_ui.host = "0.0.0.0"
    imported.web_ui.auth_enabled = False
    files = {"file": ("mercure-gateway.json", imported.model_dump_json(), "application/json")}
    r = client.post("/api/config/import", files=files)

    assert r.status_code == 409
    assert "auth_enabled" in r.json()["detail"]
    # The running config is unchanged, and the file on disk was not overwritten.
    assert client.get("/api/config").json()["web_ui"]["host"] == "127.0.0.1"
    from json import loads

    assert loads(config_path.read_text())["web_ui"]["host"] == "127.0.0.1"
    # The refused import is in the audit chain like the PUT path's rejections.
    events = client.get("/api/audit?limit=25").json()
    assert isinstance(events, list)
    assert any(e["event"] == "CONFIG_SECURITY_REJECTED" for e in events)


# ══════════════════════════════════════════════════════════════════════
# Forwarding-rule preview  (US-09 / review P0-9 — the engine existed but was
# reachable only from these tests; the operator-facing half was never wired)
# ══════════════════════════════════════════════════════════════════════


def _config_with_rules(client: TestClient, *, rules: list, enabled: bool = True) -> None:
    """Give the app two destinations plus *rules*, returning nothing.

    Destinations are set on ``app.state.config`` (what the preview reads) with
    one disabled, so the default-route test can tell 'every enabled target'
    from 'every target'.
    """
    from mercure_gateway.config import DICOMDestination

    client.app.state.config.destinations = [
        DICOMDestination(
            name="pacs", host="pacs.local", port=104, aet_target="PACS", enabled=enabled
        ),
        DICOMDestination(name="archive", host="arch.local", port=104, aet_target="ARCH"),
        DICOMDestination(
            name="off", host="off.local", port=104, aet_target="OFF", enabled=False
        ),
    ]
    client.app.state.config.forwarding_rules = rules


def test_preview_rules_matches_a_configured_rule(client: TestClient) -> None:
    from mercure_gateway.config import ForwardingRule

    _config_with_rules(
        client,
        rules=[ForwardingRule(rule="modality:CT", targets=["archive"], priority="high")],
    )
    r = client.post("/api/rules/preview", json={"tags": {"Modality": "CT"}})

    assert r.status_code == 200
    body = r.json()
    assert body["targets"] == ["archive"]
    assert body["matched_any"] is True


def test_preview_rules_no_match_is_the_default_route(client: TestClient) -> None:
    """A tag set no rule matches previews every *enabled* destination.

    A disabled destination never receives a study regardless of what a rule
    says, so the default route is the enabled set — previewing all three would
    disagree with what enqueue actually does.
    """
    _config_with_rules(client, rules=[])
    r = client.post("/api/rules/preview", json={"tags": {"Modality": "US"}})

    assert r.status_code == 200
    body = r.json()
    assert body["targets"] == ["pacs", "archive"]
    # The target list alone cannot tell "a rule picked these" from "no rule
    # said anything" — the flag is the difference.
    assert body["matched_any"] is False


def test_preview_rules_supplied_rules_override_the_configured_ones(
    client: TestClient,
) -> None:
    """An unsaved rule set is previewable without touching the config.

    The destinations panel edits rules before saving them; without this the
    operator could only check the rules already on disk.
    """
    from mercure_gateway.config import ForwardingRule

    _config_with_rules(client, rules=[ForwardingRule(rule="modality:CT", targets=["pacs"])])
    r = client.post(
        "/api/rules/preview",
        json={
            "tags": {"Modality": "MR"},
            "rules": [{"rule": "modality:MR", "targets": ["archive"]}],
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert body["targets"] == ["archive"]
    assert body["matched_any"] is True
    # The configured rules were not consulted and were not changed.
    assert client.app.state.config.forwarding_rules[0].targets == ["pacs"]


def test_preview_rules_rejects_a_malformed_configured_rule(client: TestClient) -> None:
    """A bad configured rule is a 400, not a silent default route.

    Routing fails open (a typo over-delivering beats a stranded study), but
    preview is the operator's explicit check, so the problem is reported —
    with its index, the same pointer /config/warnings uses.
    """
    from mercure_gateway.config import ForwardingRule

    _config_with_rules(
        client,
        rules=[
            ForwardingRule(rule="modality:CT", targets=["archive"]),
            ForwardingRule(rule="malformed", targets=["pacs"]),
        ],
    )
    r = client.post("/api/rules/preview", json={"tags": {"Modality": "CT"}})

    assert r.status_code == 400
    assert "forwarding_rules[1]" in r.json()["detail"]


def test_preview_rules_rejects_a_malformed_supplied_rule(client: TestClient) -> None:
    _config_with_rules(client, rules=[])
    r = client.post(
        "/api/rules/preview",
        json={"tags": {}, "rules": [{"rule": "garbage", "targets": ["pacs"]}]},
    )

    assert r.status_code == 400
    assert "forwarding_rules[0]" in r.json()["detail"]


def test_preview_rules_rejects_an_invalid_rule_shape(client: TestClient) -> None:
    """A rule missing its required fields is a 422, not a preview of nothing.

    ForwardingRule requires a non-empty rule and at least one target; an
    empty target list is not a rule that matches nothing, it is one that can
    never deliver.
    """
    r = client.post(
        "/api/rules/preview",
        json={"tags": {"Modality": "CT"}, "rules": [{"rule": "modality:CT"}]},
    )
    assert r.status_code == 422


def test_preview_rules_agrees_with_enqueue(client: TestClient, spool: Spool) -> None:
    """The preview's answer is the answer enqueue gives a real study.

    Before P0-9 the panel previewed one engine and production routed by
    another (the preview engine raised on every deployed ``modality:CT``
    rule; the router hand-rolled a matcher with no priority). This pins the
    unified behaviour end to end.
    """
    from mercure_gateway.config import DICOMDestination, ForwardingRule

    _config_with_rules(
        client,
        rules=[
            ForwardingRule(rule="modality:CT", targets=["archive"], priority="high"),
            ForwardingRule(rule="modality:CT", targets=["pacs"], priority="normal"),
        ],
    )
    # enqueue reads the spool's own config ref — keep the two in step, the way
    # the running appliance does (a save takes effect after restart).
    spool._config = client.app.state.config

    r = client.post("/api/rules/preview", json={"tags": {"Modality": "CT"}})
    assert r.json()["targets"] == ["archive"]

    study_id = spool.receive("1.2.3.4", modality="CT")
    spool.enqueue(
        study_id,
        [
            DICOMDestination(name="pacs", host="pacs.local", port=104, aet_target="PACS"),
            DICOMDestination(name="archive", host="arch.local", port=104, aet_target="ARCH"),
        ],
    )
    routes = [row["target_name"] for row in spool.get_routes(study_id)]
    assert routes == ["archive"]


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


def test_password_change_sets_a_pbkdf2_hash_and_audits(client: TestClient) -> None:
    """POST /api/web-ui/password is the only API surface that creates a hash."""
    r = client.post(
        "/api/web-ui/password", json={"new_password": "s3cret-s3cret"}
    )
    assert r.status_code == 200

    cfg = client.app.state.config
    assert cfg.web_ui.auth_password_hash.startswith("pbkdf2$")
    # Auth is NOT enabled on the operator's behalf — the endpoint only sets the
    # hash, so enabling it stays an explicit decision.
    assert cfg.web_ui.auth_enabled is False

    # The change is visible in the audit chain.
    events = client.get("/api/audit?limit=10").json()
    assert any(e["event"] == "WEB_UI_PASSWORD_CHANGED" for e in events)


def test_password_change_requires_the_current_password_when_auth_is_on(
    client: TestClient,
) -> None:
    """A hijacked or stale session cannot silently rotate the credential."""
    from mercure_gateway.web.auth import hash_password

    client.app.state.config.web_ui.auth_enabled = True
    client.app.state.config.web_ui.auth_password_hash = hash_password("old-pw-1234")
    # The endpoint is auth-gated once auth is on, so establish a session.
    assert client.post("/api/login", json={"password": "old-pw-1234"}).status_code == 200

    r = client.post(
        "/api/web-ui/password",
        json={"current_password": "wrong", "new_password": "new-pw-1234"},
    )
    assert r.status_code == 401
    # The stored hash is unchanged.
    assert verify_password("old-pw-1234", client.app.state.config.web_ui.auth_password_hash)

    r = client.post(
        "/api/web-ui/password",
        json={"current_password": "old-pw-1234", "new_password": "new-pw-1234"},
    )
    assert r.status_code == 200
    assert verify_password("new-pw-1234", client.app.state.config.web_ui.auth_password_hash)


def test_password_change_rejects_a_short_password(client: TestClient) -> None:
    r = client.post("/api/web-ui/password", json={"new_password": "short"})
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
    # file_path must NOT be echoed: the report row stores the server-side path
    # of the retrieved DICOM object, which is not the client's business. The
    # status is pending (no retriever wired), so this is the branch where the
    # path used to leak alongside content=None.
    assert "file_path" not in data


def test_report_rows_carry_no_server_path(client: TestClient, spool: Spool, tmp_path: Path) -> None:
    """No reports endpoint may echo the stored file_path (PHI-adjacent leak).

    All three read paths — the list, the single row and the content body —
    were built on ``SELECT * FROM reports``, which ships the column
    unconditionally. A response model that omits the field is the enforcement;
    these assertions prove the wire is clean rather than trusting the model.

    The content branch reads the file for real, so the leak's own test object
    is a stored DICOM object whose path must never reach the client.
    """
    sr_path = str(tmp_path / "secret-report.dcm")
    _write_minimal_sr(sr_path)

    study_id = spool.receive("1.1.1")
    spool._db.insert_report(
        study_id, "1.1.1", "sr", status="retrieved", file_path=sr_path
    )
    rows = client.get("/api/reports").json()
    assert len(rows) == 1
    assert "file_path" not in rows[0]

    report_id = rows[0]["id"]
    one = client.get(f"/api/reports/{report_id}").json()
    assert "file_path" not in one
    assert one["report_type"] == "sr"

    content = client.get(f"/api/reports/{report_id}/content").json()
    assert "file_path" not in content
    assert content["status"] == "retrieved"
    # The file was read (this is the dcmread branch, not the pending one) —
    # a minimal SR renders nothing, so either rendered text or a render error
    # is a fine outcome; a server path in either branch is not.
    assert content["content"] is not None or content.get("error") is not None


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
    # The bounded-error contract (item B7): the UI needs the true total to say
    # "showing N of M", and truncated is what makes the cap visible.
    assert data["total_error_count"] == 0
    assert data["truncated"] is False


def test_verify_audit_reports_a_broken_chain(client: TestClient, spool: Spool) -> None:
    """A tampered row is detected, and the error carries the event id."""
    from mercure_gateway.audit import AuditLog

    audit = AuditLog(spool._db)
    audit.append("FIRST")
    audit.append("SECOND")
    # The append-only triggers block UPDATE, so the audit module's own prune
    # path is the honest way to reach a broken link: rewrite a stored hash
    # underneath the triggers.
    from mercure_gateway.spool.db import _AUDIT_NO_DELETE, _AUDIT_NO_UPDATE

    with spool._db.transaction() as conn:
        conn.execute("DROP TRIGGER IF EXISTS audit_events_no_update")
        conn.execute(
            "UPDATE audit_events SET hash = 'not-the-real-hash' WHERE event = 'FIRST'"
        )
        conn.execute(_AUDIT_NO_UPDATE)
        conn.execute(_AUDIT_NO_DELETE)

    r = client.get("/api/audit/verify")
    assert r.status_code == 200
    data = r.json()
    assert data["valid"] is False
    assert data["total_error_count"] >= 1
    assert data["truncated"] is False
    assert data["errors"][0]["reason"] == "hash mismatch"


def test_verify_audit_caps_the_error_list(client: TestClient, spool: Spool) -> None:
    """A corrupted log must not rebuild the whole table in the response body.

    The verifier streams row by row; the response spells out at most
    _MAX_VERIFY_ERRORS_SHOWN and counts the rest. A chain where every link is
    broken is the worst case — months of history, one error per row.
    """
    from mercure_gateway.audit import AuditLog
    from mercure_gateway.web.routes import _MAX_VERIFY_ERRORS_SHOWN

    audit = AuditLog(spool._db)
    n_events = _MAX_VERIFY_ERRORS_SHOWN + 500
    for _ in range(n_events):
        audit.append("FILLER")

    # Break every link: the verifier propagates the *computed* hash, so
    # rewriting one row's stored hash fails only that row. A corrupted log is
    # the worst case only when every stored hash is wrong, which is what an
    # attacker rewriting history and botching the recomputation looks like.
    from mercure_gateway.spool.db import _AUDIT_NO_DELETE, _AUDIT_NO_UPDATE

    with spool._db.transaction() as conn:
        conn.execute("DROP TRIGGER IF EXISTS audit_events_no_update")
        conn.execute("UPDATE audit_events SET hash = 'tampered'")
        conn.execute(_AUDIT_NO_UPDATE)
        conn.execute(_AUDIT_NO_DELETE)

    r = client.get("/api/audit/verify")
    assert r.status_code == 200
    data = r.json()
    assert data["valid"] is False
    assert data["total_error_count"] == n_events
    assert data["truncated"] is True
    assert len(data["errors"]) == _MAX_VERIFY_ERRORS_SHOWN
    assert data["errors"][0]["reason"] == "hash mismatch"


def test_iter_audit_events_steps_a_cursor_not_a_fetchall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chain walk steps a cursor; it never materializes the table (B7).

    The point of the change is bounded peak memory on an appliance that has
    accumulated months of audit rows.  Memory is awkward to assert on, and
    the *consequences* people reach for do not actually work: a generator
    that buffers is still lazy, so "a row appended after the walk starts is
    not seen" is true under both, and on a file-backed database neither
    implementation holds the write lock (that fallback only applies to
    ``:memory:``).  Both of those tests pass against a ``fetchall()``
    regression and are therefore not load-bearing.

    What does distinguish them is the mechanism, so this asserts it: every
    ``next()`` steps the cursor with one ``fetchone()``, and ``fetchall()`` is
    never called.  Reverting to the pre-B7 implementation trips both halves.
    """
    import sqlite3

    from mercure_gateway.audit import AuditLog
    from mercure_gateway.spool.db import open_database

    fetches: list[str] = []

    class _SpyingCursor:
        """Delegates to the real cursor, recording how it is stepped."""

        def __init__(self, cur: sqlite3.Cursor) -> None:
            self._cur = cur

        def fetchone(self) -> object:
            fetches.append("one")
            return self._cur.fetchone()

        def fetchall(self) -> list[sqlite3.Row]:
            fetches.append("all")
            return self._cur.fetchall()

        def __getattr__(self, name: str) -> object:
            return getattr(self._cur, name)

    class _SpyingConnection:
        """Wraps the read connection so every cursor it hands out is spied on.

        ``sqlite3.Cursor`` is an immutable type, so the cursor methods cannot
        be monkeypatched in place — wrapping the connection at the seam the
        walk actually goes through is the only way to observe the stepping.
        """

        def __init__(self, conn: sqlite3.Connection) -> None:
            self._conn = conn

        def execute(self, sql: str, *args: object) -> _SpyingCursor:
            return _SpyingCursor(self._conn.execute(sql, *args))

        def __getattr__(self, name: str) -> object:
            return getattr(self._conn, name)

    db = open_database(tmp_path / "spool.db")
    try:
        real_read_connection = db._read_connection
        monkeypatch.setattr(
            db,
            "_read_connection",
            lambda: _SpyingConnection(real_read_connection()),
        )
        audit = AuditLog(db)
        for name in ("ONE", "TWO", "THREE"):
            audit.append(name)

        it = iter(db.iter_audit_events())
        # Interleave the consumer with the stepping: each yield must have
        # exactly one fetchone behind it, and no fetchall anywhere.
        seen: list[str] = []
        while True:
            before = len(fetches)
            row = next(it, None)
            if row is None:
                break
            seen.append(row["event"])
            # One step per row — a buffered read would have done all three
            # before the first yield.
            assert fetches[before:] == ["one"], fetches[before:]

        assert seen == ["ONE", "TWO", "THREE"]
        assert "all" not in fetches, "the walk materialized the whole table"
        # One step per row, plus the final empty step that ends the walk.
        assert fetches == ["one", "one", "one", "one"], fetches
    finally:
        db.close()


def test_iter_audit_events_takes_no_write_lock_mid_walk(tmp_path: Path) -> None:
    """A walk in progress does not block an append on a file-backed database.

    Before B7 the scan ran under ``_lock``; now it runs on the WAL read
    connection and takes no write lock at all.  Leaving the iterator open
    mid-walk while another thread appends is the observation that would hang
    under the old code.

    Note this is a real property of the read path, not a streaming test: a
    ``fetchall()`` regression on a file-backed database would also pass it,
    because the lock only guards the ``:memory:`` fallback.  See the companion
    test above for the assertion that actually catches that regression.
    """
    from mercure_gateway.audit import AuditLog
    from mercure_gateway.spool.db import open_database

    db = open_database(tmp_path / "spool.db")
    try:
        audit = AuditLog(db)
        audit.append("FILLER")

        it = iter(db.iter_audit_events())
        assert next(it)["event"] == "FILLER"

        done: list[str] = []

        def append_from_another_thread() -> None:
            audit.append("MID_WALK")
            done.append("appended")

        t = threading.Thread(target=append_from_another_thread)
        t.start()
        t.join(timeout=10)
        assert done == ["appended"], "concurrent append blocked on the walk's lock"

        assert next(it, None) is None
        fresh = open_database(tmp_path / "spool.db")
        try:
            rows = list(fresh.iter_audit_events())
        finally:
            fresh.close()
        assert [r["event"] for r in rows] == ["FILLER", "MID_WALK"]
    finally:
        db.close()


def test_verify_iter_is_lazy(spool: Spool) -> None:
    """verify_iter yields one ChainError at a time and never buffers the table.

    A healthy chain yields nothing; the point is that it did not read the
    whole table to reach that answer — see the companion cursor test above for
    the snapshot property that distinguishes streaming from buffering.
    """
    from mercure_gateway.audit import AuditLog

    audit = AuditLog(spool._db)
    for _ in range(50):
        audit.append("FILLER")
    assert list(audit.verify_iter()) == []
    # verify() keeps its whole-list contract for the callers that want it.
    ok, errors = audit.verify()
    assert ok is True
    assert errors == []


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


def test_get_config_warnings_clean(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A healthy running config reports no warnings."""
    from mercure_gateway.config import DICOMDestination

    # The suite defaults to cleartext secrets (conftest), which lints as an
    # intentional info note — not a warning this test is about.
    monkeypatch.delenv("MERCURE_GATEWAY_ALLOW_PLAINTEXT_SECRETS", raising=False)

    cfg = default_config()
    cfg.destinations = [DICOMDestination(name="pacs", host="h", port=104, aet_target="A")]
    client.put("/api/config", json=cfg.model_dump(mode="json"))

    r = client.get("/api/config/warnings")
    assert r.status_code == 200
    assert r.json()["warnings"] == []


def test_get_config_warnings_reports_stale_rule(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stale-rule finding reaches the panel, not just the log."""
    monkeypatch.delenv("MERCURE_GATEWAY_ALLOW_PLAINTEXT_SECRETS", raising=False)

    from mercure_gateway.config import DICOMDestination, ForwardingRule

    cfg = default_config()
    cfg.destinations = [DICOMDestination(name="pacs-a", host="h", port=104, aet_target="A")]
    cfg.forwarding_rules = [
        ForwardingRule(rule="StudyDescription=*CHEST*", targets=["pacs-a", "ghost"])
    ]
    client.put("/api/config", json=cfg.model_dump(mode="json"))

    r = client.get("/api/config/warnings")
    assert r.status_code == 200
    warnings = r.json()["warnings"]
    assert len(warnings) == 1
    assert warnings[0]["path"] == "forwarding_rules[0].targets"
    assert "ghost" in warnings[0]["message"]


def test_get_config_warnings_reports_an_unparsable_rule(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rule that cannot parse makes routing fail open, so the panel says so.

    The spool ignores a rule it cannot parse, which is the right call at
    runtime (a stranded study is worse than an over-delivered one) but is
    invisible in operation: a study routed everywhere looks like a config with
    no rules. The warning is what turns it into something the operator sees
    (review P0-9).
    """
    from mercure_gateway.config import DICOMDestination, ForwardingRule

    monkeypatch.delenv("MERCURE_GATEWAY_ALLOW_PLAINTEXT_SECRETS", raising=False)
    cfg = default_config()
    cfg.destinations = [DICOMDestination(name="pacs", host="h", port=104, aet_target="A")]
    cfg.forwarding_rules = [ForwardingRule(rule="no-equals-here", targets=["pacs"])]
    client.put("/api/config", json=cfg.model_dump(mode="json"))

    r = client.get("/api/config/warnings")
    assert r.status_code == 200
    (warning,) = r.json()["warnings"]
    assert warning["path"] == "forwarding_rules[0].rule"
    assert "cannot be parsed" in warning["message"]


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


# ── Strict schema at the write boundary (review P0-4) ───────────────────


def test_put_config_rejects_an_unknown_key(client: TestClient) -> None:
    """PUT stays strict: the SPA round-trips GET /config, whose body is
    model_dump_json() and therefore key-clean, so an unknown key reaching this
    endpoint is a client bug a 400 should name (review P0-4).
    """
    current = client.get("/api/config").json()
    current["general"]["ae_title"] = "GATEWAY"  # belongs on receiver
    r = client.put("/api/config", json=current)

    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "ae_title" in detail
    # Nothing was persisted.
    assert (
        client.get("/api/config").json()["general"]["appliance_name"]
        == (current["general"]["appliance_name"])
    )


def test_import_config_heals_and_reports_unknown_keys(client: TestClient, tmp_path) -> None:
    """A foreign file is healed, not rejected — and the panel is told which
    settings did not survive, so nothing is silently swallowed (review P0-4)."""
    config_path = tmp_path / "gw.json"
    from mercure_gateway.config import save_config

    save_config(default_config(), config_path)
    client.app.state.config_path = str(config_path)

    import json

    payload = json.loads(default_config().model_dump_json())
    payload["general"]["ae_title"] = "GATEWAY"
    payload["toplevel_stray"] = 1
    files = {"file": ("mercure-gateway.json", json.dumps(payload), "application/json")}
    r = client.post("/api/config/import", files=files)

    assert r.status_code == 200
    assert sorted(r.json()["ignored_keys"]) == [
        "general.ae_title",
        "toplevel_stray",
    ]
    # The healed state is what took effect.
    assert "ae_title" not in client.get("/api/config").json()["general"]


# ── Bind security at the write boundary (review P0-3) ─────────────────────


def test_put_config_rejects_an_insecure_bind(client: TestClient) -> None:
    """A valid config that would open the admin API to the network is 409, not
    saved: the document is valid, the running appliance's posture is what
    conflicts with it (review P0-3).
    """
    current = client.get("/api/config").json()
    current["web_ui"]["host"] = "0.0.0.0"
    current["web_ui"]["auth_enabled"] = False
    r = client.put("/api/config", json=current)

    assert r.status_code == 409
    assert "auth_enabled" in r.json()["detail"]
    # Nothing was applied or persisted: the running config is unchanged.
    assert client.get("/api/config").json()["web_ui"]["host"] == "127.0.0.1"


def test_put_config_secure_bind_is_saved(client: TestClient) -> None:
    """Auth on, or loopback, is no obstacle."""
    current = client.get("/api/config").json()
    current["web_ui"]["host"] = "0.0.0.0"
    current["web_ui"]["auth_enabled"] = True
    # A hash the validator accepts is required for auth to be enabled.
    from mercure_gateway.web.auth import hash_password

    current["web_ui"]["auth_password_hash"] = hash_password("s3cret")
    r = client.put("/api/config", json=current)

    assert r.status_code == 200
    # Enabling auth locks out the cookieless client — log in to read it back.
    assert client.post("/api/login", json={"password": "s3cret"}).status_code == 200
    assert client.get("/api/config").json()["web_ui"]["host"] == "0.0.0.0"


def test_put_config_insecure_rejection_is_audited(client: TestClient) -> None:
    """The refused write is visible in the audit chain, not only the response."""
    current = client.get("/api/config").json()
    current["web_ui"]["host"] = "0.0.0.0"
    current["web_ui"]["auth_enabled"] = False
    client.put("/api/config", json=current)

    events = client.get("/api/audit?limit=25").json()
    assert isinstance(events, list)
    assert any(e["event"] == "CONFIG_SECURITY_REJECTED" for e in events)


def test_put_config_rejects_an_unrecognised_hash(client: TestClient) -> None:
    """A mistyped hash is caught at save time, not at the login screen."""
    current = client.get("/api/config").json()
    current["web_ui"]["auth_enabled"] = True
    current["web_ui"]["auth_password_hash"] = "HASH"

    r = client.put("/api/config", json=current)

    assert r.status_code == 400
    assert "auth_password_hash" in r.text
    # The bad value was not persisted: auth is still off and the hash still empty.
    after = client.get("/api/config").json()["web_ui"]
    assert after["auth_enabled"] is False
    assert after["auth_password_hash"] == ""


def test_put_config_rejects_auth_enabled_with_an_empty_hash(client: TestClient) -> None:
    """The lockout combination is unreachable through the write boundary."""
    current = client.get("/api/config").json()
    current["web_ui"]["auth_enabled"] = True
    current["web_ui"]["auth_password_hash"] = ""

    r = client.put("/api/config", json=current)

    assert r.status_code == 400
    assert "auth_password_hash" in r.text
    assert client.get("/api/config").json()["web_ui"]["auth_enabled"] is False


def test_put_config_accepts_a_pbkdf2_hash(client: TestClient) -> None:
    """A PBKDF2 hash round-trips through the write boundary and still verifies.

    The read-back path redacts the hash to ``***``, so the proof that the stored
    value survived is that a login with the plaintext succeeds — and that a
    second GET → PUT round-trip (which carries only the sentinel) does not
    clobber it.
    """
    from mercure_gateway.web.auth import hash_password

    current = client.get("/api/config").json()
    current["web_ui"]["auth_enabled"] = True
    current["web_ui"]["auth_password_hash"] = hash_password("s3cret")

    assert client.put("/api/config", json=current).status_code == 200

    # The hash is redacted on read, never returned in plaintext.
    assert client.post("/api/login", json={"password": "s3cret"}).status_code == 200
    assert (
        client.get("/api/config").json()["web_ui"]["auth_password_hash"] == "***"
    )

    # A no-op save round-trips the sentinel back to the stored hash.
    r = client.put("/api/config", json=client.get("/api/config").json())
    assert r.status_code == 200
    client.cookies.clear()
    assert client.post("/api/login", json={"password": "s3cret"}).status_code == 200


# ══════════════════════════════════════════════════════════════════════
# Hub delivery observability (review P0-10): a bookkeeper that has been
# down since boot must not read "streaming: healthy" to a scraper.
# ══════════════════════════════════════════════════════════════════════


def _wait_until(predicate: object, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return
        time.sleep(0.02)
    raise AssertionError(f"condition not met within {timeout}s")


def test_metrics_hub_delivery_series_absent_when_hub_is_off(client: TestClient) -> None:
    """No streamer wired -> the series still exist as zeros (never missing)."""
    body = client.get("/api/system/metrics").text
    assert "mercure_gateway_hub_outbox_depth 0" in body
    assert "mercure_gateway_hub_delivering 0" in body
    assert "mercure_gateway_hub_delivered_total 0" in body
    assert "mercure_gateway_hub_delivery_failures_total 0" in body
    assert "mercure_gateway_hub_events_evicted_total 0" in body
    # Unsigned anchoring: nothing to verify is not a failure.
    assert "mercure_gateway_audit_anchor_ok 1" in body
    assert "mercure_gateway_audit_anchor_errors_total 0" in body


def test_metrics_expose_a_bookkeeper_down_since_boot(app, client: TestClient) -> None:
    """The P0-10 failure mode, made visible instead of latent.

    ``hub_streaming`` reads healthy here (the worker thread is alive, retrying)
    — outbox depth and the failure counter are what an unmanned box alerts on.
    """
    from unittest.mock import MagicMock, patch

    from mercure_gateway.hub_events import HubEventStreamer

    failing = MagicMock()
    failing.ok = False
    failing.status_code = 401
    with patch("requests.post", return_value=failing):
        streamer = HubEventStreamer(
            "https://hub.example", "key", "GW", max_batch_size=2
        )
        app.state.hub_streamer = streamer
        streamer.start()
        for _ in range(3):
            streamer.feed("STUDY_RECEIVED", {"study_uid": "1.2.3.4"})
        _wait_until(lambda: streamer.delivery_failures_total >= 3)
        streamer.stop()  # deterministic: not mid-POST, counters preserved

        body = client.get("/api/system/metrics").text

    assert "mercure_gateway_hub_outbox_depth 3" in body
    assert "mercure_gateway_hub_delivered_total 0" in body
    assert streamer.delivery_failures_total >= 3
    assert f"mercure_gateway_hub_delivery_failures_total {streamer.delivery_failures_total}" in body
    assert "mercure_gateway_hub_delivering 0" in body


def test_metrics_expose_anchor_verification_failure(app, client: TestClient) -> None:
    """A rewritten audit chain surfaces as anchor_ok 0 in the scrape feed."""
    from mercure_gateway.audit.anchoring import AnchorError, AnchorVerification

    class _Verifier:
        last_result = AnchorVerification(
            ok=False, errors=(AnchorError(1, "signature does not verify"),)
        )
        failures_total = 1

    app.state.anchor_verifier = _Verifier()  # type: ignore[assignment]
    body = client.get("/api/system/metrics").text
    assert "mercure_gateway_audit_anchor_ok 0" in body
    assert "mercure_gateway_audit_anchor_errors_total 1" in body


def test_metrics_expose_chain_verification_failure(app, client: TestClient) -> None:
    """A broken audit chain surfaces as chain_ok 0 in the scrape feed.

    This is the series that exists on *every* deployment, signed or not: the
    anchor series above is only meaningful when a hub public key is
    configured.  On the stock install this gauge is the whole integrity
    signal, so it must not be a constant 1.
    """
    from mercure_gateway.audit import ChainError, ChainVerification

    class _Verifier:
        last_result = ChainVerification(
            ok=False, errors=(ChainError(3, "a" * 64, "b" * 64, "hash mismatch"),)
        )
        failures_total = 2

    app.state.chain_verifier = _Verifier()  # type: ignore[assignment]
    body = client.get("/api/system/metrics").text
    assert "mercure_gateway_audit_chain_ok 0" in body
    assert "mercure_gateway_audit_chain_errors_total 2" in body


def test_metrics_chain_ok_before_the_first_pass(app, client: TestClient) -> None:
    """No finding yet: the first scheduled pass has not run (5s deferred).

    An unset verifier (tests, or a composition root that never wired one) and a
    wired one that has not completed a pass both report 1 — a boot-time zero
    would cry wolf on every restart.
    """
    body = client.get("/api/system/metrics").text
    assert "mercure_gateway_audit_chain_ok 1" in body

    class _Pending:
        last_result = None
        failures_total = 0

    app.state.chain_verifier = _Pending()  # type: ignore[assignment]
    body = client.get("/api/system/metrics").text
    assert "mercure_gateway_audit_chain_ok 1" in body
    assert "mercure_gateway_audit_chain_errors_total 0" in body
