// Typed client for the mercure-gateway REST API (refinement §7).
// All endpoints are relative to the SPA origin — FastAPI serves the SPA and
// the API from the same localhost:8080 origin (ADR-0002 Method 1).

export interface StudySummary {
  id: number;
  study_uid: string;
  accession: string | null;
  modality: string | null;
  patient_name: string | null;
  state: string;
  created_at: string;
  num_destinations: number;
}

export interface StudyPage {
  total: number;
  page: number;
  page_size: number;
  items: StudySummary[];
}

export interface QueueStats {
  total: number;
  queued: number;
  sending: number;
  sent: number;
  error: number;
  failed: number;
}

export interface SystemStatus {
  receiver: string;
  forwarder: string;
  report_retriever: string;
  uptime_sec: number;
  version: string;
  hub_registered: boolean | null;
  hub_streaming: boolean | null;
}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
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

export async function postJson(url: string): Promise<{ status: string } | null> {
  const res = await fetch(url, { method: "POST" });
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
  const res = await fetch(
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

export interface LogsResponse {
  lines: string[];
  total_available: number;
  limit: number;
}

export function fetchLogs(limit = 200): Promise<LogsResponse> {
  return getJson<LogsResponse>(`/api/logs?limit=${limit}`);
}

export interface AuditEvent {
  id: number;
  ts: string;
  event: string;
  detail: string | null;
  user: string | null;
  hash: string | null;
}

export function fetchAudit(limit = 200): Promise<AuditEvent[]> {
  return getJson<AuditEvent[]>(`/api/audit?limit=${limit}`);
}

export interface AuditVerifyResult {
  valid: boolean;
  errors: Array<{ event_id: number; reason: string }>;
}

export function verifyAudit(): Promise<AuditVerifyResult> {
  return getJson<AuditVerifyResult>("/api/audit/verify");
}

export interface ReportRow {
  id: number;
  study_uid: string;
  accession: string | null;
  report_type: string;
  status: string;
  file_path: string | null;
  created_at: string | null;
  retrieved_at: string | null;
  sop_class_uid: string | null;
}

export function fetchReports(limit = 200): Promise<ReportRow[]> {
  return getJson<ReportRow[]>(`/api/reports?limit=${limit}`);
}

export interface ReportContent {
  report_id: number;
  report_type: string;
  status: string;
  file_path: string | null;
  content: string | null;
  mime: string | null;
}

export function fetchReportContent(reportId: number): Promise<ReportContent> {
  return getJson<ReportContent>(`/api/reports/${reportId}/content`);
}

export function fetchConfig(): Promise<Record<string, unknown>> {
  return getJson<Record<string, unknown>>("/api/config");
}

export async function saveConfig(payload: Record<string, unknown>): Promise<boolean> {
  const res = await fetch("/api/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return res.ok;
}

// ── Pipeline flow view ───────────────────────────────────────────────

export interface DestinationHealth {
  status: string;
  checked_at: string;
  latency_ms: number;
}

export interface DestinationNode {
  name: string;
  type: string;
  routes: { complete: number; sending: number; waiting: number; error: number };
  last_activity: string | null;
  host?: string;
  port?: number;
  aet?: string;
  health?: DestinationHealth | null;
}

export interface PipelineSnapshot {
  components: { receiver: boolean; forwarder: boolean; reports: boolean };
  queue: QueueStats;
  receiver_counts: { received_last_hour: number };
  destinations: DestinationNode[];
  generated_at: string;
}

export function fetchPipeline(): Promise<PipelineSnapshot> {
  return getJson<PipelineSnapshot>("/api/pipeline");
}

export interface DestinationRouteRow {
  route_id: number;
  study_id: number;
  target_type: string;
  status: string;
  attempts: number;
  last_error: string | null;
  updated_at: string;
  study_uid: string;
  accession: string | null;
  patient_name: string | null;
  modality: string | null;
}

export function fetchDestinationStudies(name: string): Promise<DestinationRouteRow[]> {
  return getJson<DestinationRouteRow[]>(`/api/destinations/${encodeURIComponent(name)}/studies`);
}

export interface StudyRouteDetail {
  id: number;
  target_name: string;
  target_type: string;
  status: string;
  attempts: number;
  last_error: string | null;
  updated_at: string;
  next_retry_sec: number | null;
}

export interface StudyDetail {
  id: number;
  study_uid: string;
  accession: string | null;
  modality: string | null;
  patient_name: string | null;
  state: string;
  created_at: string;
  routes: StudyRouteDetail[];
}

export function fetchStudyDetail(studyId: number): Promise<StudyDetail> {
  return getJson<StudyDetail>(`/api/studies/${studyId}/detail`);
}

export interface TimelineEvent {
  id: number;
  ts: string;
  event: string;
  detail: Record<string, unknown>;
  user: string | null;
}

export function fetchStudyTimeline(studyId: number): Promise<TimelineEvent[]> {
  return getJson<TimelineEvent[]>(`/api/studies/${studyId}/timeline`);
}

// ── Navigation helper ────────────────────────────────────────────────

export function navigate(page: string): void {
  window.location.hash = `#/${page}`;
}
