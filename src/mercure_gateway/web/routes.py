"""REST API route handlers for the web admin panel (product refinement §7).

Endpoints cover: system, queue/studies, config, reports, and audit.
All routes read from the shared :class:`Spool` and :class:`GatewayConfig` —
no state is owned by the web layer. All data access goes through the
:class:`Spool` / :class:`Database` public API — no raw SQL on private
attributes.

The ``app.state`` carries shared references:
- ``config`` — the gateway :class:`GatewayConfig`
- ``spool`` — the gateway :class:`Spool`
- ``receiver`` — the gateway :class:`Receiver` (for start/stop)
- ``forwarder`` — the gateway :class:`Forwarder` (for start/stop)

Handlers are plain ``def`` (not ``async def``): they perform blocking SQLite
and filesystem work, which FastAPI runs on its threadpool — an async handler
would stall the event loop (freezing every endpoint) on a slow query.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from mercure_gateway import __version__
from mercure_gateway.audit import AuditLog
from mercure_gateway.config import GatewayConfig
from mercure_gateway.redact import (
    CREDENTIAL_ENTRY_FIELDS,
    DESTINATION_SECRET_FIELDS,
    redact_config,
)
from mercure_gateway.spool import Spool

# Sentinel written by :func:`redact_config` — a PUT carrying it means
# "unchanged" and must restore the previously stored secret (review F4).
_REDACTED_SENTINEL = "***"


class _Runnable(Protocol):
    """Minimal interface for receiver/forwarder start/stop."""
    @property
    def is_running(self) -> bool: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...

router = APIRouter()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _spool(request: Request) -> Spool:
    """Return the shared Spool instance from app state."""
    sp: Spool = request.app.state.spool
    return sp


def _config(request: Request) -> GatewayConfig:
    """Return the shared GatewayConfig from app state."""
    cfg: GatewayConfig = request.app.state.config
    return cfg


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a sqlite3.Row to a plain dict."""
    if row is None:
        return {}
    return dict(row)


_start_time = time.time()

# ---------------------------------------------------------------------------
# Auth endpoints  (§7 — session login, review F5)
#
# These live on a SEPARATE router without the require_auth dependency: the
# login route must be reachable before a session exists.
# ---------------------------------------------------------------------------

auth_router = APIRouter()


class LoginRequest(BaseModel):
    password: str


@auth_router.post("/login")
def login(request: Request, response: JSONResponse, payload: LoginRequest) -> dict[str, str]:
    """Authenticate the operator and issue the session cookie.

    With ``web_ui.auth_enabled`` a wrong or missing password returns 401;
    with auth disabled any password is accepted (the API is open regardless)
    so the SPA login flow works uniformly.
    """
    from mercure_gateway.web.auth import login as _login

    _login(request, response, payload.password or None)
    return {"status": "ok"}


@auth_router.post("/logout")
def logout(response: JSONResponse) -> dict[str, str]:
    """Clear the admin session cookie."""
    from mercure_gateway.web.auth import logout as _logout

    _logout(response)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# System endpoints  (§7.5)
# ---------------------------------------------------------------------------

class SystemStatus(BaseModel):
    receiver: str = "stopped"
    forwarder: str = "stopped"
    report_retriever: str = "stopped"
    uptime_sec: float = 0.0
    version: str = __version__
    hub_registered: bool | None = None
    hub_streaming: bool | None = None


def _get_state(request: Request, name: str) -> _Runnable | None:
    """Safely extract a runnable component from app state."""
    value: object = getattr(request.app.state, name, None)
    if value is not None and hasattr(value, "is_running"):
        runnable: _Runnable = value  # type: ignore[assignment]
        return runnable
    return None


@router.get("/system/status", response_model=SystemStatus)
def system_status(request: Request) -> SystemStatus:
    """Gateway status — receiver, forwarder, report retriever, hub."""
    receiver = _get_state(request, "receiver")
    forwarder = _get_state(request, "forwarder")
    report_retriever = _get_state(request, "report_retriever")
    report_retriever_running = report_retriever is not None and report_retriever.is_running
    hub_status: object = getattr(request.app.state, "hub_status", None)
    hub_registered: bool | None = None
    hub_streaming: bool | None = None
    if isinstance(hub_status, dict):
        hub_registered = bool(hub_status.get("registered"))
        hub_streaming = bool(hub_status.get("streaming"))
    return SystemStatus(
        receiver="running" if receiver and receiver.is_running else "stopped",
        forwarder="running" if forwarder and forwarder.is_running else "stopped",
        report_retriever="running" if report_retriever_running else "stopped",
        uptime_sec=round(time.time() - _start_time, 2),
        hub_registered=hub_registered,
        hub_streaming=hub_streaming,
    )


@router.get("/system/health")
def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok", "version": __version__}


class DiskStatus(BaseModel):
    """Live spool filesystem capacity + disk-full management state (S10-T7)."""

    usage_pct: float = 0.0
    total_bytes: int = 0
    used_bytes: int = 0
    free_bytes: int = 0
    warning_pct: int = 90
    over_threshold: bool = False
    purge_on_disk_full: bool = False


@router.get("/system/disk", response_model=DiskStatus)
def system_disk(request: Request) -> DiskStatus:
    """Spool capacity for the disk-full banner / dashboard gauge (§5.3).

    ``over_threshold`` is True once ``usage_pct >= warning_pct`` (90 % by
    default, aggressive for the USB profile).  ``purge_on_disk_full`` mirrors
    the config flag so the SPA can tell operators whether oldest-delivered
    auto-purge is armed.  A measurement failure is a 503 — a "Disk OK" UI
    state must never be shown from an unmeasurable filesystem.
    """
    storage = _config(request).storage
    try:
        disk = shutil.disk_usage(_spool(request).spool_dir)
    except OSError as exc:
        raise HTTPException(status_code=503, detail="spool filesystem not available") from exc
    usage_pct = disk.used * 100.0 / max(1, disk.total)
    return DiskStatus(
        usage_pct=usage_pct,
        total_bytes=disk.total,
        used_bytes=disk.used,
        free_bytes=disk.free,
        warning_pct=storage.disk_full_warning_pct,
        over_threshold=usage_pct >= storage.disk_full_warning_pct,
        purge_on_disk_full=storage.purge_on_disk_full,
    )


@router.post("/system/start")
def system_start(request: Request) -> dict[str, str]:
    """Start receiver + forwarder."""
    receiver = _get_state(request, "receiver")
    forwarder = _get_state(request, "forwarder")
    started: list[str] = []
    if receiver and not receiver.is_running:
        receiver.start()
        started.append("receiver")
    if forwarder and not forwarder.is_running:
        forwarder.start()
        started.append("forwarder")
    return {"status": "started", "components": ",".join(started) or "none needed"}


@router.post("/system/stop")
def system_stop(request: Request) -> dict[str, str]:
    """Graceful shutdown of receiver + forwarder."""
    receiver = _get_state(request, "receiver")
    forwarder = _get_state(request, "forwarder")
    stopped: list[str] = []
    if receiver and receiver.is_running:
        receiver.stop()
        stopped.append("receiver")
    if forwarder and forwarder.is_running:
        forwarder.stop()
        stopped.append("forwarder")
    return {"status": "stopped", "components": ",".join(stopped) or "none running"}


# ---------------------------------------------------------------------------
# Windows service management  (PRD §13 Q3, S07-T9)
# ---------------------------------------------------------------------------

class ServiceStatusModel(BaseModel):
    """Windows service install/run state for the admin panel."""

    available: bool = False
    installed: bool = False
    state: str = "unsupported"


def _service_controller(request: Request) -> object | None:
    """The composition-root ServiceController, or None off Windows."""
    return getattr(request.app.state, "service_controller", None)


@router.get("/service", response_model=ServiceStatusModel)
def service_status(request: Request) -> ServiceStatusModel:
    """Windows service status for the admin panel (S07-T9).

    On non-Windows composition roots (``app.state.service_controller is None``)
    this returns 200 with ``available: false`` so the SPA renders a clean
    "not supported" card instead of an error banner; the POST actions 501.
    """
    controller = _service_controller(request)
    if controller is None:
        return ServiceStatusModel(available=False, installed=False, state="unsupported")
    status = controller.status()  # type: ignore[attr-defined]
    return ServiceStatusModel(
        available=True,
        installed=status.installed,
        state=status.state.value,
    )


@router.post("/service/{action}")
def service_action(action: str, request: Request) -> dict[str, str]:
    """Install/uninstall/start/stop the Windows service (S07-T9).

    Install and uninstall are operator-confirm actions in the SPA; the API is
    admin-auth'd like every other mutating endpoint on this router.
    """
    controller = _service_controller(request)
    if controller is None:
        raise HTTPException(
            status_code=501,
            detail="Windows service management is only available on Windows",
        )
    try:
        if action == "install":
            controller.install()  # type: ignore[attr-defined]
        elif action == "uninstall":
            controller.uninstall()  # type: ignore[attr-defined]
        elif action == "start":
            controller.start()  # type: ignore[attr-defined]
        elif action == "stop":
            controller.stop()  # type: ignore[attr-defined]
        else:
            raise HTTPException(status_code=404, detail=f"unknown service action {action!r}")
    except HTTPException:
        raise
    except RuntimeError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — boundary: SCM errors → 500 with detail
        raise HTTPException(status_code=500, detail=f"service operation failed: {exc}") from exc
    return {"status": action}


# ---------------------------------------------------------------------------
# Queue / Studies endpoints  (§7.2)
# ---------------------------------------------------------------------------

class QueueStats(BaseModel):
    total: int = 0
    queued: int = 0
    sending: int = 0
    sent: int = 0
    error: int = 0
    failed: int = 0


@router.get("/queue/stats", response_model=QueueStats)
def queue_stats(request: Request) -> QueueStats:
    """Queue statistics by state."""
    counts = _spool(request).count_states()
    return QueueStats(
        total=sum(counts.values()),
        queued=counts.get("QUEUED", 0),
        sending=counts.get("SENDING", 0),
        sent=counts.get("SENT", 0),
        error=counts.get("ERROR", 0),
        failed=counts.get("FAILED", 0),
    )


class StudySummary(BaseModel):
    id: int
    study_uid: str
    accession: str | None = None
    modality: str | None = None
    patient_name: str | None = None
    state: str
    created_at: str
    num_destinations: int = 0


class StudyPage(BaseModel):
    """Paginated studies list with metadata for page controls (§7.2)."""

    total: int
    page: int
    page_size: int
    items: list[StudySummary]


@router.get("/studies", response_model=StudyPage)
def list_studies(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    state: str | None = None,
    modality: str | None = None,
) -> StudyPage:
    """List studies with pagination, filtering and page metadata."""
    sp = _spool(request)
    offset = (page - 1) * page_size
    rows = sp.list_studies_with_route_counts(
        state=state, modality=modality, limit=page_size, offset=offset
    )
    total = sp.count_studies(state=state, modality=modality)
    return StudyPage(
        total=total,
        page=page,
        page_size=page_size,
        items=[
            StudySummary(
                id=r["id"],
                study_uid=r["study_uid"],
                accession=r["accession"],
                modality=r["modality"],
                patient_name=r["patient_name"],
                state=r["state"],
                created_at=r["created_at"],
                num_destinations=r["num_destinations"],
            )
            for r in rows
        ],
    )


@router.get("/studies/{study_id}")
def get_study(request: Request, study_id: int) -> dict[str, Any]:
    """Study detail with routes and timestamps."""
    sp = _spool(request)
    row = sp.get_study(study_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Study {study_id} not found")
    routes = sp.get_routes(study_id)
    result = _row_to_dict(row)
    result["routes"] = [_row_to_dict(r) for r in routes]
    return result


@router.get("/studies/{study_id}/routes")
def get_study_routes(request: Request, study_id: int) -> list[dict[str, Any]]:
    """Per-destination routing status for a study."""
    sp = _spool(request)
    row = sp.get_study(study_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Study {study_id} not found")
    routes = sp.get_routes(study_id)
    return [_row_to_dict(r) for r in routes]


@router.post("/studies/{study_id}/retry")
def retry_study(request: Request, study_id: int) -> dict[str, str]:
    """Re-forward a FAILED study (or any study with incomplete routes)."""
    sp = _spool(request)
    row = sp.get_study(study_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Study {study_id} not found")
    requeued = sp.reforward_study(study_id)
    if not requeued:
        raise HTTPException(status_code=400, detail="Study has no incomplete routes to retry")
    return {"status": "queued", "study_id": str(study_id)}


# ---------------------------------------------------------------------------
# Pipeline visualization endpoints  (§7.2 — flow view)
# ---------------------------------------------------------------------------


@router.get("/pipeline")
def pipeline(request: Request) -> dict[str, Any]:
    """One-shot payload for the Pipeline flow view (components + queue +
    per-destination rollups with cached C-ECHO health)."""
    from mercure_gateway.web.pipeline import pipeline_snapshot

    sp = _spool(request)
    receiver = getattr(request.app.state, "receiver", None)
    forwarder = getattr(request.app.state, "forwarder", None)
    retriever = getattr(request.app.state, "report_retriever", None)
    monitor = getattr(request.app.state, "health_monitor", None)
    return pipeline_snapshot(
        request.app.state.config,
        sp,
        monitor.snapshot() if monitor is not None else None,
        receiver_running=bool(receiver and receiver.is_running),
        forwarder_running=bool(forwarder and forwarder.is_running),
        reports_running=bool(retriever and getattr(retriever, "is_running", False)),
    )


@router.get("/destinations")
def list_destinations(request: Request) -> list[dict[str, Any]]:
    """Read-only summary of configured destinations (no credentials)."""
    out: list[dict[str, Any]] = []
    for dest in request.app.state.config.destinations:
        entry: dict[str, Any] = {
            "name": dest.name,
            "type": dest.type,
            "enabled": dest.enabled,
        }
        if dest.type == "dicom":
            entry.update(host=dest.host, port=dest.port, aet=dest.aet_target)
        out.append(entry)
    return out


@router.get("/destinations/{name}/studies")
def destination_studies(request: Request, name: str) -> list[dict[str, Any]]:
    """Latest studies routed to one destination (pipeline drill-down)."""
    return [
        _row_to_dict(r) for r in _spool(request).list_recent_routes(name, limit=20)
    ]


@router.get("/studies/{study_id}/detail")
def study_detail(request: Request, study_id: int) -> dict[str, Any]:
    """Study detail with per-route forwarding state and computed next-retry.

    ``next_retry_sec`` is derived from the forwarder's backoff schedule
    (5 * 2**(attempt-1)) for routes in ``error`` — it is never persisted.
    """
    from mercure_gateway.forwarder import RetryPolicy

    sp = _spool(request)
    row = sp.get_study(study_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Study {study_id} not found")
    policy = RetryPolicy()
    routes = []
    for r in sp.get_routes(study_id):
        entry = _row_to_dict(r)
        entry["next_retry_sec"] = (
            policy.delay(entry["attempts"]) if entry["status"] == "error" else None
        )
        routes.append(entry)
    result = _row_to_dict(row)
    result["routes"] = routes
    return result


@router.get("/studies/{study_id}/timeline")
def study_timeline(request: Request, study_id: int) -> list[dict[str, Any]]:
    """Audit events touching a study, newest first (pipeline timeline)."""
    import json as _json

    sp = _spool(request)
    row = sp.get_study(study_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Study {study_id} not found")
    events = []
    for e in sp.list_audit_for_study(row["study_uid"], limit=50):
        entry = _row_to_dict(e)
        with contextlib.suppress(TypeError, ValueError):
            entry["detail"] = _json.loads(entry["detail"]) if entry["detail"] else {}
        events.append(entry)
    return events


# ---------------------------------------------------------------------------
# Config endpoints  (§7.1)
# ---------------------------------------------------------------------------


@router.get("/config")
def get_config(request: Request) -> dict[str, Any]:
    """Get current configuration (all credentials redacted)."""
    cfg = _config(request)
    data: dict[str, Any] = json.loads(cfg.model_dump_json())
    return redact_config(data)


def _restore_redacted_secrets(payload: dict[str, Any], current: GatewayConfig) -> dict[str, Any]:
    """Replace '***' sentinels in *payload* with the current stored secrets.

    GET /config returns redacted values; the SPA saves the whole body back on
    every edit. Without this restoration the sentinel would be persisted as
    the real credential (destroying it — review F4).
    """
    data: dict[str, Any] = json.loads(json.dumps(payload))  # deep copy
    current_data: dict[str, Any] = json.loads(current.model_dump_json())

    current_dests = {d.get("name"): d for d in current_data.get("destinations", [])}
    for destination in data.get("destinations", []):
        prev = current_dests.get(destination.get("name"))
        if prev is None:
            continue
        for key in DESTINATION_SECRET_FIELDS:
            if destination.get(key) == _REDACTED_SENTINEL and prev.get(key):
                destination[key] = prev[key]

    current_entries = current_data.get("credentials", {}).get("entries", {})
    payload_entries = data.get("credentials", {}).get("entries", {})
    for name, entry in payload_entries.items():
        prev = current_entries.get(name)
        if prev is None:
            continue
        for key in CREDENTIAL_ENTRY_FIELDS:
            if entry.get(key) == _REDACTED_SENTINEL and prev.get(key):
                entry[key] = prev[key]

    hub = data.get("audit", {}).get("hub_reporting", {})
    prev_hub = current_data.get("audit", {}).get("hub_reporting", {})
    if hub.get("api_key") == _REDACTED_SENTINEL and prev_hub.get("api_key"):
        hub["api_key"] = prev_hub["api_key"]

    web_ui = data.get("web_ui", {})
    prev_ui = current_data.get("web_ui", {})
    if web_ui.get("auth_password_hash") == _REDACTED_SENTINEL and prev_ui.get("auth_password_hash"):
        web_ui["auth_password_hash"] = prev_ui["auth_password_hash"]

    update = data.get("update", {})
    prev_update = current_data.get("update", {})
    if update.get("public_key") == _REDACTED_SENTINEL and prev_update.get("public_key"):
        update["public_key"] = prev_update["public_key"]

    return data


@router.put("/config")
def update_config(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Update configuration and persist it to ``mercure-gateway.json``.

    The request body is the full config as returned by ``GET /config``; '***'
    redaction sentinels are first restored to the current stored values, then
    the body is validated into a :class:`GatewayConfig` and saved to the
    ``config_path`` configured on app state.  When no path is configured the
    update is validated and applied in memory only.
    """
    restored = _restore_redacted_secrets(payload, _config(request))
    try:
        updated = GatewayConfig.model_validate(restored)
    except Exception as exc:  # noqa: BLE001 — surface validation as 400
        raise HTTPException(status_code=400, detail=f"Invalid config: {exc}") from exc
    config_path: object = getattr(request.app.state, "config_path", None)
    if config_path:
        from mercure_gateway.config import save_config

        save_config(updated, str(config_path))
    # Keep the running config (with real secrets) in sync with what was saved.
    request.app.state.config = updated
    # Components hold their own config refs captured at construction, so a
    # restart is required for the new config to take effect (review H5).
    return {"status": "ok", "message": "Config update saved", "restart_required": True}


@router.get("/config/export")
def export_config(request: Request) -> JSONResponse:
    """Export configuration as downloadable JSON file (credentials redacted)."""
    cfg = _config(request)
    data: dict[str, Any] = json.loads(cfg.model_dump_json())
    redacted = redact_config(data)
    return JSONResponse(
        content=redacted,
        headers={
            "Content-Disposition": 'attachment; filename="mercure-gateway.json"',
        },
    )


@router.post("/config/import")
async def import_config(request: Request) -> dict[str, Any]:
    """Import configuration from uploaded JSON file.

    Accepts multipart/form-data with a 'file' field containing the JSON config.
    Validates config_version (must be '1.0'), restores redacted secrets from
    current config, validates the full config, and persists to config_path.
    """
    from starlette.datastructures import UploadFile as StarletteUploadFile

    # Get the uploaded file
    form = await request.form()
    file: StarletteUploadFile | str | None = form.get("file")
    if not file or isinstance(file, str) or not hasattr(file, "read"):
        raise HTTPException(status_code=400, detail="No file uploaded")

    # Read and parse JSON
    content = await file.read()
    try:
        payload: dict[str, Any] = json.loads(content.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

    # Validate config_version
    config_version = payload.get("config_version", "1.0")
    if config_version != "1.0":
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported config_version: {config_version}. Expected '1.0'.",
        )

    # Restore redacted secrets from current config (same logic as PUT /config)
    restored = _restore_redacted_secrets(payload, _config(request))

    # Validate full config
    try:
        updated = GatewayConfig.model_validate(restored)
    except Exception as exc:  # noqa: BLE001 — surface validation as 400
        raise HTTPException(status_code=400, detail=f"Invalid config: {exc}") from exc

    # Persist to disk if config_path is configured
    config_path: object = getattr(request.app.state, "config_path", None)
    if config_path:
        from mercure_gateway.config import save_config

        save_config(updated, str(config_path))

    # Update in-memory config
    request.app.state.config = updated
    # See update_config: components hold their own config refs, so a restart is
    # required for the new config to take effect (review H5).
    return {"status": "ok", "message": "Config import saved", "restart_required": True}


# ---------------------------------------------------------------------------
# Reports endpoints  (§7.3)
# ---------------------------------------------------------------------------

def _report_retriever(request: Request) -> Any:
    """Return the shared ReportRetriever from app state (may be None in tests)."""
    value: object = getattr(request.app.state, "report_retriever", None)
    return value


@router.post("/studies/{study_id}/reports")
def request_report(
    request: Request,
    study_id: int,
    report_type: str = Query("sr", pattern="^(sr|pdf|both)$"),
) -> dict[str, Any]:
    """Request a report for a study (on-demand, US-06).

    Creates a PENDING report row via the shared ``ReportRetriever`` and returns
    its id.  ``report_type`` is ``"sr"``, ``"pdf"`` or ``"both"``.
    """
    sp = _spool(request)
    row = sp.get_study(study_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Study {study_id} not found")
    retriever = _report_retriever(request)
    if retriever is None:
        raise HTTPException(status_code=503, detail="Report retriever not configured")
    report_id = retriever.request_report(
        study_uid=str(row["study_uid"]),
        accession=row["accession"],
        report_type=report_type,
    )
    return {"status": "pending", "report_id": report_id}


@router.get("/reports")
def list_reports(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: str | None = None,
    report_type: str | None = None,
) -> list[dict[str, Any]]:
    """List reports with optional filtering."""
    rows = _spool(request).list_reports(
        status=status, report_type=report_type, limit=limit, offset=offset
    )
    return [_row_to_dict(r) for r in rows]


@router.get("/reports/{report_id}")
def get_report(request: Request, report_id: int) -> dict[str, Any]:
    """Report detail."""
    row = _spool(request).get_report(report_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Report {report_id} not found")
    return _row_to_dict(row)


@router.post("/reports/{report_id}/refresh")
def refresh_report(request: Request, report_id: int) -> dict[str, str]:
    """Trigger on-demand report retrieval (US-06).

    Delegates to the shared ``ReportRetriever.retrieve`` so a manual refresh
    actually walks PENDING → RETRIEVING → RETRIEVED/FAILED instead of just
    acknowledging the request.
    """
    row = _spool(request).get_report(report_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Report {report_id} not found")
    retriever = _report_retriever(request)
    if retriever is None:
        raise HTTPException(status_code=503, detail="Report retriever not configured")
    status = retriever.retrieve(report_id)
    return {"status": status, "report_id": str(report_id)}


@router.get("/reports/{report_id}/content")
def get_report_content(request: Request, report_id: int) -> dict[str, Any]:
    """Get report content (SR rendered text or PDF bytes).

    For a RETRIEVED report this loads the stored DICOM file and either renders
    the SR as structured text or extracts the raw PDF bytes (base64-encoded).
    Pending/failed reports return ``content=None``.
    """
    from mercure_gateway.reports.render import RenderError, RenderService

    row = _spool(request).get_report(report_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Report {report_id} not found")
    report_type = str(row["report_type"])
    status = str(row["status"])
    file_path = row["file_path"]
    if status != "retrieved" or not file_path:
        return {
            "report_id": report_id,
            "report_type": report_type,
            "status": status,
            "file_path": file_path,
            "content": None,
            "mime": None,
        }
    try:
        import pydicom

        ds = pydicom.dcmread(str(file_path))
        if report_type == "pdf":
            pdf_bytes = RenderService.extract_pdf(ds)
            import base64

            return {
                "report_id": report_id,
                "report_type": report_type,
                "status": status,
                "file_path": file_path,
                "content": base64.b64encode(pdf_bytes).decode("ascii"),
                "mime": "application/pdf",
            }
        text = RenderService.render_sr(ds)
        return {
            "report_id": report_id,
            "report_type": report_type,
            "status": status,
            "file_path": file_path,
            "content": text,
            "mime": "text/plain",
        }
    except RenderError as exc:
        return {
            "report_id": report_id,
            "report_type": report_type,
            "status": status,
            "file_path": file_path,
            "content": None,
            "mime": None,
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Audit endpoints  (§7.4)
# ---------------------------------------------------------------------------

@router.get("/audit")
def list_audit(
    request: Request,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    event: str | None = None,
) -> list[dict[str, Any]]:
    """List audit events with pagination and optional event filter."""
    rows = _spool(request).list_audit_events(event=event, limit=limit, offset=offset)
    return [_row_to_dict(r) for r in rows]


@router.get("/audit/verify")
def verify_audit(request: Request) -> dict[str, Any]:
    """Verify audit chain integrity."""

    sp = _spool(request)
    audit = AuditLog(sp.database)
    ok, errors = audit.verify()
    error_list = [{"event_id": e.event_id, "reason": e.reason} for e in errors]
    result: dict[str, Any] = {"valid": ok, "errors": error_list}
    return result


@router.get("/audit/export")
def export_audit(
    request: Request,
    limit: int = Query(10000, ge=1, le=100000),
) -> JSONResponse:
    """Export audit log as downloadable JSON (includes chain hashes).

    PHI scoping (§6.4): when ``config.audit.phi_scope`` is ``"minimal"``
    (the default), patient-identifying detail keys are stripped from the
    exported events (review M5).
    """
    sp = _spool(request)
    cfg = _config(request)
    phi_scope = getattr(cfg.audit, "phi_scope", "minimal")
    from mercure_gateway.audit import redact_phi

    rows = sp.list_audit_events(limit=limit)
    events = []
    for r in rows:
        ev = _row_to_dict(r)
        detail = json.loads(ev.get("detail") or "{}")
        ev["detail"] = redact_phi(detail, phi_scope)
        events.append(ev)
    return JSONResponse(
        content={"events": events, "count": len(events)},
        headers={
            "Content-Disposition": 'attachment; filename="audit-log.json"',
        },
    )


# ---------------------------------------------------------------------------
# Connectivity echo  (§7.5 — wizard step validation, Flow A-7)
# ---------------------------------------------------------------------------

class EchoTarget(BaseModel):
    name: str = ""
    host: str = ""
    port: int = 104
    aet: str = "MERCURE"


@router.post("/echo")
def echo_probe(payload: EchoTarget) -> dict[str, Any]:
    """C-ECHO a DICOM target and return its connectivity status.

    Used by the web wizard to validate receiver/destination connectivity per
    step (Flow A-7).  Returns ``status`` of ``ok``/``refused``/``timeout``/
    ``error`` plus the probe target.
    """
    from mercure_gateway.config import DICOMDestination
    from mercure_gateway.web.echo import echo_destination

    dest = DICOMDestination(
        name=payload.name or "probe",
        host=payload.host,
        port=payload.port,
        aet_target=payload.aet,
    )
    status = echo_destination(dest)
    return {"status": status, "target": payload.name or payload.host}


# ---------------------------------------------------------------------------
# Setup wizard  (§7.5 — guided first-run, US-08)
# ---------------------------------------------------------------------------

@router.post("/wizard/validate/{step}")
def wizard_validate(step: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate one wizard step's data (US-08 AC: per-step validation).

    Returns the list of validation errors (empty = the step's gate passes).
    Unknown steps return 400.
    """
    from mercure_gateway.web.wizard import SetupWizard

    try:
        errors = SetupWizard().validate_step(step, payload)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"unknown wizard step {step!r}") from None
    return {"step": step, "errors": errors}


# ---------------------------------------------------------------------------
# Operations log  (§7.6 — admin log viewer)
# ---------------------------------------------------------------------------

@router.get("/logs")
def get_logs(
    request: Request,
    limit: int = Query(100, ge=1, le=5000),
) -> dict[str, Any]:
    """Return the tail of the rotating operations log (S06-T4).

    Reads ``app.state.text_log_path`` (set by the composition root to the
    ``operations.log`` path).  Returns the last ``limit`` lines plus how many
    lines exist in total so the UI can indicate truncation.
    """
    text_log_path: object = getattr(request.app.state, "text_log_path", None)
    lines: list[str] = []
    if isinstance(text_log_path, str) and Path(text_log_path).exists():
        try:
            with Path(text_log_path).open("r", encoding="utf-8") as fh:
                lines = [line.rstrip("\n") for line in fh.readlines()]
        except OSError:
            lines = []
    return {
        "lines": lines[-limit:],
        "total_available": len(lines),
        "limit": limit,
    }


# ---------------------------------------------------------------------------
# Diagnostics bundle export  (§7, S09-T5)
# ---------------------------------------------------------------------------

@router.get("/diagnostics/export")
def diagnostics_export(request: Request) -> JSONResponse:
    """One-click support bundle: redacted config + audit + spool summary.

    Serves a downloadable JSON bundle for support triage (S09-T5).  Secrets
    are redacted via :func:`redact_config` (reuses the S04-T2 path); PHI
    scoping follows the audit ``phi_scope`` the same way the audit export
    does, so the bundle never leaks credentials or patient identifiers.
    """

    sp = _spool(request)
    cfg = _config(request)

    # Redacted configuration (never serializes real secrets).
    config_data = json.loads(cfg.model_dump_json())
    redacted = redact_config(config_data)

    # Structured audit events (PHI-scoped like the audit export).
    audit = AuditLog(sp.database)
    phi_scope = getattr(cfg.audit, "phi_scope", "minimal")
    from mercure_gateway.audit import redact_phi

    events: list[dict[str, Any]] = []
    for row in sp.list_audit_events(limit=1000):
        detail = redact_phi(json.loads(row["detail"]), phi_scope)
        events.append(
            {
                "id": row["id"],
                "ts": row["ts"],
                "event": row["event"],
                "detail": detail,
                "user": row["user"],
                "hash": row["hash"],
            }
        )

    # Spool summary by state.
    counts = sp.count_states()
    spool_summary = {
        "total": sum(counts.values()),
        "states": dict(counts),
    }

    bundle = {
        "config": redacted,
        "audit": {
            "events": events,
            "count": len(events),
            "head_hash": audit.head_hash(),
        },
        "spool": spool_summary,
        "generated_at": datetime.now(UTC).isoformat(),
        "version": __version__,
    }
    return JSONResponse(
        content=bundle,
        headers={
            "Content-Disposition": 'attachment; filename="mercure-gateway-diagnostics.json"',
        },
    )


# ---------------------------------------------------------------------------
# Operator console v0  (§2.2 Flow B — read-only dashboard)
# ---------------------------------------------------------------------------

@router.get("/console/dashboard")
def console_dashboard(request: Request) -> dict[str, Any]:
    """Read-only operator dashboard: queue/status/logs/errors (S04-T6).

    Aggregates queue statistics, recent audit events, error events, the audit
    chain head hash and the tail of the rotating text log (when configured on
    ``app.state.text_log_path``) into one response for the console SPA.
    """
    from mercure_gateway.web.console import ConsoleService

    sp = _spool(request)
    text_log_path: object = getattr(request.app.state, "text_log_path", None)
    service = ConsoleService(
        sp,
        AuditLog(sp.database),
        text_log_path=text_log_path if isinstance(text_log_path, str) else None,
    )
    dash = service.dashboard()
    return {
        "queue": dash.queue,
        "recent_events": dash.recent_events,
        "recent_errors": dash.recent_errors,
        "head_hash": dash.head_hash,
        "text_log_tail": dash.text_log_tail,
    }
