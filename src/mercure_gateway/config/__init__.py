"""Pydantic v2 configuration models for ``mercure-gateway.json``.

The schema mirrors the draft configuration in the product spec (PRD §5.5):
``general``, ``receiver``, ``destinations[]`` (per-target-type fields),
``forwarding_rules[]``, ``reports``, ``audit`` and ``storage``.

Deviation note (per quality bar):
- PRD §5.5 lists target types DICOM / DICOM-TLS / DICOMweb / SFTP / rsync / Folder / S3 / XNAT.
  ``dicom_tls`` is modelled as an explicit destination type here (``type="dicom_tls"``)
  so TLS settings stay on the target, matching mercure's target-type conventions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field

__all__ = [
    "AuditConfig",
    "BaseDestination",
    "DICOMDestination",
    "DICOMTLSDestination",
    "DICOMwebDestination",
    "Destination",
    "FolderDestination",
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
    """Local DICOM C-STORE SCP settings."""

    ae_title: str = Field(default="GATEWAY", min_length=1, max_length=16)
    port: int = Field(default=11112, ge=1, le=65535)
    accept_compressed: bool = True
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


class ReportConfig(BaseModel):
    """Report-retrieval configuration."""

    enabled: bool = False
    query_source: ReportQuerySource | None = None
    poll_interval_sec: int = Field(default=300, ge=10)
    on_retrieval: Literal["store", "store_and_forward"] = "store"


class HubReporting(BaseModel):
    """Optional streaming of audit events to a mercure hub bookkeeper (v1.1)."""

    enabled: bool = False
    bookkeeper_url: str = ""
    api_key: str = ""


class AuditConfig(BaseModel):
    """Local tamper-evident audit log configuration."""

    local: bool = True
    encrypt: bool = True
    hub_reporting: HubReporting = Field(default_factory=HubReporting)


class StorageConfig(BaseModel):
    """Spool directory and retention settings."""

    spool_dir: str = "C:\\mercure-gateway\\spool"
    max_spool_gb: int = Field(default=20, ge=1)
    retention_delivered_days: int = Field(default=3, ge=0)


class GatewayConfig(BaseModel):
    """Root model for ``mercure-gateway.json``."""

    general: GeneralConfig = Field(default_factory=GeneralConfig)
    receiver: ReceiverConfig = Field(default_factory=ReceiverConfig)
    destinations: list[Destination] = Field(default_factory=list)
    forwarding_rules: list[ForwardingRule] = Field(default_factory=list)
    reports: ReportConfig = Field(default_factory=ReportConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)


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
