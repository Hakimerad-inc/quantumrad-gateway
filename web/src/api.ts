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
  ConfigWarning,
  ConfigImportResponse,
  ConfigWarningsResult,
  DestinationHealth,
  DestinationNode,
  DestinationRouteRow,
  DiskStatus,
  GatewayConfig,
  LogsResponse,
  PipelineSnapshot,
  QueueStats,
  ReportContent,
  ReportRow,
  RulePreview,
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
  ConfigWarning,
  ConfigImportResponse,
  ConfigWarningsResult,
  DestinationHealth,
  DestinationNode,
  DestinationRouteRow,
  DiskStatus,
  GatewayConfig,
  LogsResponse,
  PipelineSnapshot,
  QueueStats,
  ReportContent,
  ReportRow,
  RulePreview,
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
  // harmless for same-origin. `init` may carry `signal` so callers can
  // cancel a superseded request (see usePoll, review P1-20).
  return fetch(apiUrl(url), { ...init, credentials: "include" });
}

// Build an Error carrying the server's 400/500 `detail` when there is one, so
// the operator sees "Invalid config: port: Input should be greater than 0"
// instead of a bare "400 Bad Request" (review M8). A non-JSON body keeps the
// status line.
async function errorFromResponse(res: Response): Promise<Error> {
  let detail = `${res.status} ${res.statusText}`;
  try {
    const body = (await res.json()) as { detail?: string };
    if (body && typeof body.detail === "string" && body.detail) detail = body.detail;
  } catch {
    // non-JSON error body — keep the status-line detail
  }
  return new Error(detail);
}

async function getJson<T>(url: string, init: RequestInit = {}): Promise<T> {
  const res = await apiFetch(url, init);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as T;
}

// A fetch cancelled by an AbortController — usePoll aborts when a newer call
// supersedes it, the tab goes hidden, or the component unmounts. Consumers
// must treat this as "no result", not a fetch failure: otherwise a fast
// refresh would report the request it deliberately cancelled as an error
// banner (review P1-20).
export function isAbortError(e: unknown): boolean {
  return e instanceof DOMException && e.name === "AbortError";
}

// `init` carries the AbortSignal useAsync owns, so a page change or unmount
// cancels the request at the network rather than only dropping its result.
export function fetchStudies(
  page: number,
  pageSize: number,
  state?: string,
  modality?: string,
  init?: RequestInit,
): Promise<StudyPage> {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  if (state) params.set("state", state);
  if (modality) params.set("modality", modality);
  return getJson<StudyPage>(`/api/studies?${params}`, init ?? {});
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

export async function postJson(
  url: string,
  init: RequestInit = {},
): Promise<{ status: string } | null> {
  const res = await apiFetch(url, { method: "POST", ...init });
  if (!res.ok) return null;
  return (await res.json()) as { status: string };
}

export async function retryStudy(studyId: number): Promise<boolean> {
  return (await postJson(`/api/studies/${studyId}/retry`)) !== null;
}

// Rescue a RECEIVED study that never got routes (destinations were added
// after receipt, or auto-enqueue failed). /retry only resets *existing*
// routes, so it cannot reach these — see the E1 dry-run writeup.
export async function enqueueStudy(studyId: number): Promise<boolean> {
  return (await postJson(`/api/studies/${studyId}/enqueue`)) !== null;
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

// Preview where a tag set would route under the configured (or supplied)
// forwarding rules — the operator-facing half of the unified rule engine
// (review P0-9). Returns null on a malformed rule; the server reports the
// offending rule index in `detail` and the panel renders it.
export async function previewRouting(
  tags: Record<string, string>,
  rules?: { rule: string; targets: string[]; priority?: "normal" | "high" | "low" }[],
): Promise<RulePreview | null> {
  const res = await apiFetch("/api/rules/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tags, rules: rules ?? null }),
  });
  if (!res.ok) return null;
  return (await res.json()) as RulePreview;
}

// ── Admin tabs (S06-T4) ──────────────────────────────────────────────

export function fetchLogs(limit = 200, init: RequestInit = {}): Promise<LogsResponse> {
  return getJson<LogsResponse>(`/api/logs?limit=${limit}`, init);
}

export function fetchAudit(limit = 200, init?: RequestInit): Promise<AuditEvent[]> {
  return getJson<AuditEvent[]>(`/api/audit?limit=${limit}`, init ?? {});
}

export function verifyAudit(): Promise<AuditVerifyResult> {
  return getJson<AuditVerifyResult>("/api/audit/verify");
}

export function fetchReports(limit = 200, init?: RequestInit): Promise<ReportRow[]> {
  return getJson<ReportRow[]>(`/api/reports?limit=${limit}`, init ?? {});
}

export function fetchReportContent(reportId: number): Promise<ReportContent> {
  return getJson<ReportContent>(`/api/reports/${reportId}/content`);
}

export function fetchConfig(): Promise<GatewayConfig> {
  return getJson<GatewayConfig>("/api/config");
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
  payload: GatewayConfig,
): Promise<SaveConfigResult | null> {
  const res = await apiFetch("/api/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    // Surface the server's 400 detail ("Invalid config: …") instead of a bare
    // "Save failed" — the operator cannot fix what they cannot see (review M8).
    throw await errorFromResponse(res);
  }
  const data = (await res.json()) as SaveConfigResult;
  if (data.restart_required) {
    restartRequired = true;
    restartListeners.forEach((cb) => cb(true));
  }
  return data;
}

// ── Config lint (refinement 2026-09-17) ───────────────────────────────

export function fetchConfigWarnings(): Promise<ConfigWarningsResult> {
  return getJson<ConfigWarningsResult>("/api/config/warnings");
}

// ── Export / import (US-08c) and the support bundle (S09-T5) ──────────

// The export endpoints answer JSON with an attachment Content-Disposition.
// The body is fetched and handed to a synthesized anchor rather than
// window.open: inside the packaged Tauri shell a relative URL would resolve
// against the tauri:// origin and never reach the backend (review C2), and
// this path carries the session cookie the authed case needs.
function filenameFromDisposition(header: string | null): string | null {
  if (!header) return null;
  const match = /filename="?([^";]+)"?/i.exec(header);
  return match ? match[1] : null;
}

async function downloadJson(url: string, fallbackName: string): Promise<void> {
  const res = await apiFetch(url);
  if (!res.ok) throw await errorFromResponse(res);
  const blob = await res.blob();
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download =
    filenameFromDisposition(res.headers.get("Content-Disposition")) ?? fallbackName;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // Revoking on the next macrotask lets the browser process the navigation the
  // click queued before the blob URL is pulled out from under it.
  setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
}

// Download the redacted running config (secrets are '***', so the file is safe
// to hand around but cannot be restored verbatim — import keeps the secrets
// the appliance already holds).
export function exportConfig(): Promise<void> {
  return downloadJson("/api/config/export", "mercure-gateway.json");
}

// Download the audit chain — events with their hashes and the current head —
// for offline anchoring or for archiving before retention prunes it.
export function exportAuditLog(): Promise<void> {
  return downloadJson("/api/audit/export", "audit-log.json");
}

// Download the one-click support bundle: redacted config, PHI-scoped audit
// events, spool summary, version (S09-T5).
export function exportDiagnostics(): Promise<void> {
  return downloadJson("/api/diagnostics/export", "mercure-diagnostics.json");
}

// Apply a previously exported config file. The server restores '***' secrets
// from the running config and reports any keys its schema does not know, so
// the caller surfaces `ignored_keys` rather than letting them vanish.
export async function importConfig(file: File): Promise<ConfigImportResponse> {
  const form = new FormData();
  form.append("file", file);
  const res = await apiFetch("/api/config/import", { method: "POST", body: form });
  if (!res.ok) {
    // Surface the 400 detail ("Invalid config: …") — the operator cannot fix
    // what they cannot see (review M8).
    throw await errorFromResponse(res);
  }
  return (await res.json()) as ConfigImportResponse;
}

// ── Pipeline flow view ───────────────────────────────────────────────

export function fetchPipeline(init: RequestInit = {}): Promise<PipelineSnapshot> {
  return getJson<PipelineSnapshot>("/api/pipeline", init);
}

export function fetchDestinationStudies(
  name: string,
  init: RequestInit = {},
): Promise<DestinationRouteRow[]> {
  return getJson<DestinationRouteRow[]>(`/api/destinations/${encodeURIComponent(name)}/studies`, init);
}

export function fetchStudyDetail(studyId: number, init: RequestInit = {}): Promise<StudyDetail> {
  return getJson<StudyDetail>(`/api/studies/${studyId}/detail`, init);
}

export function fetchStudyTimeline(studyId: number, init: RequestInit = {}): Promise<TimelineEvent[]> {
  return getJson<TimelineEvent[]>(`/api/studies/${studyId}/timeline`, init);
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
