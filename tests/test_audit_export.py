"""S04-T2 (RED): redacted export bundle (US-07, §7, §6.4).

One-click export produces a JSON bundle containing:
- The gateway configuration with all secrets redacted (no api_key / credential fields)
- The audit log as structured JSON with chain hashes
- PHI scoping honored per §6.4 (when phi_scope=minimal, patient_name/mrn are omitted)

The export is a static JSON file that can be transferred offline for support
or compliance audit without exposing secrets or PHI beyond the configured scope.
"""

from __future__ import annotations

import json

from mercure_gateway.audit import AuditLog
from mercure_gateway.config import default_config
from mercure_gateway.spool.db import mem_database


def test_export_is_valid_json() -> None:
    """Export produces a parseable JSON file."""
    db = mem_database()
    audit = AuditLog(db)
    audit.append("STUDY_RECEIVED", {"study_uid": "1.2.3.4"})
    cfg = default_config()

    bundle = audit.export_bundle(cfg)

    data = json.loads(bundle)
    assert "config" in data
    assert "audit" in data
    assert "events" in data["audit"]
    assert data["audit"]["count"] == 1


def test_export_redacts_api_key() -> None:
    """The hub_reporting.api_key must never appear in the export."""
    cfg = default_config()
    cfg.audit.hub_reporting.api_key = "super-secret-key-12345"
    db = mem_database()
    audit = AuditLog(db)

    bundle = json.loads(audit.export_bundle(cfg))

    hub = bundle["config"].get("audit", {}).get("hub_reporting", {})
    assert hub.get("api_key") == "***"


def test_export_redacts_credential_fields() -> None:
    """All credential entries must have their secret fields redacted."""
    from mercure_gateway.config import CredentialEntry, GatewayConfig

    cfg = GatewayConfig(
        credentials={
            "entries": {
                "hub": CredentialEntry(
                    type="sftp",
                    password_encrypted="AES256GCM:abc123",
                    private_key_encrypted="AES256GCM:def456",
                )
            }
        }
    )
    db = mem_database()
    audit = AuditLog(db)

    bundle = json.loads(audit.export_bundle(cfg))

    creds = bundle["config"].get("credentials", {}).get("entries", {})
    hub_entry = creds.get("hub", {})
    assert hub_entry.get("password_encrypted") == "***"
    assert hub_entry.get("private_key_encrypted") == "***"


def test_export_redacts_web_ui_password_hash() -> None:
    """auth_password_hash is redacted from the config export."""
    cfg = default_config()
    cfg.web_ui.auth_password_hash = "sha256$salt$hash"
    db = mem_database()
    audit = AuditLog(db)

    bundle = json.loads(audit.export_bundle(cfg))

    assert bundle["config"]["web_ui"]["auth_password_hash"] == "***"


def test_export_redacts_destination_secrets() -> None:
    """Per-destination fields like password, private_key, access_key_id are redacted."""
    from mercure_gateway.config import S3Destination

    cfg = default_config()
    cfg.destinations = [
        S3Destination(
            name="s3-backup",
            bucket="my-bucket",
            access_key_id="AKIA123",
            secret_access_key="abc123",
        )
    ]
    db = mem_database()
    audit = AuditLog(db)

    bundle = json.loads(audit.export_bundle(cfg))

    dest = bundle["config"]["destinations"][0]
    assert dest.get("access_key_id") == "***"
    assert dest.get("secret_access_key") == "***"


def test_export_minimal_phi_scope_omits_patient_fields() -> None:
    """When PHI scope is minimal, patient_name and mrn are not in the export."""
    cfg = default_config()
    cfg.audit.phi_scope = "minimal"
    db = mem_database()
    audit = AuditLog(db)
    audit.append(
        "STUDY_RECEIVED",
        {"study_uid": "1.2.3.4", "patient_name": "DOE^JOHN", "mrn": "12345"},
    )

    bundle = json.loads(audit.export_bundle(cfg))

    for event in bundle["audit"]["events"]:
        assert "patient_name" not in event["detail"]
        assert "mrn" not in event["detail"]
    assert bundle["audit"]["count"] == 1


def test_export_full_phi_scope_keeps_patient_fields() -> None:
    """When PHI scope is full, patient-identifying detail survives the export."""
    cfg = default_config()
    cfg.audit.phi_scope = "full"
    db = mem_database()
    audit = AuditLog(db)
    audit.append(
        "STUDY_RECEIVED",
        {"study_uid": "1.2.3.4", "patient_name": "DOE^JOHN", "mrn": "12345"},
    )

    bundle = json.loads(audit.export_bundle(cfg))

    event = bundle["audit"]["events"][0]
    assert event["detail"]["patient_name"] == "DOE^JOHN"
    assert event["detail"]["mrn"] == "12345"


def test_export_includes_chain_hash() -> None:
    """Exported audit events must include chain hashes for offline verification."""
    cfg = default_config()
    db = mem_database()
    audit = AuditLog(db)
    audit.append("STUDY_RECEIVED", {"study_uid": "1.2.3"})

    bundle = json.loads(audit.export_bundle(cfg))

    for event in bundle["audit"]["events"]:
        assert event.get("hash"), "export missing chain hash"


def test_export_head_hash_included() -> None:
    """The export includes the current head_hash for offline tamper evidence."""
    cfg = default_config()
    db = mem_database()
    audit = AuditLog(db)
    audit.append("STUDY_RECEIVED")

    bundle = json.loads(audit.export_bundle(cfg))

    assert "head_hash" in bundle["audit"]
    assert len(bundle["audit"]["head_hash"]) == 64
