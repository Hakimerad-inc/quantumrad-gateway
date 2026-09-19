"""Pydantic v2 configuration models for ``mercure-gateway.json``.

The schema mirrors the draft configuration in the product spec (PRD §5.5)
and the product refinement spec:
``general``, ``receiver``, ``destinations[]`` (per-target-type fields),
``forwarding``, ``forwarding_rules[]``, ``reports``, ``audit``, ``storage``,
``web_ui``, ``credentials``, and ``usb_mode``.

Deviation note (per quality bar):
- PRD §5.5 lists target types DICOM / DICOM-TLS / DICOMweb / SFTP / rsync / Folder / S3 / XNAT.
  ``dicom_tls`` is modelled as an explicit destination type here (``type="dicom_tls"``)
  so TLS settings stay on the target, matching mercure's target-type conventions.
- Refinement additions: ``forwarding`` (concurrency), ``web_ui`` (localhost SPA),
  ``credentials`` (encrypted per-destination blocks), ``usb_mode`` (portable USB variant).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, get_origin

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

logger = logging.getLogger(__name__)

# Non-secret placeholder written to disk in place of an encrypted secret field.
# Distinct from the ``***`` redaction sentinel used by the web API, and non-empty
# so ``min_length=1`` secret fields (e.g. the XNAT password) still validate on
# load. Defined here — next to the models that have to accept it — because
# ``config/encryption.py`` imports this package and cannot be imported back.
ENC_PLACEHOLDER = "__ENCRYPTED_AT_REST__"

__all__ = [
    "AuditConfig",
    "BaseDestination",
    "CredentialEntry",
    "CredentialsConfig",
    "DICOMDestination",
    "DICOMTLSDestination",
    "DICOMwebDestination",
    "Destination",
    "FolderDestination",
    "ForwardingConfig",
    "ForwardingRule",
    "GatewayConfig",
    "GeneralConfig",
    "HubReporting",
    "ReportConfig",
    "ReportQuerySource",
    "ReceiverConfig",
    "RsyncDestination",
    "S3Destination",
    "SFTPDestination",
    "StorageConfig",
    "UpdateConfig",
    "USBModeConfig",
    "WebUIConfig",
    "XNATDestination",
    "apply_env_overrides",
    "insecure_bind_reason",
    "apply_usb_defaults",
    "default_config",
    "detect_usb_mode",
    "is_removable_volume",
    "load_config",
    "save_config",
]


class _StrictConfigModel(BaseModel):
    """Base for every config model: unknown keys are rejected.

    The config schema is the product's contract, but nothing enforced it
    (review P0-4) — a typo like ``general.ae_title`` (which the Setup Wizard
    was writing until P0-4's sibling fix) was silently dropped on load, so the
    appliance ran with a field the operator believed they had set. Pydantic's
    default ``extra="ignore"`` is right for parsing foreign data, wrong for the
    one document this process is authoritative for.

    Pydantic v2 does **not** propagate ``model_config`` to nested classes, so
    this has to be re-parented onto each model explicitly — setting it on
    ``GatewayConfig`` alone changes nothing for ``GeneralConfig``.

    ``_healed_unknown_keys`` records what :func:`load_config` pruned from a
    file, so the boot path can preserve the original and the import endpoint
    can tell the operator which of their settings did not survive. It is a
    private attribute, so it never serialises into a round-tripped body.
    """

    model_config = ConfigDict(extra="forbid")
    _healed_unknown_keys: list[list[Any]] = PrivateAttr(default_factory=list)


class GeneralConfig(_StrictConfigModel):
    """Top-level gateway identity and runtime settings."""

    appliance_name: str = "Gateway-CLI-01"
    locale: str = "en"
    log_level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")


class ReceiverConfig(_StrictConfigModel):
    """Local DICOM C-STORE SCP settings.

    ``port=0`` is allowed in code/tests: it asks the OS for an ephemeral port
    (resolved after the transport binds; see ``Receiver.port``). Config files
    should always set a real port.
    """

    ae_title: str = Field(default="GATEWAY", min_length=1, max_length=16)
    port: int = Field(default=11112, ge=0, le=65535)
    max_associations: int = Field(
        default=25,
        ge=1,
        le=256,
        description=(
            "Maximum concurrent DICOM associations (US-01 AC: ≥25 modalities "
            "can push simultaneously)."
        ),
    )
    decompress_common: bool = Field(
        default=True,
        description=(
            "Decompress common compressed syntaxes (JPEG 2000, JPEG-LS, RLE) on receive. "
            "Rare/proprietary syntaxes are passed through as-is."
        ),
    )
    auto_enqueue_delay_sec: float = Field(
        default=5.0,
        ge=0.0,
        description=(
            "Idle window (seconds) after the last received instance before a study "
            "is auto-enqueued to the enabled destinations (US-03). The delay "
            "debounces multi-instance studies so the forwarder never delivers a "
            "half-received study; modalities do not signal end-of-study."
        ),
    )
    allowed_ae_titles: list[str] = Field(
        default_factory=list, description="Empty = accept any AE title."
    )


class BaseDestination(_StrictConfigModel):
    """Common fields shared by every destination target."""

    name: str = Field(min_length=1)
    enabled: bool = True
    timeout_sec: float | None = Field(
        default=None,
        ge=1.0,
        description=(
            "Connect/association timeout in seconds. None uses the transport's "
            "default (DIMSE 30 s, SFTP 30 s, rsync 300 s). A peer that neither "
            "connects nor rejects within this window fails the delivery and the "
            "study retries on the next pass — without it, one hung destination "
            "halts all delivery (review P1-12)."
        ),
    )


class DICOMDestination(BaseDestination):
    """C-STORE (SCU) destination — mercure hub receiver or vendor PACS."""

    type: Literal["dicom"] = "dicom"
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    aet_target: str = Field(min_length=1, max_length=16)
    aet_source: str = Field(default="GATEWAY", min_length=1, max_length=16)


class DICOMTLSDestination(BaseDestination):
    """C-STORE over DICOM-TLS (AES/TLS)."""

    type: Literal["dicom_tls"] = "dicom_tls"
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    aet_target: str = Field(min_length=1, max_length=16)
    aet_source: str = Field(default="GATEWAY", min_length=1, max_length=16)
    verify_peer: bool = True
    cacert: str | None = None


class DICOMwebDestination(BaseDestination):
    """RESTful DICOMweb target (STOW-RS)."""

    type: Literal["dicomweb"] = "dicomweb"
    url: str = Field(min_length=1)
    aet: str | None = None
    auth_token: str | None = None


class SFTPDestination(BaseDestination):
    """SFTP (SSH file transfer) target."""

    type: Literal["sftp"] = "sftp"
    host: str = Field(min_length=1)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1)
    password: str | None = None
    private_key: str | None = None
    passphrase: str | None = None
    remote_path: str = "/"
    # Path to an OpenSSH ``known_hosts`` file used to verify the server's host
    # key. Required for secure operation: an unset value means no keys are
    # trusted and every connection is rejected (review H4). The recommended
    # setup pre-seeds this file with the PACS host key (``ssh-keyscan``).
    known_hosts: str = ""


class RsyncDestination(BaseDestination):
    """rsync-over-SSH target."""

    type: Literal["rsync"] = "rsync"
    host: str = Field(min_length=1)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1)
    remote_path: str = Field(min_length=1)


class S3Destination(BaseDestination):
    """S3-compatible object-store target."""

    type: Literal["s3"] = "s3"
    bucket: str = Field(min_length=1)
    endpoint_url: str | None = None
    region: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    remote_prefix: str = ""
    use_https: bool = True


class FolderDestination(BaseDestination):
    """Local/network folder target (drop folder)."""

    type: Literal["folder"] = "folder"
    path: str = Field(min_length=1)


class XNATDestination(BaseDestination):
    """XNAT server target."""

    type: Literal["xnat"] = "xnat"
    url: str = Field(min_length=1)
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    project: str = Field(min_length=1)
    subject: str | None = None


Destination = Annotated[
    DICOMDestination
    | DICOMTLSDestination
    | DICOMwebDestination
    | SFTPDestination
    | RsyncDestination
    | S3Destination
    | FolderDestination
    | XNATDestination,
    Field(discriminator="type"),
]


class ForwardingRule(_StrictConfigModel):
    """Optional advanced routing rule (v1.1). Evaluated against extracted DICOM tags."""

    rule: str = Field(min_length=1)
    targets: list[str] = Field(min_length=1)
    priority: Literal["normal", "high", "low"] = "normal"


class ReportQuerySource(_StrictConfigModel):
    """PACS endpoint used for report retrieval (C-FIND/C-MOVE)."""

    type: Literal["dicom", "dicomweb", "fhir", "hl7"] = "dicom"
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    aet: str = Field(min_length=1, max_length=16)
    timeout_sec: float | None = Field(
        default=None,
        ge=1.0,
        description=(
            "Association timeout for the report C-FIND/C-MOVE. None uses the "
            "DIMSE default (30 s). A PACS that never answers the association "
            "would otherwise hang the report poller and stop retrieval "
            "silently (review P1-12)."
        ),
    )


_DEFAULT_REPORT_TYPES: list[Literal["sr", "pdf"]] = ["sr", "pdf"]


class ReportConfig(_StrictConfigModel):
    """Report-retrieval configuration."""

    enabled: bool = False
    query_source: ReportQuerySource | None = None
    # C-MOVE is a *pull*: the PACS opens a second association back to us, to the
    # AE title it has registered for this gateway. That AE must be reachable on
    # a fixed, known port — the move destination is looked up by AE title in the
    # PACS config, so an ephemeral port cannot work. Default is one above the
    # receiver's default 11112.
    store_scp_port: int = Field(
        default=11113,
        ge=1,
        le=65535,
        description=(
            "Port for the report C-STORE SCP the PACS C-MOVEs reports into. "
            "Must match the port registered for this gateway's AE title on the "
            "PACS (a C-MOVE destination is resolved by AE title)."
        ),
    )
    poll_interval_sec: int = Field(default=300, ge=10)
    sla_seconds: int = Field(
        default=300,
        ge=1,
        description="SLA window (seconds) for report retrieval. Exceeded → SLA_EXPIRED event (K3).",
    )
    on_retrieval: Literal["store", "store_and_forward"] = "store"
    report_types: list[Literal["sr", "pdf"]] = Field(
        default_factory=lambda: list(_DEFAULT_REPORT_TYPES),
        description=(
            "Which report SOP classes to retrieve. "
            "'sr' = DICOM Structured Report (1.2.840.10008.5.1.4.1.1.88.33); "
            "'pdf' = Encapsulated PDF (1.2.840.10008.5.1.4.1.1.104.2)."
        ),
    )


class HubReporting(_StrictConfigModel):
    """Optional streaming of audit events to a mercure hub bookkeeper (v1.1)."""

    enabled: bool = False
    bookkeeper_url: str = ""
    api_key: str = ""
    # Hub's Ed25519 public key (PEM or raw base64) for signed head anchors
    # (review M4). Presence of the key enables signed anchoring — no separate
    # boolean; empty means file-only anchoring.
    anchor_public_key: str = ""


class AuditConfig(_StrictConfigModel):
    """Local tamper-evident audit log configuration."""

    local: bool = True
    encrypt: bool = True
    phi_scope: Literal["minimal", "full"] = Field(
        default="minimal",
        description=(
            "PHI scoping for audit events and exports (§6.4). 'minimal' omits "
            "patient-identifying fields (patient_name, mrn) from audit detail "
            "and exports; 'full' records them. Default = minimal."
        ),
    )
    retention_days: int = Field(
        default=365,
        ge=1,
        description=(
            "Audit log retention window (days). Events older than this are "
            "pruned (default 1 year per PRD §7). Applies to the audit chain "
            "and the rotating text log."
        ),
    )
    hub_reporting: HubReporting = Field(default_factory=HubReporting)


class UpdateConfig(_StrictConfigModel):
    """Auto-update settings (PRD §2.3 Q5, ADR-0006).

    The updater is fail-closed: without ``public_key`` configured, every
    signature check rejects the archive, so the default state is
    "updates disabled" rather than "updates unverified".
    """

    enabled: bool = Field(
        default=False,
        description="Check the update endpoint at startup (never auto-installs).",
    )
    update_url: str = Field(
        default="",
        description="URL of the signed update manifest (latest.json).",
    )
    public_key: str = Field(
        default="",
        description=(
            "Ed25519 public key (PEM or raw base64) used to verify update "
            "archives. Empty = updates cannot verify (fail-closed)."
        ),
    )


def _default_spool_dir() -> str:
    """Platform-appropriate default spool directory.

    Windows: ``C:\\mercure-gateway\\spool`` (PRD baseline).  Elsewhere the
    user's data dir (``~/.local/share/mercure-gateway/spool``) — a hardcoded
    ``C:\\...`` string would otherwise become a *literal relative directory
    name* on POSIX systems.
    """
    if sys.platform == "win32":
        return "C:\\mercure-gateway\\spool"
    return str(Path.home() / ".local" / "share" / "mercure-gateway" / "spool")


class StorageConfig(_StrictConfigModel):
    """Spool directory and retention settings."""

    spool_dir: str = Field(
        default_factory=_default_spool_dir,
        description="Root directory for the DICOM spool and database.",
    )
    max_spool_gb: int = Field(
        default=20,
        ge=1,
        description=(
            "Maximum spool size in GiB of persisted DICOM instances. Enforced "
            "by the disk monitor: delivered studies are purged oldest-first "
            "when exceeded (review M3); undelivered studies are never removed."
        ),
    )
    retention_delivered_days: int = Field(default=3, ge=0)
    disk_full_warning_pct: int = Field(
        default=90,
        ge=50,
        le=100,
        description="Capacity percentage at which a disk-full warning is emitted.",
    )
    purge_on_disk_full: bool = Field(
        default=False,
        description=(
            "Automatically purge oldest delivered studies when capacity exceeds "
            "'disk_full_warning_pct'. Undelivered/FAILED studies are never purged."
        ),
    )


class ForwardingConfig(_StrictConfigModel):
    """Concurrent forwarding worker settings (product refinement spec §2.2)."""

    concurrency: int = Field(
        default=3,
        ge=1,
        le=16,
        description="Maximum number of concurrent forwarding workers.",
    )
    queue_poll_interval_ms: int = Field(
        default=500,
        ge=100,
        le=5000,
        description="How often workers poll for new tasks (milliseconds).",
    )


class WebUIConfig(_StrictConfigModel):
    """Web admin panel settings (product refinement spec §7)."""

    host: str = Field(
        default="127.0.0.1",
        description="Bind address for the web admin panel. Use 0.0.0.0 for network access.",
    )
    port: int = Field(default=8080, ge=1, le=65535)
    auth_enabled: bool = Field(
        default=False,
        description=(
            "Require password authentication for the web UI. "
            "Disabled by default for localhost-only access; enable for shared machines."
        ),
    )
    auth_password_hash: str = Field(
        default="",
        description=(
            "Hash of the web UI password. Created as "
            "``pbkdf2$<iterations>$<salt hex>$<derived key hex>`` by the "
            "``--set-web-password`` CLI, the setup wizard, or "
            "``PUT /api/web-ui/password``; the legacy ``sha256$salt$hex`` form "
            "from older installs still verifies. Empty means no password is "
            "set — which must not be combined with auth_enabled (see "
            "_auth_needs_a_hash), or the panel becomes unloggable."
        ),
    )
    tls_cert_file: str = Field(
        default="",
        description=(
            "PEM certificate for the web admin panel (ADR-0007). Empty = plain HTTP. "
            "Must be paired with tls_key_file; intended for non-loopback binds, "
            "where the unauthenticated panel would otherwise leak PHI."
        ),
    )
    tls_key_file: str = Field(
        default="",
        description="PEM private key matching tls_cert_file. Empty = plain HTTP.",
    )

    @model_validator(mode="after")
    def _tls_needs_both_files(self) -> WebUIConfig:
        """Half a TLS pair is a config error — never a silent HTTP downgrade."""
        if bool(self.tls_cert_file) != bool(self.tls_key_file):
            raise ValueError("web_ui.tls_cert_file and web_ui.tls_key_file must be set together")
        return self

    @model_validator(mode="after")
    def _auth_needs_a_hash(self) -> WebUIConfig:
        """Enabling auth with no hash set is an unrecoverable lockout (P0-8).

        Nothing can log in — there is no password to type — and the panel is
        the only way to fix it, so the appliance would need its config file
        hand-edited. Reject the combination at the write boundary instead of
        discovering it at the login screen.
        """
        if self.auth_enabled and not self.auth_password_hash:
            raise ValueError(
                "web_ui.auth_enabled is true but web_ui.auth_password_hash is "
                "empty — set a password first (--set-web-password, the setup "
                "wizard, or PUT /api/web-ui/password), or the panel cannot be "
                "logged into."
            )
        return self

    @field_validator("auth_password_hash")
    @classmethod
    def _hash_is_a_known_scheme(cls, value: str) -> str:
        """Reject a garbage hash at save time, not at the login screen.

        A free-text field that silently accepts anything means a mistyped hash
        is discovered only when nobody can log in (P0-8). Empty is allowed — it
        pairs with ``auth_enabled=false`` and is caught by ``_auth_needs_a_hash``.
        ``__ENCRYPTED_AT_REST__`` is the storage placeholder written by
        :func:`save_config`; it never reaches a login and is replaced by the
        real hash when the file is read back.
        """
        if not value or value == ENC_PLACEHOLDER:
            return value
        if value.startswith(("pbkdf2$", "sha256$")) or value.startswith(("$2a$", "$2b$", "$2y$")):
            return value
        raise ValueError(
            "web_ui.auth_password_hash is not a recognised hash — expected "
            "pbkdf2$<iters>$<salt hex>$<key hex>, legacy sha256$salt$hex, or a "
            "bcrypt $2b$ hash. Create one with --set-web-password."
        )


class CredentialEntry(_StrictConfigModel):
    """Encrypted credential block for a single destination."""

    type: str = Field(min_length=1, description='Destination type (e.g. "sftp", "s3").')
    username: str | None = None
    password_encrypted: str | None = Field(
        default=None,
        description='AES-256-GCM encrypted password ("AES256GCM:...").',
    )
    private_key_encrypted: str | None = Field(
        default=None,
        description="AES-256-GCM encrypted private key (SSH/SFTP).",
    )
    passphrase_encrypted: str | None = Field(
        default=None,
        description="AES-256-GCM encrypted passphrase for the private key.",
    )
    api_key_encrypted: str | None = Field(
        default=None,
        description="AES-256-GCM encrypted API key (S3, XNAT, DICOMweb, hub reporting).",
    )


class CredentialsConfig(_StrictConfigModel):
    """Encrypted credential storage (product refinement spec §2.5).

    Credentials are stored as AES-256-GCM encrypted blocks, decrypted with a
    master password on startup via PBKDF2 (100k iterations, SHA-256).  When
    ``encrypted`` is false, credentials are stored in plaintext (dev/test only).
    """

    encrypted: bool = Field(
        default=True,
        description="Encrypt credential values at rest.  Disable for dev/test only.",
    )
    salt: str | None = Field(
        default=None,
        description=(
            "Base64-encoded PBKDF2 salt.  Set once when the configuration is "
            "first locked with a master password.  Must be present when "
            "``encrypted`` is true."
        ),
    )
    entries: dict[str, CredentialEntry] = Field(
        default_factory=dict,
        description="Per-destination credential blocks, keyed by destination name.",
    )


class USBModeConfig(_StrictConfigModel):
    """USB dongle variant settings (usb-dongle-gateway-spec §6)."""

    enabled: bool = Field(
        default=False,
        description=(
            "Enable USB-specific behavior (aggressive retention, storage budget, "
            "hot-unplug monitoring).  Auto-detected when spool is on removable media."
        ),
    )
    storage_budget_gb: int = Field(
        default=18,
        ge=1,
        description="Maximum data partition usage in GB (shared data partition).",
    )
    retention_delivered_hours: int = Field(
        default=24,
        ge=1,
        description="Aggressive retention for USB: delivered studies purged after N hours.",
    )
    hot_unplug_safe: bool = Field(
        default=True,
        description="Enable graceful shutdown on USB removal detection.",
    )
    flush_timeout_sec: float = Field(
        default=10.0,
        gt=0,
        description=(
            "Max seconds the hot-unplug flush may run before removal is forced (K10: flush ≤10 s)."
        ),
    )
    auto_start_on_boot: bool = Field(
        default=True,
        description="Auto-start gateway on USB boot or plug-in.",
    )
    led_enabled: bool = Field(
        default=False,
        description="Hardware LED status indicator support (requires compatible device).",
    )
    led_pin: str = Field(
        default="GPIO18",
        description="GPIO pin for LED control in Linux mode (BCM numbering).",
    )


class GatewayConfig(_StrictConfigModel):
    """Root model for ``mercure-gateway.json``."""

    config_version: str = Field(
        default="1.0",
        description="Configuration schema version for migration compatibility.",
    )
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    receiver: ReceiverConfig = Field(default_factory=ReceiverConfig)
    destinations: list[Destination] = Field(default_factory=list)
    forwarding: ForwardingConfig = Field(default_factory=ForwardingConfig)
    forwarding_rules: list[ForwardingRule] = Field(default_factory=list)
    reports: ReportConfig = Field(default_factory=ReportConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    update: UpdateConfig = Field(default_factory=UpdateConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    web_ui: WebUIConfig = Field(default_factory=WebUIConfig)
    credentials: CredentialsConfig = Field(default_factory=CredentialsConfig)
    usb_mode: USBModeConfig = Field(default_factory=USBModeConfig)

    @model_validator(mode="after")
    def _warn_on_stale_forwarding_rule_targets(self) -> GatewayConfig:
        """Warn (not reject) when a forwarding rule names no destination.

        A rule target that was renamed, removed, or copied in from another
        profile narrows the routed set silently. ``Spool.enqueue`` already
        refuses to strand a study when *every* target is stale (f8c3250), but
        the operator still wants the misconfiguration surfaced as early as
        config load rather than at the first received study.
        """
        if not self.forwarding_rules:
            return self
        known = {d.name for d in self.destinations}
        for rule in self.forwarding_rules:
            stale = [t for t in rule.targets if t not in known]
            if stale:
                logger.warning(
                    "forwarding rule %r targets unknown destination(s) %s — known destinations: %s",
                    rule.rule,
                    ", ".join(sorted(stale)),
                    ", ".join(sorted(known)) or "(none configured)",
                )
        return self


def default_config() -> GatewayConfig:
    """Return a validated configuration populated with PRD defaults."""
    return GatewayConfig()


def _resolve_master_password(
    master_password: str | None,
    config_path: str | Path | None = None,
    *,
    encryption_enabled: bool = True,
) -> str | None:
    """Return *master_password* if given, else resolve or generate one.

    A default install has nothing configured, and an unmastered config writes
    every secret to disk in cleartext (review P1-1) — so an appliance that
    wants encryption at rest gets a key on first boot instead. See
    :func:`mercure_gateway.config.encryption.resolve_or_create_master_password`.
    """
    if master_password is not None:
        return master_password
    from .encryption import resolve_or_create_master_password

    return resolve_or_create_master_password(
        config_path, encryption_enabled=encryption_enabled
    )


def _prune_extra_keys(payload: Any, errors: list[Any]) -> Any:
    """Return a deep copy of *payload* without the ``extra_forbidden`` locations.

    Each error's ``loc`` is a path (``("general", "ae_title")``) into the parsed
    document. ``copy.deepcopy`` is taken first because the payload may be an
    arbitrary object graph Pydantic built from the input; mutating it in place
    would corrupt the caller's copy.
    """
    import copy

    pruned = copy.deepcopy(payload)
    for err in errors:
        loc = list(err.get("loc", ()))
        if not loc:
            continue
        parent, leaf = loc[:-1], loc[-1]
        node: Any = pruned
        for part in parent:
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        if isinstance(node, dict) and leaf in node:
            del node[leaf]
    return pruned


# Bind hosts that are single-user by definition (admin guide §Authentication,
# and web/auth.py's no-op-when-auth-off argument rests on loopback being
# trusted — everything else needs auth or the escape hatch below).
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

# Deliberate deployments (dev rigs, sidecar frontends behind their own auth)
# opt out of the refusal. It downgrades a boot refusal to a loud warning and
# silences the write-boundary 409 — the operator has stated the posture.
_ALLOW_INSECURE_BIND_ENV = "MERCURE_GATEWAY_ALLOW_INSECURE_BIND"


def insecure_bind_reason(
    config: GatewayConfig, environ: Mapping[str, str] | None = None
) -> str | None:
    """Return why *config* would expose an unauthenticated admin API, or None.

    With ``web_ui.auth_enabled`` false the admin API — PHI, credentials,
    receiver/forwarder start/stop — is open to whoever can reach the bind
    address. Loopback (127.0.0.1, localhost, ::1) is single-user by definition.
    The escape hatch returns None as well; the caller decides whether that is a
    boot refusal (main) or a warning.

    Pure on purpose, so the boot check and the write boundary
    (``PUT /api/config``) cannot drift apart: applying this to a *prospective*
    config before it is persisted is what stops an operator from saving an
    open panel onto a networked appliance one click at a time (review P0-3).
    """
    ui = config.web_ui
    env = os.environ if environ is None else environ
    if ui.auth_enabled or ui.host in _LOOPBACK_HOSTS:
        return None
    if env.get(_ALLOW_INSECURE_BIND_ENV, "") == "1":
        return None
    return (
        f"web_ui.auth_enabled is false while binding to {ui.host!r}: the admin "
        "API (PHI, credentials, start/stop) would be unauthenticated on the "
        "network. Enable web_ui auth, set web_ui.host to 127.0.0.1, or set "
        f"{_ALLOW_INSECURE_BIND_ENV}=1 for a deliberate deployment."
    )


def _prune_for_disk(payload: Any, locs: list[list[Any]]) -> Any:
    """Re-prune *payload* by the recorded ``loc`` paths, for the one-shot rewrite.

    :func:`_prune_extra_keys` operates on the error objects Pydantic returns;
    this variant takes the already-recorded locs straight from the healed
    config, so the file rewrite prunes exactly what validation dropped.
    """
    import copy

    pruned = copy.deepcopy(payload)
    for loc in locs:
        parent, leaf = loc[:-1], loc[-1]
        node: Any = pruned
        for part in parent:
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        if isinstance(node, dict) and leaf in node:
            del node[leaf]
    return pruned


def normalize_and_validate(payload: Any, *, source: str | Path) -> GatewayConfig:
    """Validate *payload*, healing unknown keys when it came from disk.

    Two boundaries, one model (review P0-4):

    * The **write** boundary (``PUT /api/config``) stays strict: the SPA
      round-trips ``GET /config``, whose body is ``model_dump_json()`` and is
      therefore key-clean, so an extra key reaching that endpoint is a client
      bug a 400 should name rather than a file to paper over.
    * The **boot** boundary (``load_config``) cannot be strict without bricking
      every appliance on upgrade — an older build's config carrying a field the
      new schema dropped, or a file edited by hand with a typo, would make the
      whole appliance unbootable. So a file whose *only* problem is unknown
      keys is healed: the offending paths are pruned, logged by name, the
      original is preserved beside it, and validation is retried.

    *source* is the path the payload was read from, used for the backup and the
    log. Pass ``source="request"`` (or any non-path sentinel) to keep the
    strict behaviour for an in-memory payload.
    """
    try:
        return GatewayConfig.model_validate(payload)
    except ValidationError as exc:
        errors = list(exc.errors())
        if not all(e["type"] == "extra_forbidden" for e in errors):
            raise
        logger.warning(
            "config %s: ignoring %d unknown key(s) — the schema is strict, but "
            "a config file is healed rather than refusing to boot: %s",
            source,
            len(errors),
            ", ".join(".".join(str(p) for p in e["loc"]) for e in errors),
        )
        healed = normalize_and_validate(_prune_extra_keys(payload, errors), source=source)
        healed._healed_unknown_keys = [  # noqa: SLF001 — surfaced to the caller
            list(e["loc"]) for e in errors
        ]
        return healed


def load_config(path: str | Path, *, master_password: str | None = None) -> GatewayConfig:
    """Load and strictly validate ``mercure-gateway.json`` from ``path``.

    Unknown keys are healed rather than fatal (see
    :func:`normalize_and_validate`): the offending paths are pruned from the
    file, the operator's original is preserved beside it as
    ``<path>.unknown-keys.bak``, and every dropped key is named in the log.
    Healing is the *only* case where a boot rewrites the config file — it
    rewrites the raw JSON it just read, so no secret round-trips through a
    model and encryption placeholders stay exactly as written. Every other
    validation error still raises: an unreadable config is better surfaced as a
    boot failure than silently ignored.

    When the config is encrypted at rest, secrets are decrypted back into the
    in-memory model using the master password (from *master_password* or the
    ``MERCURE_MASTER_PASSWORD`` / ``MERCURE_MASTER_PASSWORD_FILE`` env vars).
    """
    path = Path(path)
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    config = normalize_and_validate(payload, source=path)

    unknown = getattr(config, "_healed_unknown_keys", None)
    if unknown:
        # Rewrite the pruned JSON and preserve the operator's original beside
        # it. Both writes are best-effort: read-only media (a sealed USB
        # appliance, a read-mounted partition) must not fail the boot over a
        # file we cannot write — the heal still applies in memory.
        import contextlib

        pruned = json.dumps(_prune_for_disk(payload, unknown), indent=2) + "\n"
        with contextlib.suppress(OSError):
            path.with_name(path.name + ".unknown-keys.bak").write_text(raw, encoding="utf-8")
            path.write_text(pruned, encoding="utf-8")

    mp = _resolve_master_password(
        master_password, path, encryption_enabled=config.credentials.encrypted
    )
    if mp is not None or config.credentials.encrypted:
        from .encryption import decrypt_config_from_storage

        config = decrypt_config_from_storage(config, mp)
    return config


def save_config(
    config: GatewayConfig, path: str | Path, *, master_password: str | None = None
) -> None:
    """Serialize ``config`` to ``path`` as pretty-printed JSON.

    When encryption at rest is enabled (``credentials.encrypted`` — the default)
    the secrets are encrypted into ``credentials.entries`` and replaced on disk
    by a non-secret placeholder. The master password comes from the caller, the
    environment, the OS keyring, or a 0600 sidecar — an appliance that wants
    encryption at rest gets a key on first boot rather than writing secrets in
    cleartext (review P1-1). Cleartext is an explicit opt-out:
    ``MERCURE_GATEWAY_ALLOW_PLAINTEXT_SECRETS=1``.
    """
    mp = _resolve_master_password(
        master_password, path, encryption_enabled=config.credentials.encrypted
    )
    to_write = config
    if mp is not None and config.credentials.encrypted:
        from .encryption import encrypt_config_for_storage

        to_write = encrypt_config_for_storage(config, mp)
    Path(path).write_text(
        to_write.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )


# ── USB variant: removable-volume detection + defaults profile (S10-T5) ─
#
# The USB dongle gateway runs its spool from a removable drive.  Spec §5.3/
# §6 prescribes auto-detection (spool on removable media → usb_mode profile:
# aggressive retention, disk-full purge at 90%, spool budgeted to the data
# partition).  Linux detection is stdlib-only: /proc/mounts → block device →
# the kernel's ``removable`` flag under /sys/class/block.  Windows uses the
# Win32 GetDriveTypeW API.

_REMOVABLE_BASE_RE = re.compile(
    r"^(sd[a-z]+|vd[a-z]+|xvd[a-z]+|mmcblk\d+|sr\d+|nvme\d+n\d+)(?:p?\d+)?$"
)


def _block_base(device: str) -> str | None:
    """Map a partition device (``/dev/sdb1``) to its removable-media base
    (``/dev/sdb``).  Returns ``None`` for non-block devices (mapper, tmpfs)."""
    if not device.startswith("/dev/"):
        return None
    name = device[len("/dev/") :]
    match = _REMOVABLE_BASE_RE.match(name)
    return match.group(1) if match else None


def _read_removable(device: str) -> bool:
    """True when the kernel marks the block device as removable."""
    base = _block_base(device)
    if base is None:
        return False
    flag = Path("/sys/class/block") / base / "removable"
    try:
        return flag.read_text(encoding="ascii").strip() == "1"
    except OSError:
        return False


def _linux_mounts() -> list[tuple[str, str]]:
    """Parse ``/proc/mounts`` into ``[(mountpoint, device)]`` pairs.

    Pseudo / memory filesystems (proc, tmpfs, cgroup, ...) are skipped — their
    backing "devices" never map to removable media.
    """
    pseudo = {
        "proc",
        "sysfs",
        "tmpfs",
        "devtmpfs",
        "devpts",
        "cgroup",
        "cgroup2",
        "overlay",
    }
    try:
        lines = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    mounts: list[tuple[str, str]] = []
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        device, mountpoint, fstype = fields[0], fields[1], fields[2]
        if fstype in pseudo:
            continue
        # /proc/mounts escapes spaces/tabs/newlines as \040, \011, \012.
        device = device.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n")
        mountpoint = mountpoint.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n")
        mounts.append((mountpoint, device))
    return mounts


def _mount_device_for(mounts: list[tuple[str, str]], raw_path: str) -> str | None:
    """Device backing the *deepest* mount containing ``raw_path``."""
    best_device: str | None = None
    best_len = -1
    for mountpoint, device in mounts:
        if (raw_path == mountpoint or raw_path.startswith(mountpoint + "/")) and len(
            mountpoint
        ) > best_len:
            best_device, best_len = device, len(mountpoint)
    return best_device


def _linux_removable(path: Path) -> bool:
    """Resolve *path* to a block device and test its removable flag.

    The supplied directory may be covered by a mount even before it exists on
    disk (the spool dir is created only after detection).  Falling back to the
    nearest existing ancestor keeps the probe working for not-yet-created
    paths whose parents are mounts (e.g. ``/media/user/USB/spool``).
    """
    probe = path.resolve()
    while True:
        device = _mount_device_for(_linux_mounts(), str(probe).rstrip("/") or "/")
        if device is not None:
            return _read_removable(device)
        if probe.exists() or probe == probe.parent:
            return False
        probe = probe.parent


def _windows_removable(path: Path) -> bool:
    """Win32 ``GetDriveTypeW`` == ``DRIVE_REMOVABLE`` on the path's drive."""
    import ctypes

    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return False
    drive = os.path.splitdrive(str(path))[0]
    if not drive:
        return False
    try:
        return int(windll.kernel32.GetDriveTypeW(f"{drive}\\")) == 2  # DRIVE_REMOVABLE
    except Exception:
        return False


def is_removable_volume(path: str | Path) -> bool:
    """True when ``path`` lives on removable (USB) media (S10-T5).

    - Linux: ``/proc/mounts`` maps the path to its block device, whose kernel
      ``removable`` flag (``/sys/class/block/<dev>/removable``) decides.  When
      the spool directory does not exist yet, the nearest existing ancestor is
      probed instead.
    - Windows: Win32 ``GetDriveTypeW == DRIVE_REMOVABLE``.
    - Anything else: ``False`` — the USB variant targets Linux + Windows only.
    """
    path = Path(path)
    if sys.platform == "win32":
        return _windows_removable(path)
    if sys.platform.startswith("linux"):
        return _linux_removable(path)
    return False


def detect_usb_mode(spool_dir: str | Path) -> bool:
    """Auto-detect the USB variant: True when the spool directory is on
    removable media (usb-dongle-gateway-spec §5.3)."""
    return is_removable_volume(spool_dir)


def apply_usb_defaults(config: GatewayConfig) -> GatewayConfig:
    """Apply the USB-specific storage profile when ``config.usb_mode.enabled``.

    usb-dongle-gateway-spec §5.3/§6 (S10-T5/T7): the USB variant adopts
    aggressive, self-managing storage — purge oldest delivered studies when
    capacity crosses the 90 % warning threshold, and budget the spool to the
    USB data partition (``usb_mode.storage_budget_gb``).  Returns the
    (mutated) config; a no-op for non-USB configurations.
    """
    if not config.usb_mode.enabled:
        return config
    config.storage.purge_on_disk_full = True
    config.storage.disk_full_warning_pct = 90
    config.storage.max_spool_gb = config.usb_mode.storage_budget_gb
    return config


_ENV_PREFIX = "MERCURE_GATEWAY_"


def apply_env_overrides(config: GatewayConfig, environ: Any | None = None) -> GatewayConfig:
    """Apply ``MERCURE_GATEWAY_*`` environment overrides onto ``config`` in place.

    12-factor value injection: env vars win over the config file so secrets
    (e.g. the hub ``api_key``) never have to be written to disk and deployment
    can retarget a host/port without editing JSON.  Keys mirror the model path:

    - ``MERCURE_GATEWAY_WEB_UI_PORT=9090``
    - ``MERCURE_GATEWAY_RECEIVER_AE_TITLE=DEPLOY``
    - ``MERCURE_GATEWAY_AUDIT_HUB_REPORTING_API_KEY=...``

    Values are coerced to the field type (JSON-decoded first, then passed
    raw), so booleans/int are placed as ``true``/``8080``.  Collection fields
    (``destinations[]``, ``forwarding_rules[]``) are deliberately skipped — they
    stay in the config file.  An unset/absent variable leaves the field as-is.
    """
    environ = os.environ if environ is None else environ

    def _coerce(annotation: Any, raw: str) -> Any:
        adapter = TypeAdapter(annotation)
        try:
            return adapter.validate_python(json.loads(raw))
        except (json.JSONDecodeError, ValidationError):
            return adapter.validate_python(raw)

    def _walk(model: BaseModel, prefix: str) -> None:
        for name, field in type(model).model_fields.items():
            annotation: Any = field.annotation
            key = f"{prefix}{name.upper()}"
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                _walk(getattr(model, name), f"{key}_")
                continue
            if get_origin(annotation) in (list, tuple):
                continue
            if key not in environ:
                continue
            raw = environ[key]
            try:
                setattr(model, name, _coerce(annotation, raw))
            except ValidationError as exc:
                raise ValueError(
                    f"environment variable {key}={raw!r} cannot be interpreted as {annotation}"
                ) from exc

    _walk(config, _ENV_PREFIX)
    return config
