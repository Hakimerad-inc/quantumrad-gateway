"""REST API route handlers for the web admin panel (product refinement §7).

Endpoints cover: system, queue/studies, config, reports, and audit.
All routes read from the shared :class:`Spool` and :class:`GatewayConfig` —
no state is owned by the web layer.

The ``app.state`` carries shared references:
- ``config`` — the gateway :class:`GatewayConfig`
- ``spool`` — the gateway :class:`Spool`
- ``receiver`` — the gateway :class:`Receiver` (for start/stop)
- ``forwarder`` — the gateway :class:`Forwarder` (for start/stop)
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from typing import Protocol

from mercure_gateway import __version__
from mercure_gateway.config import GatewayConfig
from mercure_gateway.spool import Spool


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
# System endpoints  (§7.5)
# ---------------------------------------------------------------------------

class SystemStatus(BaseModel):
    receiver: str = "stopped"
    forwarder: str = "stopped"
    report_retriever: str = "stopped"
    uptime_sec: float = 0.0
    version: str = __version__


def _get_state(request: Request, name: str) -> _Runnable | None:
    """Safely extract a runnable component from app state."""
    value: object = getattr(request.app.state, name, None)
    if value is not None and hasattr(value, "is_running"):
        runnable: _Runnable = value  # type: ignore[assignment]
        return runnable
    return None


@router.get("/system/status", response_model=SystemStatus)
async def system_status(request: Request) -> SystemStatus:
    """Gateway status — receiver, forwarder, report retriever."""
    receiver = _get_state(request, "receiver")
    forwarder = _get_state(request, "forwarder")
    report_retriever = _get_state(request, "report_retriever")
    return SystemStatus(
        receiver="running" if receiver and receiver.is_running else "stopped",
        forwarder="running" if forwarder and forwarder.is_running else "stopped",
        report_retriever="running" if report_retriever and report_retriever.is_running else "stopped",
        uptime_sec=round(time.time() - _start_time, 2),
    )


@router.get("/system/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok", "version": __version__}


@router.post("/system/start")
async def system_start(request: Request) -> dict[str, str]:
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
async def system_stop(request: Request) -> dict[str, str]:
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
async def queue_stats(request: Request) -> QueueStats:
    """Queue statistics by state."""
    sp = _spool(request)
    rows = sp._db.list_studies()
    counts: dict[str, int] = {}
    for r in rows:
        s = r["state"]
        counts[s] = counts.get(s, 0) + 1
    return QueueStats(
        total=len(rows),
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


@router.get("/studies", response_model=list[StudySummary])
async def list_studies(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    state: str | None = None,
    modality: str | None = None,
) -> list[StudySummary]:
    """List studies with pagination and filtering."""
    sp = _spool(request)
    offset = (page - 1) * page_size
    rows = sp._db.list_studies(state=state, limit=page_size, offset=offset)
    results: list[StudySummary] = []
    for r in rows:
        if modality and r["modality"] != modality:
            continue
        routes = sp._db.get_routes(r["id"])
        results.append(
            StudySummary(
                id=r["id"],
                study_uid=r["study_uid"],
                accession=r["accession"],
                modality=r["modality"],
                patient_name=r["patient_name"],
                state=r["state"],
                created_at=r["created_at"],
                num_destinations=len(routes),
            )
        )
    return results


@router.get("/studies/{study_id}")
async def get_study(request: Request, study_id: int) -> dict[str, Any]:
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
async def get_study_routes(request: Request, study_id: int) -> list[dict[str, Any]]:
    """Per-destination routing status for a study."""
    sp = _spool(request)
    row = sp._db.get_study(study_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Study {study_id} not found")
    routes = sp._db.get_routes(study_id)
    return [_row_to_dict(r) for r in routes]


@router.post("/studies/{study_id}/retry")
async def retry_study(request: Request, study_id: int) -> dict[str, str]:
    """Re-forward a FAILED study."""
    sp = _spool(request)
    row = sp._db.get_study(study_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Study {study_id} not found")
    if row["state"] != "FAILED":
        raise HTTPException(status_code=400, detail="Only FAILED studies can be retried")
    routes = sp._db.get_routes(study_id)
    for r in routes:
        if r["status"] == "error":
            sp._db.reset_route_waiting(r["id"])
    sp._db.set_study_state(study_id, "QUEUED")
    return {"status": "queued", "study_id": str(study_id)}


# ---------------------------------------------------------------------------
# Config endpoints  (§7.1)
# ---------------------------------------------------------------------------

def _redact_config(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *data* with sensitive fields replaced by '***'."""
    redacted: dict[str, Any] = json.loads(json.dumps(data))  # deep copy
    if "credentials" in redacted:
        entries = redacted["credentials"].get("entries", {})
        for entry in entries.values():
            for key in ("password_encrypted", "private_key_encrypted",
                        "passphrase_encrypted", "api_key_encrypted"):
                if entry.get(key):
                    entry[key] = "***"
    if "audit" in redacted and "hub_reporting" in redacted["audit"]:
        br = redacted["audit"]["hub_reporting"]
        if br.get("api_key"):
            br["api_key"] = "***"
    return redacted


@router.get("/config")
async def get_config(request: Request) -> dict[str, Any]:
    """Get current configuration (credentials redacted)."""
    cfg = _config(request)
    data: dict[str, Any] = json.loads(cfg.model_dump_json())
    return _redact_config(data)


@router.put("/config")
async def update_config(request: Request) -> dict[str, str]:
    """Update configuration (stub — full persistence in S06)."""
    return {"status": "ok", "message": "Config update accepted"}


@router.get("/config/export")
async def export_config(request: Request) -> JSONResponse:
    """Export configuration as downloadable JSON file."""
    cfg = _config(request)
    data: dict[str, Any] = json.loads(cfg.model_dump_json())
    redacted = _redact_config(data)
    return JSONResponse(
        content=redacted,
        headers={
            "Content-Disposition": 'attachment; filename="mercure-gateway.json"',
        },
    )


@router.post("/config/import")
async def import_config(request: Request) -> dict[str, str]:
    """Import configuration from uploaded JSON (stub — full impl in S06)."""
    return {"status": "ok", "message": "Config import accepted"}


# ---------------------------------------------------------------------------
# Reports endpoints  (§7.3)
# ---------------------------------------------------------------------------

@router.get("/reports")
async def list_reports(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: str | None = None,
    report_type: str | None = None,
) -> list[dict[str, Any]]:
    """List reports with optional filtering."""
    sp = _spool(request)
    sql = "SELECT * FROM reports"
    params: list[Any] = []
    conditions: list[str] = []
    if status is not None:
        conditions.append("status = ?")
        params.append(status)
    if report_type is not None:
        conditions.append("report_type = ?")
        params.append(report_type)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = sp._db._conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


@router.get("/reports/{report_id}")
async def get_report(request: Request, report_id: int) -> dict[str, Any]:
    """Report detail."""
    sp = _spool(request)
    row = sp._db._conn.execute(
        "SELECT * FROM reports WHERE id = ?", (report_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Report {report_id} not found")
    return _row_to_dict(row)


@router.post("/reports/{report_id}/refresh")
async def refresh_report(request: Request, report_id: int) -> dict[str, str]:
    """Trigger on-demand report retrieval."""
    sp = _spool(request)
    row = sp._db._conn.execute(
        "SELECT * FROM reports WHERE id = ?", (report_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Report {report_id} not found")
    return {"status": "pending", "report_id": str(report_id)}


@router.get("/reports/{report_id}/content")
async def get_report_content(request: Request, report_id: int) -> dict[str, Any]:
    """Get report content (SR rendered text or PDF path)."""
    sp = _spool(request)
    row = sp._db._conn.execute(
        "SELECT * FROM reports WHERE id = ?", (report_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Report {report_id} not found")
    return {
        "report_id": report_id,
        "report_type": row["report_type"],
        "status": row["status"],
        "file_path": row["file_path"],
        "content": None,  # populated when report is retrieved
    }


# ---------------------------------------------------------------------------
# Audit endpoints  (§7.4)
# ---------------------------------------------------------------------------

@router.get("/audit")
async def list_audit(
    request: Request,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    event: str | None = None,
) -> list[dict[str, Any]]:
    """List audit events with pagination and optional event filter."""
    sp = _spool(request)
    sql = "SELECT id, ts, event, detail, user, hash FROM audit_events"
    params: list[Any] = []
    if event is not None:
        sql += " WHERE event = ?"
        params.append(event)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = sp._db._conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


@router.get("/audit/verify")
async def verify_audit(request: Request) -> dict[str, Any]:
    """Verify audit chain integrity."""
    from mercure_gateway.audit import AuditLog

    sp = _spool(request)
    audit = AuditLog(sp._db.connection())
    ok, errors = audit.verify()
    error_list = [{"event_id": e.event_id, "reason": e.reason} for e in errors]
    result: dict[str, Any] = {"valid": ok, "errors": error_list}
    return result


@router.get("/audit/export")
async def export_audit(
    request: Request,
    limit: int = Query(10000, ge=1, le=100000),
) -> JSONResponse:
    """Export audit log as downloadable JSON (redacted — no PHI)."""
    sp = _spool(request)
    rows = sp._db._conn.execute(
        "SELECT id, ts, event, detail, user FROM audit_events "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    events = [_row_to_dict(r) for r in rows]
    return JSONResponse(
        content={"events": events, "count": len(events)},
        headers={
            "Content-Disposition": 'attachment; filename="audit-log.json"',
        },
    )
