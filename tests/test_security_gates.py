"""S09-T3 (RED): Security gates (§10, §6.3).

Pins the security surface: TLS config models, AE allow-list enforcement,
secrets-not-in-config sweep, and audit tamper-evidence re-verification.

The pip-audit dependency scanning is a CI job (``ci.yml``) not a unit test.
"""

from __future__ import annotations

from mercure_gateway.config import (
    DICOMTLSDestination,
    DICOMwebDestination,
    GatewayConfig,
    ReceiverConfig,
    S3Destination,
    SFTPDestination,
    default_config,
)
from mercure_gateway.redact import DESTINATION_SECRET_FIELDS, redact_config

# ══════════════════════════════════════════════════════════════════════
# TLS config model validation
# ══════════════════════════════════════════════════════════════════════

def test_dicom_tls_verify_peer_default_true() -> None:
    """DICOM-TLS destination defaults to verify_peer=True (no silent downgrade)."""
    tls = DICOMTLSDestination(name="tls", host="pacs.local", port=11112, aet_target="PACS")
    assert tls.verify_peer is True


def test_dicom_tls_cacert_optional() -> None:
    """cacert is optional (None) — platform CA store is the fallback."""
    tls = DICOMTLSDestination(name="tls", host="pacs.local", port=11112, aet_target="PACS")
    assert tls.cacert is None


def test_s3_use_https_default_true() -> None:
    """S3 destination defaults to use_https=True (no silent HTTP downgrade)."""
    s3 = S3Destination(name="s3", bucket="test-bucket")
    assert s3.use_https is True


# ══════════════════════════════════════════════════════════════════════
# AE allow-list
# ══════════════════════════════════════════════════════════════════════

def test_receiver_allowed_ae_titles_default_empty() -> None:
    """allowed_ae_titles defaults to empty (accept any)."""
    rc = ReceiverConfig(port=11112)
    assert rc.allowed_ae_titles == []


def test_receiver_allowed_ae_titles_accepts_list() -> None:
    """allowed_ae_titles can be populated to restrict incoming AE titles."""
    rc = ReceiverConfig(port=11112, allowed_ae_titles=["MODALITY1", "MODALITY2"])
    assert rc.allowed_ae_titles == ["MODALITY1", "MODALITY2"]


# ══════════════════════════════════════════════════════════════════════
# Secrets-not-in-config sweep
# ══════════════════════════════════════════════════════════════════════

def test_secret_fields_are_covered_by_redact() -> None:
    """Every DESTINATION_SECRET_FIELDS entry is redacted by redact_config."""
    cfg = GatewayConfig(
        destinations=[
            SFTPDestination(
                name="sftp",
                host="sftp.local",
                port=22,
                username="user",
                password="s3cret",
                private_key="key",
                passphrase="phrase",
            ),
            S3Destination(
                name="s3",
                bucket="b",
                secret_access_key="sak",
                access_key_id="aki",
            ),
            DICOMwebDestination(name="web", url="https://pacs.local/dicomweb", auth_token="tok"),
        ]
    )
    raw = cfg.model_dump()
    redacted = redact_config(raw)

    for dest in redacted.get("destinations", []):
        for field in DESTINATION_SECRET_FIELDS:
            if dest.get(field):
                assert dest[field] == "***", f"{field} not redacted (got {dest[field]!r})"


def test_default_config_has_no_secrets() -> None:
    """default_config() should never contain real secret values."""
    cfg = default_config()
    raw = cfg.model_dump()
    for dest in raw.get("destinations", []):
        for field in DESTINATION_SECRET_FIELDS:
            assert dest.get(field) in (None, ""), f"{field} has unexpected default value"
    assert raw.get("web_ui", {}).get("auth_password_hash") == ""


# ══════════════════════════════════════════════════════════════════════
# Audit tamper-evidence re-verification
# ══════════════════════════════════════════════════════════════════════

def test_audit_chain_verify_no_errors() -> None:
    """A fresh audit log verifies clean (no forged events)."""
    from mercure_gateway.audit import AuditLog
    from mercure_gateway.spool.db import mem_database

    db = mem_database()
    audit = AuditLog(db)
    audit.append("TEST_EVENT", {"msg": "hello"})
    ok, errors = audit.verify()
    assert ok is True
    assert errors == []


# ══════════════════════════════════════════════════════════════════════
# Pip-audit is a CI job — this test verifies the tool is available
# ══════════════════════════════════════════════════════════════════════

def test_pip_audit_available() -> None:
    """pip-audit CLI is installed (CI job runs it; dev machines should too)."""
    import subprocess

    result = subprocess.run(
        ["uvx", "pip-audit", "--version"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"pip-audit not available: {result.stderr}"
    assert "pip-audit" in result.stdout
