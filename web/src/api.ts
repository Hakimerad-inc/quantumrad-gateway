// Typed client for the mercure-gateway REST API (refinement §7).
//
// Base URL: when the SPA is served directly by the FastAPI backend on
// 127.0.0.1:8080 (dev, or ADR-0002 Method 1), calls use relative `/api/...`
// URLs resolved against that same origin. Inside the packaged Tauri shell the
// SPA is loaded from a `tauri://localhost` (asset://) origin, so relative URLs
// would resolve to the wrong origin and never reach the backend (review C2).
// There we target the backend explicitly — either via a build-time
// VITE_API_BASE_URL override, or, when running inside Tauri, the fixed
// 127.0.0.1:8080 the Rust sidecar binds (the backend CORS allow-list already
// permits the `tauri://localhost` origin).
//
// Response payload types live in `types/api.ts`, derived from the backend's
// OpenAPI schema (review M8) and re-exported here so existing imports keep
// working: `import { StudySummary } from "../api"` is unchanged.
import type {
  AuditEvent,
  AuditVerifyResult,
  DestinationHealth,
  DestinationNode,
  DestinationRouteRow,
  DiskStatus,
  LogsResponse,
  PipelineSnapshot,
  QueueStats,
  ReportContent,
  ReportRow,
  SaveConfigResult,
  ServiceStatus,
  StudyDetail,
  StudyPage,
  StudyRouteDetail,
  StudySummary,
  SystemStatus,
  TimelineEvent,
} from "./types/api";

export type {
  AuditEvent,
  AuditVerifyResult,
  DestinationHealth,
  DestinationNode,
  DestinationRouteRow,
  DiskStatus,
  LogsResponse,
  PipelineSnapshot,
  QueueStats,
  ReportContent,
  ReportRow,
  SaveConfigResult,
  ServiceStatus,
  StudyDetail,
  StudyPage,
  StudyRouteDetail,
  StudySummary,
  SystemStatus,
  TimelineEvent,
};

// Fixed backend address the packaged Tauri sidecar binds (see src-tauri).
const TAURI_API_BASE = "http://127.0.0.1:8080";

function resolveApiBase(): string {
  const override = import.meta.env.VITE_API_BASE_URL;
  if (typeof override === "string" && override.length > 0) return override;
  if (
    typeof window !== "undefined" &&
    typeof (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ !==
      "undefined"
  ) {
    // The Rust shell injects __MERCURE_PORT__ (init script) so a packaged
    // install can bind a non-default backend port via MERCURE_BACKEND_PORT.
    const injected = (window as { __MERCURE_PORT__?: unknown }).__MERCURE_PORT__;
    if (typeof injected === "number" && injected > 0) {
      return `http://127.0.0.1:${injected}`;
    }
    return TAURI_API_BASE;
  }
  return "";
}

// "" means "same origin" — callers pass relative `/api/...` paths and the
// browser resolves them against wherever the SPA itself was served from.
export const API_BASE = resolveApiBase();

export function apiUrl(path: string): string {
  return API_BASE ? `${API_BASE}${path}` : path;
}

async function apiFetch(url: string, init: RequestInit = {}): Promise<Response> {
  // Credentials are always sent so the session cookie travels with every
  // request — required for the cross-origin (Tauri → 127.0.0.1:8080) case and
  // harmless for same-origin.
  return fetch(apiUrl(url), { ...init, credentials: "include" });
}

async function getJson<T>(url: string): Promise<T> {
  const res = await apiFetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as T;
}

export function fetchStudies(
  page: number,
  pageSize: number,
  state?: string,
  modality?: string,
): Promise<StudyPage> {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  if (state) params.set("state", state);
  if (modality) params.set("modality", modality);
  return getJson<StudyPage>(`/api/studies?${params}`);
}

export function fetchQueueStats(): Promise<QueueStats> {
  return getJson<QueueStats>("/api/queue/stats");
}

export function fetchSystemStatus(): Promise<SystemStatus> {
  return getJson<SystemStatus>("/api/system/status");
}

export function fetchDiskStatus(): Promise<DiskStatus> {
  return getJson<DiskStatus>("/api/system/disk");
}

export async function postJson(url: string): Promise<{ status: string } | null> {
  const res = await apiFetch(url, { method: "POST" });
  if (!res.ok) return null;
  return (await res.json()) as { status: string };
}

export async function retryStudy(studyId: number): Promise<boolean> {
  return (await postJson(`/api/studies/${studyId}/retry`)) !== null;
}

export async function requestReport(
  studyId: number,
  reportType: "sr" | "pdf" | "both",
): Promise<{ report_id: number } | null> {
  const res = await apiFetch(
    `/api/studies/${studyId}/reports?report_type=${reportType}`,
    { method: "POST" },
  );
  if (!res.ok) return null;
  return (await res.json()) as { report_id: number };
}

export async function refreshReport(reportId: number): Promise<{ status: string } | null> {
  return postJson(`/api/reports/${reportId}/refresh`);
}

// ── Admin tabs (S06-T4) ──────────────────────────────────────────────

export function fetchLogs(limit = 200): Promise<LogsResponse> {
  return getJson<LogsResponse>(`/api/logs?limit=${limit}`);
}

export function fetchAudit(limit = 200): Promise<AuditEvent[]> {
  return getJson<AuditEvent[]>(`/api/audit?limit=${limit}`);
}

export function verifyAudit(): Promise<AuditVerifyResult> {
  return getJson<AuditVerifyResult>("/api/audit/verify");
}

export function fetchReports(limit = 200): Promise<ReportRow[]> {
  return getJson<ReportRow[]>(`/api/reports?limit=${limit}`);
}

export function fetchReportContent(reportId: number): Promise<ReportContent> {
  return getJson<ReportContent>(`/api/reports/${reportId}/content`);
}

export function fetchConfig(): Promise<Record<string, unknown>> {
  return getJson<Record<string, unknown>>("/api/config");
}

// Result of a config save. `restart_required` is always true on success today:
// the running Receiver/Forwarder/Spool captured their own config references at
// construction, so a saved change only takes effect after a restart (review H5).

// Global "restart required" signal so any component can render a persistent
// banner once the operator saves config (review H5). Kept in module scope so
// ConfigView (which performs the save) and App (which renders the banner) stay
// decoupled — no React context plumbing required.
let restartRequired = false;
const restartListeners = new Set<(value: boolean) => void>();

export function onRestartRequired(cb: (value: boolean) => void): () => void {
  restartListeners.add(cb);
  return () => {
    restartListeners.delete(cb);
  };
}

export function isRestartRequired(): boolean {
  return restartRequired;
}

export async function saveConfig(
  payload: Record<string, unknown>,
): Promise<SaveConfigResult | null> {
  const res = await apiFetch("/api/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    // Surface the server's 400 detail ("Invalid config: …") instead of a bare
    // "Save failed" — the operator cannot fix what they cannot see (review M8).
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body && typeof body.detail === "string" && body.detail) detail = body.detail;
    } catch {
      // non-JSON error body — keep the status-line detail
    }
    throw new Error(detail);
  }
  const data = (await res.json()) as SaveConfigResult;
  if (data.restart_required) {
    restartRequired = true;
    restartListeners.forEach((cb) => cb(true));
  }
  return data;
}

// ── Pipeline flow view ───────────────────────────────────────────────

export function fetchPipeline(): Promise<PipelineSnapshot> {
  return getJson<PipelineSnapshot>("/api/pipeline");
}

export function fetchDestinationStudies(name: string): Promise<DestinationRouteRow[]> {
  return getJson<DestinationRouteRow[]>(`/api/destinations/${encodeURIComponent(name)}/studies`);
}

export function fetchStudyDetail(studyId: number): Promise<StudyDetail> {
  return getJson<StudyDetail>(`/api/studies/${studyId}/detail`);
}

export function fetchStudyTimeline(studyId: number): Promise<TimelineEvent[]> {
  return getJson<TimelineEvent[]>(`/api/studies/${studyId}/timeline`);
}

// ── Windows service management (S07-T9) ──────────────────────────────

export function fetchServiceStatus(): Promise<ServiceStatus> {
  return getJson<ServiceStatus>("/api/service");
}

export type ServiceAction = "start" | "stop" | "install" | "uninstall";

export async function postServiceAction(action: ServiceAction): Promise<boolean> {
  const res = await apiFetch(`/api/service/${action}`, { method: "POST" });
  return res.ok;
}

// ── Navigation helper ────────────────────────────────────────────────

export function navigate(page: string): void {
  window.location.hash = `#/${page}`;
}
