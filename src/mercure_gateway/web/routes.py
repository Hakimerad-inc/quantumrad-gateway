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
import logging
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.responses import Response

from mercure_gateway import __version__
from mercure_gateway.audit import AuditLog
from mercure_gateway.config import ForwardingRule, GatewayConfig
from mercure_gateway.config.lint import lint_config
from mercure_gateway.redact import (
    CREDENTIAL_ENTRY_FIELDS,
    DESTINATION_SECRET_FIELDS,
    RedactedGatewayConfig,
    redact_config,
)
from mercure_gateway.spool import Spool
from mercure_gateway.web.auth import require_auth

logger = logging.getLogger(__name__)

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
    so the SPA login flow works uniformly. Either way the attempt is counted
    by the rate limiter when auth is enabled — an open panel has nothing to
    brute-force, a locked one must not be brute-forceable either.
    """
    from mercure_gateway.web.auth import login as _login
    from mercure_gateway.web.ratelimit import enforce_rate_limit, record_result

    tracker = enforce_rate_limit(request)

    cfg: GatewayConfig = request.app.state.config
    if cfg.web_ui.auth_enabled:
        from mercure_gateway.web.auth import verify_password

        stored = cfg.web_ui.auth_password_hash
        ok = bool(payload.password) and bool(stored) and verify_password(
            payload.password, stored
        )
        if not ok:
            record_result(tracker, request, success=False)
            raise HTTPException(status_code=401, detail="invalid credentials")

    _login(request, response, payload.password or None)
    record_result(tracker, request, success=cfg.web_ui.auth_enabled)
    return {"status": "ok"}


@auth_router.post("/logout")
def logout(request: Request, response: JSONResponse) -> dict[str, str]:
    """Clear the admin session cookie."""
    from mercure_gateway.web.auth import logout as _logout

    _logout(response, secure=request.url.scheme == "https")
    return {"status": "ok"}


class PasswordChangeRequest(BaseModel):
    """Rotate the admin password.

    ``current_password`` is required whenever auth is already enabled: without
    it, a hijacked or stale session could silently rotate the credential and
    extend its own access (review P0-8).
    """

    current_password: str | None = None
    new_password: str = Field(min_length=8)


@auth_router.post("/web-ui/password", dependencies=[Depends(require_auth)])
def change_password(request: Request, payload: PasswordChangeRequest) -> dict[str, str]:
    """Set the admin password hash.

    This is the only API surface that creates a hash, and it never round-trips
    one: the new password arrives in plaintext over the (loopback or TLS)
    panel, is hashed server-side, and only the hash is stored. The session that
    made the change stays valid — the signing secret is not derived from the
    hash.

    ``require_auth`` is a no-op while auth is disabled, which is exactly the
    first-boot setup-wizard case; on a running appliance auth-off implies a
    loopback bind (the composition root refuses anything else), so the endpoint
    is no more exposed than the rest of the open panel. It deliberately does
    **not** enable auth on the operator's behalf — silently turning auth on
    would be a lockout in the hands of anyone who can reach the panel.
    """
    from mercure_gateway.web.auth import hash_password, verify_password
    from mercure_gateway.web.ratelimit import enforce_rate_limit, record_result

    cfg = _config(request)
    ui = cfg.web_ui

    # Rate-limited exactly like login: this endpoint verifies a credential.
    tracker = enforce_rate_limit(request)

    if ui.auth_enabled:
        stored = ui.auth_password_hash
        if not stored or not payload.current_password or not verify_password(
            payload.current_password, stored
        ):
            record_result(tracker, request, success=False)
            raise HTTPException(
                status_code=401, detail="the current password is incorrect"
            )

    record_result(tracker, request, success=ui.auth_enabled)

    # Mutate a copy so a validation failure cannot leave a half-changed model.
    updated = cfg.model_copy(deep=True)
    updated.web_ui.auth_password_hash = hash_password(payload.new_password)

    from mercure_gateway.config import insecure_bind_reason

    reason = insecure_bind_reason(updated)
    if reason is not None:
        _audit_config_rejection(request, reason)
        raise HTTPException(status_code=409, detail=reason)

    config_path: object = getattr(request.app.state, "config_path", None)
    if config_path:
        from mercure_gateway.config import save_config

        save_config(updated, str(config_path))
    request.app.state.config = updated

    from mercure_gateway.audit import AuditLog

    try:
        client_host = request.client.host if request.client else "?"
        AuditLog(_spool(request).database).append(
            "WEB_UI_PASSWORD_CHANGED", detail={"host": client_host}
        )
    except Exception:  # noqa: BLE001 — a failed audit row must not undo the change
        logger.warning("could not record the WEB_UI_PASSWORD_CHANGED audit event")
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
    # True when the saved config differs from the one the running components
    # were built with — i.e. a process restart is needed for it to take
    # effect. Server-side so it survives a page reload, unlike the client's
    # session flag.
    config_pending_restart: bool = False


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
        config_pending_restart=_config_pending_restart(request),
    )


def _config_pending_restart(request: Request) -> bool:
    """Does the saved config differ from the one the running components use?

    ``app.state.config`` is refreshed on every save, but the receiver/forwarder
    hold construction-time refs, so only a process restart applies it. The
    startup snapshot (``create_app``) is the baseline; a differing current
    config means the panel's "restart required" banner is *true*, not merely
    "was set during this browser session" — the client-side flag resets on
    reload and would otherwise let an operator believe a saved change is live.
    """
    startup: object = getattr(request.app.state, "startup_config_json", None)
    if not isinstance(startup, str):
        return False
    return _config(request).model_dump_json() != startup


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


@router.get("/system/metrics")
def system_metrics(request: Request) -> Response:
    """Prometheus text-exposition scrape target (D1 — headless monitoring).

    A single flat gauge set composed from state the dashboard endpoints already
    compute (health, disk, queue/stats, status) — no new data access. PHI-free
    by construction: numbers and fixed labels only, no paths, identifiers, or
    study metadata (reviewers of the scrape feed may include non-admins).

    When ``web_ui.auth_enabled`` is on the router-level ``require_auth``
    dependency applies here too — the Prometheus job needs the Bearer session
    token (see Monitoring section of the admin guide).

    The audit chain is deliberately NOT verified here: ``AuditLog.verify()``
    replays every row on each call — fine for the operator-triggered
    ``/api/audit/verify``, a scraper-picked DoS vector at 15 s intervals.
    """
    sp = _spool(request)
    cfg = _config(request)

    lines: list[str] = []

    def gauge(name: str, help_text: str, value: str | float | int, *, kind: str = "gauge") -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        lines.append(f"{name} {value}")

    def labeled(
        name: str, help_text: str, samples: list[tuple[str, float | int]], *, kind: str = "gauge"
    ) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            lines.append(f"{name}{{{labels}}} {value}")

    # Process liveness + clock (mirrors /system/health + status uptime).
    gauge("mercure_gateway_up", "Always 1 when the web panel answers.", 1)
    gauge(
        "mercure_gateway_uptime_seconds",
        "Seconds since process start.",
        round(time.time() - _start_time, 2),
    )
    labeled(
        "mercure_gateway_build_info",
        "Gateway version as a label; value is always 1.",
        [(f'version="{__version__}"', 1)],
        kind="info",
    )

    # Component running flags (mirrors /system/status).
    receiver = _get_state(request, "receiver")
    forwarder = _get_state(request, "forwarder")
    retriever = _get_state(request, "report_retriever")
    gauge(
        "mercure_gateway_receiver_running",
        "1 while the DICOM SCP accepts associations.",
        1 if receiver and receiver.is_running else 0,
    )
    gauge(
        "mercure_gateway_forwarder_running",
        "1 while the forwarding workers run.",
        1 if forwarder and forwarder.is_running else 0,
    )
    gauge(
        "mercure_gateway_report_retriever_running",
        "1 while report polling is active.",
        1 if retriever and retriever.is_running else 0,
    )

    # Hub streaming (mirrors /system/status hub fields; absent hub -> 0).
    hub_status: object = getattr(request.app.state, "hub_status", None)
    hub_registered = hub_streaming = 0
    if isinstance(hub_status, dict):
        hub_registered = int(bool(hub_status.get("registered")))
        hub_streaming = int(bool(hub_status.get("streaming")))
    gauge(
        "mercure_gateway_hub_registered",
        "1 when registered with the hub bookkeeper.",
        hub_registered,
    )
    gauge("mercure_gateway_hub_streaming", "1 while audit events stream to the hub.", hub_streaming)

    # Hub delivery health (review P0-10). hub_streaming above mirrors a
    # boot-time status dict whose "streaming" entry is worker-thread
    # liveness — true forever even when the bookkeeper has been unreachable
    # since startup, so a down hub read healthy to the only thing watching an
    # unmanned box. The streamer itself is the live source: queue depth, an
    # in-flight flag driven by the delivery worker, and failure/eviction
    # counters. Emitted as zeros when hub reporting is off so the series
    # always exists — a missing gauge is indistinguishable from a healthy one.
    hub_streamer = getattr(request.app.state, "hub_streamer", None)
    hub_outbox_depth = 0
    hub_delivering = 0
    hub_delivered = 0
    hub_failures = 0
    hub_evicted = 0
    if hub_streamer is not None:
        hub_outbox_depth = hub_streamer.queue_size
        hub_delivering = 1 if hub_streamer.is_delivering else 0
        hub_delivered = hub_streamer.delivered_total
        hub_failures = hub_streamer.delivery_failures_total
        hub_evicted = hub_streamer.events_evicted_total
    gauge(
        "mercure_gateway_hub_outbox_depth",
        "Audit events queued for hub delivery (queued + in flight).",
        hub_outbox_depth,
    )
    gauge(
        "mercure_gateway_hub_delivering",
        "1 while a batch POST to the bookkeeper is in flight — distinguishes "
        "an attempted delivery from an abandoned worker.",
        hub_delivering,
    )
    gauge(
        "mercure_gateway_hub_delivered_total",
        "Audit events successfully delivered to the hub since process start.",
        hub_delivered,
        kind="counter",
    )
    gauge(
        "mercure_gateway_hub_delivery_failures_total",
        "Audit event delivery attempts that failed and were requeued or "
        "evicted. Rising while outbox_depth stays nonzero = the bookkeeper is "
        "not keeping up (or is down).",
        hub_failures,
        kind="counter",
    )
    gauge(
        "mercure_gateway_hub_events_evicted_total",
        "Audit events dropped from the bounded delivery queue.",
        hub_evicted,
        kind="counter",
    )

    # Hub-signed audit anchors, verified on a timer (review P0-10). The
    # verifier holds the *last* pass — this is a cheap field read, never an
    # on-demand verification (see the AuditLog.verify() note above). Absent
    # verifier = unsigned anchoring, where chain integrity is covered by
    # /api/audit/verify; report ok=1 rather than a misleading zero.
    anchor_verifier = getattr(request.app.state, "anchor_verifier", None)
    anchor_ok = 1
    anchor_errors = 0
    if anchor_verifier is not None:
        last = anchor_verifier.last_result
        anchor_ok = 1 if last is None or last.ok else 0
        anchor_errors = anchor_verifier.failures_total
    gauge(
        "mercure_gateway_audit_anchor_ok",
        "1 when the last scheduled hub-signature check of the audit anchors "
        "passed (or anchoring is unsigned). 0 = signatures that do not "
        "verify — investigate audit tampering.",
        anchor_ok,
    )
    gauge(
        "mercure_gateway_audit_anchor_errors_total",
        "Anchor signature lines that failed scheduled verification since "
        "process start.",
        anchor_errors,
        kind="counter",
    )

    # Queue depth by lifecycle state (mirrors /queue/stats via Spool.count_states).
    counts = sp.count_states()
    labeled(
        "mercure_gateway_queue_depth",
        "Studies in each spool lifecycle state.",
        [
            (f'state="{state}"', counts.get(state, 0))
            for state in ("RECEIVED", "QUEUED", "SENDING", "SENT", "ERROR", "FAILED")
        ],
    )

    # Spool filesystem capacity (mirrors /system/disk). The spool dir may not
    # exist yet on a fresh boot (nothing received, dir created lazily) —
    # measure the nearest existing ancestor so the series is *always* present:
    # a scrape that silently omits the disk gauges makes the disk-full alert
    # (the primary unmanned-box failure mode) invisible exactly when it bites.
    storage = cfg.storage
    probe = Path(sp.spool_dir)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        disk = shutil.disk_usage(probe)
    except OSError:
        disk = None
    if disk is not None:
        usage_pct = disk.used * 100.0 / max(1, disk.total)
        gauge(
            "mercure_gateway_disk_usage_percent",
            "Spool filesystem usage percent.",
            round(usage_pct, 2),
        )
        gauge("mercure_gateway_disk_total_bytes", "Spool filesystem total bytes.", disk.total)
        gauge("mercure_gateway_disk_free_bytes", "Spool filesystem free bytes.", disk.free)
        gauge(
            "mercure_gateway_disk_over_threshold",
            "1 once usage >= the configured warning threshold.",
            1 if usage_pct >= storage.disk_full_warning_pct else 0,
        )
    else:
        # Filesystem genuinely unmeasurable (no device at all). Emit zeros
        # rather than dropping the series — a missing metric is indistinguish
        # from "healthy" in Prometheus, and this path means "look here".
        for name in (
            "mercure_gateway_disk_usage_percent",
            "mercure_gateway_disk_total_bytes",
            "mercure_gateway_disk_free_bytes",
        ):
            gauge(name, "Spool filesystem capacity (unmeasurable — 0).", 0)
        gauge(
            "mercure_gateway_disk_over_threshold",
            "1 once usage >= the configured warning threshold.",
            0,
        )

    # Purge loop bounds (review P0-11). The loops are capped so a spool growing
    # faster than it can purge does not pin the DB write lock; these counters
    # are what makes that *visible*, since the cap is otherwise indistinguishable
    # from a healthy monitor. Absent when the web app is built without the
    # monitor (tests, --write-default-config).
    monitor = getattr(request.app.state, "disk_monitor", None)
    if monitor is not None:
        gauge(
            "mercure_gateway_purge_iterations_total",
            "Delivered studies auto-purged since process start.",
            monitor.purge_iterations_total,
        )
        gauge(
            "mercure_gateway_purge_budget_hits_total",
            "Times a single check exhausted its purge iteration budget — the "
            "spool is growing faster than purge can recover.",
            monitor.purge_budget_hits,
        )

    return Response(content="\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


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


@router.post("/studies/{study_id}/enqueue")
def enqueue_study(request: Request, study_id: int) -> dict[str, str]:
    """Route a RECEIVED study that auto-enqueue left stranded.

    ``/retry`` only resets *existing* routes; a study that arrived while no
    destination was enabled, or whose auto-enqueue failed, has none and is
    unreachable from the panel without this endpoint (E1 dry run, 2026-09-16).
    """
    sp = _spool(request)
    try:
        created = sp.enqueue_study(study_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Study {study_id} not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not created:
        return {"status": "already-queued", "study_id": str(study_id)}
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
    return [_row_to_dict(r) for r in _spool(request).list_recent_routes(name, limit=20)]


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


@router.get("/config", response_model=RedactedGatewayConfig)
def get_config(request: Request) -> dict[str, Any]:
    """Get current configuration (all credentials redacted).

    ``response_model`` exports the real schema to the SPA's generated
    TypeScript types, so a merge writing a receiver field into the general
    section (the P0-4 wizard bug) fails at compile time instead of 400ing at
    runtime (review P1-9).
    """
    cfg = _config(request)
    data: dict[str, Any] = json.loads(cfg.model_dump_json())
    return redact_config(data)


class RedactedSecretUnrestorableError(Exception):
    """A '***' sentinel in *payload* names nothing in the stored config.

    Raised by :func:`_restore_redacted_secrets` so the caller can answer 400
    naming the field — the operator must re-enter the credential. Persisting
    the literal ``"***"`` would silently destroy it (review F4 / P1-2).
    """

    def __init__(self, path: str, message: str) -> None:
        super().__init__(message)
        self.path = path
        self.message = message


def _restore_redacted_secrets(payload: dict[str, Any], current: GatewayConfig) -> dict[str, Any]:
    """Replace '***' sentinels in *payload* with the current stored secrets.

    GET /config returns redacted values; the SPA saves the whole body back on
    every edit. Without this restoration the sentinel would be persisted as
    the real credential (destroying it — review F4).

    Destinations are matched to their stored counterpart **by name only**. A
    positional fallback used to cover an in-place rename, but position is not
    identity: any UI sort or filter reorders the saved body, so a renamed
    destination could inherit the *wrong* stored secret — a silent credential
    cross-wire that survives to the next restart (review P1-2). A sentinel that
    names nothing stored is now a hard 400 naming the destination; the operator
    re-enters the credential rather than getting a wrong one for free.
    """
    data: dict[str, Any] = json.loads(json.dumps(payload))  # deep copy
    current_data: dict[str, Any] = json.loads(current.model_dump_json())

    current_dests = {d.get("name"): d for d in current_data.get("destinations", [])}
    for index, destination in enumerate(data.get("destinations", [])):
        name = destination.get("name")
        prev = current_dests.get(name)
        if prev is None:
            # Nothing stored under this name — a rename, an addition, or a
            # foreign file. Any sentinel here cannot be restored; report it
            # instead of persisting the literal or guessing by position.
            unrestorable = [
                key
                for key in DESTINATION_SECRET_FIELDS
                if destination.get(key) == _REDACTED_SENTINEL
            ]
            if unrestorable:
                raise RedactedSecretUnrestorableError(
                    f"destinations[{index}].{unrestorable[0]}",
                    f"Destination {name!r} has a redacted {unrestorable[0]} but no "
                    f"destination named {name!r} exists in the stored config — "
                    "re-enter the credential (a rename cannot carry a secret by "
                    "position, and the literal value would not be accepted).",
                )
            continue
        for key in DESTINATION_SECRET_FIELDS:
            if destination.get(key) == _REDACTED_SENTINEL and prev.get(key):
                destination[key] = prev[key]

    current_entries = current_data.get("credentials", {}).get("entries", {})
    payload_entries = data.get("credentials", {}).get("entries", {})
    for name, entry in payload_entries.items():
        prev = current_entries.get(name)
        if prev is None:
            # Nothing stored under this name — same failure mode as the
            # destination branch above: a sentinel that names nothing stored
            # cannot be restored, and persisting the literal "***" would make
            # the credential unauthenticatable (review P1-2).
            unrestorable = [
                key for key in CREDENTIAL_ENTRY_FIELDS if entry.get(key) == _REDACTED_SENTINEL
            ]
            if unrestorable:
                raise RedactedSecretUnrestorableError(
                    f"credentials.entries.{name}.{unrestorable[0]}",
                    f"Credential entry {name!r} has a redacted {unrestorable[0]} but "
                    f"no entry named {name!r} exists in the stored config — re-enter "
                    "the credential (the literal value would not be accepted).",
                )
            continue
        for key in CREDENTIAL_ENTRY_FIELDS:
            if entry.get(key) == _REDACTED_SENTINEL and prev.get(key):
                entry[key] = prev[key]

    hub = data.get("audit", {}).get("hub_reporting", {})
    prev_hub = current_data.get("audit", {}).get("hub_reporting", {})
    if hub.get("api_key") == _REDACTED_SENTINEL and prev_hub.get("api_key"):
        hub["api_key"] = prev_hub["api_key"]
    if hub.get("anchor_public_key") == _REDACTED_SENTINEL and prev_hub.get("anchor_public_key"):
        hub["anchor_public_key"] = prev_hub["anchor_public_key"]

    web_ui = data.get("web_ui", {})
    prev_ui = current_data.get("web_ui", {})
    if web_ui.get("auth_password_hash") == _REDACTED_SENTINEL and prev_ui.get("auth_password_hash"):
        web_ui["auth_password_hash"] = prev_ui["auth_password_hash"]

    update = data.get("update", {})
    prev_update = current_data.get("update", {})
    if update.get("public_key") == _REDACTED_SENTINEL and prev_update.get("public_key"):
        update["public_key"] = prev_update["public_key"]

    return data


class ConfigUpdateResponse(BaseModel):
    """The result of a successful config write."""

    status: str
    message: str
    restart_required: bool
    warnings: list[ConfigWarningDict] = Field(default_factory=list)


@router.put("/config", response_model=ConfigUpdateResponse)
def update_config(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Update configuration and persist it to ``mercure-gateway.json``.

    The request body is the full config as returned by ``GET /config``; '***'
    redaction sentinels are first restored to the current stored values, then
    the body is validated into a :class:`GatewayConfig` and saved to the
    ``config_path`` configured on app state.  When no path is configured the
    update is validated and applied in memory only.

    A config that would bind an unauthenticated admin panel to a network
    address is rejected with 409 (review P0-3): the document is *valid*, the
    running appliance's posture is what conflicts with it, and refusing to
    persist is the only response that stops an open panel from being saved one
    click at a time. The check runs after validation so the operator sees the
    most specific error first.
    """
    try:
        restored = _restore_redacted_secrets(payload, _config(request))
    except RedactedSecretUnrestorableError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    try:
        updated = GatewayConfig.model_validate(restored)
    except Exception as exc:  # noqa: BLE001 — surface validation as 400
        raise HTTPException(status_code=400, detail=f"Invalid config: {exc}") from exc

    from mercure_gateway.config import insecure_bind_reason

    reason = insecure_bind_reason(updated)
    if reason is not None:
        # The appliance refuses to run like this, so the config is not applied
        # and is not persisted. Emitting the rejection makes the attempt
        # visible in the audit chain rather than only in the access log.
        _audit_config_rejection(request, reason)
        raise HTTPException(status_code=409, detail=reason)

    config_path: object = getattr(request.app.state, "config_path", None)
    if config_path:
        from mercure_gateway.config import save_config

        save_config(updated, str(config_path))
    # Keep the running config (with real secrets) in sync with what was saved.
    request.app.state.config = updated
    # Components hold their own config refs captured at construction, so a
    # restart is required for the new config to take effect (review H5).
    return {
        "status": "ok",
        "message": "Config update saved",
        "restart_required": True,
        # Non-fatal findings against the config just saved, so the panel can
        # tell the operator immediately what the saved state will do (or not
        # do). Lint never blocks a save that validated.
        "warnings": [w.as_dict() for w in lint_config(updated)],
    }


def _audit_config_rejection(request: Request, reason: str) -> None:
    """Record a rejected config write in the audit chain, best-effort.

    The web layer has no AuditLog of its own — the composition root's carries
    the head anchorer and the hub sink — so this writes through the spool's
    database directly, as ``/api/audit/verify`` already does. The event is
    chain-valid; it simply is not re-anchored or streamed from here. A failure
    here must not change the 409 the operator just received, so it is swallowed
    after logging.
    """
    try:
        audit = AuditLog(_spool(request).database)
        audit.append("CONFIG_SECURITY_REJECTED", detail={"reason": reason})
    except Exception:  # noqa: BLE001 — never mask the 409
        logger.warning("could not record the CONFIG_SECURITY_REJECTED audit event")


class ConfigWarningDict(BaseModel):
    """One lint finding as the panel consumes it (config/lint's as_dict)."""

    path: str
    message: str
    severity: str = "warning"


class ConfigWarningsResponse(BaseModel):
    """Lint findings against the running config, plus the version it ran on."""

    warnings: list[ConfigWarningDict] = Field(default_factory=list)
    config_version: str = "1.0"


@router.get("/config/warnings", response_model=ConfigWarningsResponse)
def config_warnings(request: Request) -> dict[str, Any]:
    """Non-fatal misconfiguration findings against the *running* config.

    Where :exc:`HTTPException` 400 rejects a config that cannot load, this
    reports things that load but are likely wrong — a forwarding rule naming a
    removed destination, every destination disabled, duplicate names. The
    loader already logs the stale-rule case; the panel renders it where the
    mistake is made. ``warnings`` is empty when there is nothing to flag.
    """
    return {
        "warnings": [w.as_dict() for w in lint_config(_config(request))],
        "config_version": _config(request).config_version,
    }


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


class ConfigImportResponse(BaseModel):
    """The result of importing a foreign config file."""

    status: str
    message: str
    restart_required: bool
    ignored_keys: list[str] = Field(default_factory=list)


@router.post("/config/import", response_model=ConfigImportResponse)
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

    # Restore redacted secrets from current config (same logic as PUT /config).
    # A foreign file carrying '***' sentinels names nothing stored here, so
    # this 400s naming the field rather than persisting the literal.
    try:
        restored = _restore_redacted_secrets(payload, _config(request))
    except RedactedSecretUnrestorableError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc

    # Validate full config. Unlike PUT /config this is a foreign file — it may
    # come from another appliance build or an editor — so it gets the same
    # healing the boot loader applies: unknown keys are dropped and named in
    # the 400-free path, everything else still rejects (review P0-4).
    from mercure_gateway.config import normalize_and_validate

    try:
        updated = normalize_and_validate(restored, source="import")
    except Exception as exc:  # noqa: BLE001 — surface validation as 400
        raise HTTPException(status_code=400, detail=f"Invalid config: {exc}") from exc
    healed = getattr(updated, "_healed_unknown_keys", None) or []

    # Same posture check as PUT /config (review P0-3): an imported file is
    # every bit as able to bind an unauthenticated admin panel to the LAN, and
    # this is the one config write path that persisted it without a peep —
    # boot would then refuse the very file import just wrote.
    from mercure_gateway.config import insecure_bind_reason

    reason = insecure_bind_reason(updated)
    if reason is not None:
        _audit_config_rejection(request, reason)
        raise HTTPException(status_code=409, detail=reason)

    # Persist to disk if config_path is configured
    config_path: object = getattr(request.app.state, "config_path", None)
    if config_path:
        from mercure_gateway.config import save_config

        save_config(updated, str(config_path))

    # Update in-memory config
    request.app.state.config = updated
    # See update_config: components hold their own config refs, so a restart is
    # required for the new config to take effect (review H5).
    return {
        "status": "ok",
        "message": "Config import saved",
        "restart_required": True,
        # Healed keys are reported rather than silently swallowed: the imported
        # file differs from the appliance's schema and the operator should know
        # which settings did not survive the round trip.
        "ignored_keys": [".".join(str(part) for part in loc) for loc in healed],
    }


# ---------------------------------------------------------------------------
# Forwarding-rule preview  (§5.5 / US-09 — review P0-9)
# ---------------------------------------------------------------------------


class RulePreviewRequest(BaseModel):
    """A synthetic tag set to route, and optionally the rules to route it by.

    ``rules`` omitted means "use the configured rules" — the preview then
    answers what the running appliance would do. Supplied rules are previewed
    instead, so an operator can test an edit in the destinations panel *before*
    saving it: the difference between "these rules route CT to the archive" and
    "these rules would, once I save them" is exactly the question a dry-run
    exists to answer.
    """

    tags: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "DICOM tags and values to route, e.g. "
            '{"Modality": "CT", "StudyDescription": "CHEST"}. Case-insensitive; '
            "values may use the rule's wildcard grammar."
        ),
    )
    rules: list[ForwardingRule] | None = Field(
        default=None,
        description=(
            "Rules to preview. Omit to preview the configured "
            "forwarding_rules."
        ),
    )


class RulePreviewResponse(BaseModel):
    """Where a study carrying the requested tags would be routed."""

    targets: list[str] = Field(
        description=(
            "Destination names that would receive the study. When "
            "matched_any is false this is every enabled destination (the "
            "default route), not a set any rule selected."
        ),
    )
    matched_any: bool = Field(
        description=(
            "True when at least one rule matched and selected targets. False "
            "means the default route applies — no rule spoke to this tag set."
        ),
    )


@router.post("/rules/preview", response_model=RulePreviewResponse)
def preview_rules(request: Request, payload: RulePreviewRequest) -> dict[str, Any]:
    """Preview which destinations would receive a study with the given tags.

    Evaluates the same engine ``Spool.enqueue`` uses, so what an operator
    previews is what the appliance does — before P0-9 the panel previewed one
    engine and production routed by another, and neither discrepancy was
    visible anywhere. The default route here is the *enabled* destinations,
    matching enqueue's ``[t for t in filtered if t.enabled]``: a disabled
    destination never receives a study regardless of what a rule says.

    A malformed rule is a 400 rather than a silent skip. At routing time the
    appliance fails open (a typo'd rule over-delivering beats a study stranded
    in RECEIVED); here the operator asked to check their rules, so the first
    problem is reported by index — the remaining ones are already enumerated by
    ``GET /config/warnings`` at save time.
    """
    from mercure_gateway.rules import RuleSyntaxError
    from mercure_gateway.rules_tester import preview_routing

    cfg = _config(request)
    rules = payload.rules if payload.rules is not None else cfg.forwarding_rules
    # The live path can only see the Modality tag at enqueue time; a preview
    # accepts the full set, so it is strictly more capable rather than a
    # second implementation of the matcher.
    all_targets = [d.name for d in cfg.destinations if d.enabled]
    try:
        preview = preview_routing(rules, payload.tags, all_targets=all_targets)
    except RuleSyntaxError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"targets": preview.targets, "matched_any": preview.matched_any}


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
    # The calling (source) AE title. A PACS that whitelists callers accepts
    # real forwarding but refuses a probe issued under the default title, so
    # the Destinations page sends the destination's own configured value.
    aet_source: str = "GATEWAY"


@router.post("/echo")
def echo_probe(payload: EchoTarget) -> dict[str, Any]:
    """C-ECHO a DICOM target and return its connectivity status.

    Used by the web wizard to validate receiver/destination connectivity per
    step (Flow A-7).  Returns ``status`` of ``ok``/``refused``/``timeout``/
    ``error`` plus the probe target.

    The probe runs in plaintext; TLS targets are the caller's responsibility to
    exclude (see the Destinations page's ``canEcho``).
    """
    from mercure_gateway.config import DICOMDestination
    from mercure_gateway.web.echo import echo_destination

    dest = DICOMDestination(
        name=payload.name or "probe",
        host=payload.host,
        port=payload.port,
        aet_target=payload.aet,
        aet_source=payload.aet_source,
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
