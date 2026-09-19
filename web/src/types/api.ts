// Types derived from the backend's OpenAPI schema (review M8).
//
// The FastAPI app is the single source of truth for API shapes. Regenerate
// `api-schema.ts` after changing the backend with:
//
//     uv run python scripts/export_openapi.py > /tmp/openapi.json
//     cd web && npx openapi-typescript /tmp/openapi.json -o src/types/api-schema.ts
//
// The hand-written interfaces in `api.ts` are now *derived* from that schema
// where a response model exists, so a backend change that alters a payload
// type errors here at compile time instead of surfacing as a runtime bug.
import type { components } from "./api-schema";

type Schemas = components["schemas"];

// ── Response models with server-side schemas ─────────────────────────
// NOTE: openapi-typescript renders Pydantic `X | None` fields with a
// trailing `| undefined` (the field may be absent in JSON). Fields the SPA
// treats as always-present (e.g. hub flags in tests) can use a small
// normalized view here if that ever becomes friction.

export type StudySummary = Schemas["StudySummary"];
export type StudyPage = Schemas["StudyPage"];
export type QueueStats = Schemas["QueueStats"];
export type SystemStatus = Schemas["SystemStatus"];
export type DiskStatus = Schemas["DiskStatus"];

// The config endpoints. GET /api/config returns a redacted view of the real
// GatewayConfig: every field the schema describes is present and typed, so a
// merge writing a receiver field into the general section (the wizard bug,
// review P0-4) is a compile error here rather than a runtime 400. This is the
// type `fetchConfig` used to return as Record<string, unknown>.
export type GatewayConfig = Schemas["RedactedGatewayConfig"];
export type ConfigUpdateResponse = Schemas["ConfigUpdateResponse"];
export type ConfigImportResponse = Schemas["ConfigImportResponse"];

// ── Endpoints returning ad-hoc dicts (no response model server-side) ──

export interface LogsResponse {
  lines: string[];
  total_available: number;
  limit: number;
}

export interface AuditEvent {
  id: number;
  ts: string;
  event: string;
  detail: string | null;
  user: string | null;
  hash: string | null;
}

export interface AuditVerifyResult {
  valid: boolean;
  errors: Array<{ event_id: number; reason: string }>;
}

export interface ReportRow {
  id: number;
  study_uid: string;
  accession: string | null;
  report_type: string;
  status: string;
  file_path: string | null;
  // The reports table has no created_at column and /api/reports returns the
  // rows unmodified, so this key is always absent at runtime. Optional so the
  // type says what the API actually delivers rather than what a consumer
  // might wish were there.
  created_at?: string | null;
  retrieved_at: string | null;
  sop_class_uid: string | null;
}

export interface ReportContent {
  report_id: number;
  report_type: string;
  status: string;
  file_path: string | null;
  content: string | null;
  mime: string | null;
}

// Save/import results now have server-side response models (review P1-9), so
// these are derived from the schema rather than hand-written. The lint warning
// shape is shared by both and by the panel's warnings banner.
export type SaveConfigResult = ConfigUpdateResponse;
export type ConfigWarning = Schemas["ConfigWarningDict"];
export type ConfigWarningsResult = Schemas["ConfigWarningsResponse"];

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

export interface TimelineEvent {
  id: number;
  ts: string;
  event: string;
  detail: Record<string, unknown>;
  user: string | null;
}

// ── Windows service management (S07-T9) ──────────────────────────────

export interface ServiceStatus {
  available: boolean;
  installed: boolean;
  state: string;
}
