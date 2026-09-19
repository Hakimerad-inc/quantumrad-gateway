"""Config redaction helper shared by the web API and audit export (US-07).

Secrets are never serialized in their real form: destination credentials,
credential-entry encrypted blocks, hub reporting API key and the web UI
password hash are replaced with ``"***"`` before they leave the gateway
(either via ``GET /api/config`` or the audit export bundle).
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import Field, field_validator, model_validator

from mercure_gateway.config import GatewayConfig, WebUIConfig

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

__all__ = ["RedactedGatewayConfig", "redact_config"]


class _RedactedWebUI(WebUIConfig):
    """The web-UI section as ``GET /api/config`` returns it.

    The real :class:`WebUIConfig` rejects any hash that is not a known scheme,
    which is what makes a mistyped hash a save-time error instead of a lockout
    (review P0-8). The redacted view carries ``"***"`` in that field, which is
    not a hash and never reaches a login, so the strict validators are relaxed
    here — this is the response *model*, not the write boundary.
    """

    auth_password_hash: str = Field(
        default="",
        description="pbkdf2$/sha256$ hash, or '***' when redacted by the API.",
    )

    @field_validator("auth_password_hash")
    @classmethod
    def _hash_is_a_known_scheme(cls, value: str) -> str:
        return value

    @model_validator(mode="after")
    def _auth_needs_a_hash(self) -> _RedactedWebUI:
        return self


class RedactedGatewayConfig(GatewayConfig):
    """The response model for ``GET /api/config``.

    Declaring it (rather than letting the endpoint return a bare dict) is what
    exports the real schema to the SPA's generated TypeScript types — so a
    merge writing a receiver field into the general section fails at compile
    time instead of 400ing at runtime (review P1-9).
    """

    web_ui: _RedactedWebUI = Field(default_factory=_RedactedWebUI)


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
