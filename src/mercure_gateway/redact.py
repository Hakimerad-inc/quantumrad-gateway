"""Config redaction helper shared by the web API and audit export (US-07).

Secrets are never serialized in their real form: destination credentials,
credential-entry encrypted blocks, hub reporting API key and the web UI
password hash are replaced with ``"***"`` before they leave the gateway
(either via ``GET /api/config`` or the audit export bundle).
"""

from __future__ import annotations

import json
from typing import Any

# Secret-bearing fields per destination type (never returned by the API).
DESTINATION_SECRET_FIELDS = (
    "password",
    "private_key",
    "passphrase",
    "secret_access_key",
    "access_key_id",
    "auth_token",
)

CREDENTIAL_ENTRY_FIELDS = (
    "password_encrypted",
    "private_key_encrypted",
    "passphrase_encrypted",
    "api_key_encrypted",
)

__all__ = ["redact_config"]


def redact_config(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *data* with all sensitive fields replaced by '***'."""
    redacted: dict[str, Any] = json.loads(json.dumps(data))  # deep copy
    for destination in redacted.get("destinations", []):
        for key in DESTINATION_SECRET_FIELDS:
            if destination.get(key):
                destination[key] = "***"
    for entry in redacted.get("credentials", {}).get("entries", {}).values():
        for key in CREDENTIAL_ENTRY_FIELDS:
            if entry.get(key):
                entry[key] = "***"
    hub = redacted.get("audit", {}).get("hub_reporting", {})
    if hub.get("api_key"):
        hub["api_key"] = "***"
    web_ui = redacted.get("web_ui", {})
    if web_ui.get("auth_password_hash"):
        web_ui["auth_password_hash"] = "***"
    update = redacted.get("update", {})
    if update.get("public_key"):
        update["public_key"] = "***"
    if hub.get("anchor_public_key"):
        # Same treatment as update.public_key: not secret-class in principle,
        # but the sentinel preserves the GET → PUT config round-trip (review
        # M8/M4 — a lost field would silently disable signed anchoring).
        hub["anchor_public_key"] = "***"
    return redacted
