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

import sys
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field

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
    "USBModeConfig",
    "WebUIConfig",
    "XNATDestination",
    "default_config",
    "load_config",
    "save_config",
]


class GeneralConfig(BaseModel):
    """Top-level gateway identity and runtime settings."""

    appliance_name: str = "Gateway-CLI-01"
    locale: str = "en"
    log_level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")


class ReceiverConfig(BaseModel):
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
    accept_compressed: bool = True
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


class BaseDestination(BaseModel):
    """Common fields shared by every destination target."""

    name: str = Field(min_length=1)
    enabled: bool = True


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


class ForwardingRule(BaseModel):
    """Optional advanced routing rule (v1.1). Evaluated against extracted DICOM tags."""

    rule: str = Field(min_length=1)
    targets: list[str] = Field(min_length=1)
    priority: Literal["normal", "high", "low"] = "normal"


class ReportQuerySource(BaseModel):
    """PACS endpoint used for report retrieval (C-FIND/C-MOVE)."""

    type: Literal["dicom"] = "dicom"
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    aet: str = Field(min_length=1, max_length=16)


_DEFAULT_REPORT_TYPES: list[Literal["sr", "pdf"]] = ["sr", "pdf"]


class ReportConfig(BaseModel):
    """Report-retrieval configuration."""

    enabled: bool = False
    query_source: ReportQuerySource | None = None
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


class HubReporting(BaseModel):
    """Optional streaming of audit events to a mercure hub bookkeeper (v1.1)."""

    enabled: bool = False
    bookkeeper_url: str = ""
    api_key: str = ""


class AuditConfig(BaseModel):
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


class StorageConfig(BaseModel):
    """Spool directory and retention settings."""

    spool_dir: str = Field(
        default_factory=_default_spool_dir,
        description="Root directory for the DICOM spool and database.",
    )
    max_spool_gb: int = Field(default=20, ge=1)
    retention_delivered_days: int = Field(default=3, ge=0)
    disk_full_warning_pct: int = Field(
        default=90, ge=50, le=100,
        description="Capacity percentage at which a disk-full warning is emitted.",
    )
    purge_on_disk_full: bool = Field(
        default=False,
        description=(
            "Automatically purge oldest delivered studies when capacity exceeds "
            "'disk_full_warning_pct'. Undelivered/FAILED studies are never purged."
        ),
    )


class ForwardingConfig(BaseModel):
    """Concurrent forwarding worker settings (product refinement spec §2.2)."""

    concurrency: int = Field(
        default=3, ge=1, le=16,
        description="Maximum number of concurrent forwarding workers.",
    )
    queue_poll_interval_ms: int = Field(
        default=500, ge=100, le=5000,
        description="How often workers poll for new tasks (milliseconds).",
    )


class WebUIConfig(BaseModel):
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
        description="Bcrypt hash of the web UI password. Set via the setup wizard.",
    )


class CredentialEntry(BaseModel):
    """Encrypted credential block for a single destination."""

    type: str = Field(min_length=1, description="Destination type (e.g. \"sftp\", \"s3\").")
    username: str | None = None
    password_encrypted: str | None = Field(
        default=None,
        description="AES-256-GCM encrypted password (\"AES256GCM:...\").",
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


class CredentialsConfig(BaseModel):
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


class USBModeConfig(BaseModel):
    """USB dongle variant settings (usb-dongle-gateway-spec §6)."""

    enabled: bool = Field(
        default=False,
        description=(
            "Enable USB-specific behavior (aggressive retention, storage budget, "
            "hot-unplug monitoring).  Auto-detected when spool is on removable media."
        ),
    )
    storage_budget_gb: int = Field(
        default=18, ge=1,
        description="Maximum data partition usage in GB (shared data partition).",
    )
    retention_delivered_hours: int = Field(
        default=24, ge=1,
        description="Aggressive retention for USB: delivered studies purged after N hours.",
    )
    hot_unplug_safe: bool = Field(
        default=True,
        description="Enable graceful shutdown on USB removal detection.",
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


class GatewayConfig(BaseModel):
    """Root model for ``mercure-gateway.json``."""

    general: GeneralConfig = Field(default_factory=GeneralConfig)
    receiver: ReceiverConfig = Field(default_factory=ReceiverConfig)
    destinations: list[Destination] = Field(default_factory=list)
    forwarding: ForwardingConfig = Field(default_factory=ForwardingConfig)
    forwarding_rules: list[ForwardingRule] = Field(default_factory=list)
    reports: ReportConfig = Field(default_factory=ReportConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    web_ui: WebUIConfig = Field(default_factory=WebUIConfig)
    credentials: CredentialsConfig = Field(default_factory=CredentialsConfig)
    usb_mode: USBModeConfig = Field(default_factory=USBModeConfig)


def default_config() -> GatewayConfig:
    """Return a validated configuration populated with PRD defaults."""
    return GatewayConfig()


def load_config(path: str | Path) -> GatewayConfig:
    """Load and strictly validate ``mercure-gateway.json`` from ``path``."""
    with Path(path).open("r", encoding="utf-8") as fh:
        return GatewayConfig.model_validate_json(fh.read())


def save_config(config: GatewayConfig, path: str | Path) -> None:
    """Serialize ``config`` to ``path`` as pretty-printed JSON."""
    Path(path).write_text(
        config.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
