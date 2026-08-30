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

import json
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
    counts = _spool(request)._db.count_states()
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
    rows = sp._db.list_studies_with_route_counts(
        state=state, modality=modality, limit=page_size, offset=offset
    )
    total = sp._db.count_studies(state=state, modality=modality)
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
    row = sp._db.get_study(study_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Study {study_id} not found")
    routes = sp._db.get_routes(study_id)
    result = _row_to_dict(row)
    result["routes"] = [_row_to_dict(r) for r in routes]
    return result


@router.get("/studies/{study_id}/routes")
def get_study_routes(request: Request, study_id: int) -> list[dict[str, Any]]:
    """Per-destination routing status for a study."""
    sp = _spool(request)
    row = sp._db.get_study(study_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Study {study_id} not found")
    routes = sp._db.get_routes(study_id)
    return [_row_to_dict(r) for r in routes]


@router.post("/studies/{study_id}/retry")
def retry_study(request: Request, study_id: int) -> dict[str, str]:
    """Re-forward a FAILED study (or any study with incomplete routes)."""
    sp = _spool(request)
    row = sp._db.get_study(study_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Study {study_id} not found")
    requeued = sp.reforward_study(study_id)
    if not requeued:
        raise HTTPException(status_code=400, detail="Study has no incomplete routes to retry")
    return {"status": "queued", "study_id": str(study_id)}


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

    return data


@router.put("/config")
def update_config(request: Request, payload: dict[str, Any]) -> dict[str, str]:
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
    return {"status": "ok", "message": "Config update saved"}


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
def import_config(request: Request) -> dict[str, str]:
    """Import configuration from uploaded JSON (stub — full impl in S06)."""
    return {"status": "ok", "message": "Config import accepted"}


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
    row = sp._db.get_study(study_id)
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
    rows = _spool(request)._db.list_reports(
        status=status, report_type=report_type, limit=limit, offset=offset
    )
    return [_row_to_dict(r) for r in rows]


@router.get("/reports/{report_id}")
def get_report(request: Request, report_id: int) -> dict[str, Any]:
    """Report detail."""
    row = _spool(request)._db.get_report(report_id)
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
    row = _spool(request)._db.get_report(report_id)
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

    row = _spool(request)._db.get_report(report_id)
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
    rows = _spool(request)._db.list_audit_events(event=event, limit=limit, offset=offset)
    return [_row_to_dict(r) for r in rows]


@router.get("/audit/verify")
def verify_audit(request: Request) -> dict[str, Any]:
    """Verify audit chain integrity."""

    sp = _spool(request)
    audit = AuditLog(sp._db)
    ok, errors = audit.verify()
    error_list = [{"event_id": e.event_id, "reason": e.reason} for e in errors]
    result: dict[str, Any] = {"valid": ok, "errors": error_list}
    return result


@router.get("/audit/export")
def export_audit(
    request: Request,
    limit: int = Query(10000, ge=1, le=100000),
) -> JSONResponse:
    """Export audit log as downloadable JSON (includes chain hashes)."""
    rows = _spool(request)._db.list_audit_events(limit=limit)
    events = [_row_to_dict(r) for r in rows]
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
    audit = AuditLog(sp._db)
    phi_scope = getattr(cfg.audit, "phi_scope", "minimal")
    phf_fields = ("patient_name", "mrn", "patient_id")
    events: list[dict[str, Any]] = []
    for row in sp._db.list_audit_events(limit=1000):
        detail = json.loads(row["detail"])
        if phi_scope == "minimal":
            for field in phf_fields:
                detail.pop(field, None)
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
    counts = sp._db.count_states()
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
        AuditLog(sp._db),
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
