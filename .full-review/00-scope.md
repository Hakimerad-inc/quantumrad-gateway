# Review Scope

## Target

Full-stack comprehensive review of the **dicom-gateway** repository — a DICOM
gateway appliance (Python/FastAPI backend + React/TypeScript admin web panel,
Tauri-wrapped desktop shell). The gateway receives medical imaging over DICOM
DIMSE, applies routing/redaction rules, and forwards to destinations (DIMSE,
DICOMweb, HL7 FHIR). It publishes an admin panel for configuration, queue
monitoring, audit review, and report routing.

Repo path: `/home/dev/Documents/mercurie/dicom-gateway`
Base commit: `aab94b5` (`fix(web): stable card identity, session-expired probe status`)
Current status: uncommitted work-in-progress in `web/` (11 modified files) plus
untracked accessibility scaffolding (`axe.js` assets, `web/src/test/a11y-scan.test.tsx`).
The uncommitted changes are in scope as part of the working tree under review.

## Files

### Backend — Python (src/mercure_gateway, ~11,700 LoC)

- `web/` — FastAPI routes, auth, wizard, console, echo, pipeline endpoints
- `auth.py` — authentication for the admin panel
- `credentials.py`, `keyring_store.py` — secret/credential storage
- `redact.py` — PHI de-identification of DICOM datasets
- `rules.py`, `rules_tester.py` — routing rule engine
- `forwarder/` — DICOM forwarding (association management)
- `reports/` — transport, DICOMweb, HL7 FHIR, render, find/move
- `audit/` — audit events + anchoring (tamper-evidence)
- `recovery.py`, `disk.py`, `spool/` — disk spooling and crash recovery
- `hub_client.py`, `hub_events.py` — upstream hub integration
- `hotplug.py`, `led.py`, `tray.py` — appliance hardware/desktop integration
- `service_backend.py`, `service_controller.py`, `update.py` — service lifecycle
- `sop_classes.py`, `textlog.py`, `main.py`

### Frontend — React 18 / TypeScript (web/src, ~6,900 LoC)

- `pages/` — ConfigView, DestinationsView, QueueView, AuditView, ReportsView,
  PipelineView, LogsView, LoginView, SetupWizard, BackendDownView
- `ui/` — flow.tsx, ErrorBoundary, ServiceCard, UpdaterBanner, icons
- `context/AuthContext.tsx` — client-side auth state
- `config/` — lint.ts (config validation), devApiBase
- `api.ts`, `types/api.ts`, `types/api-schema.ts` — API client + generated schema

### Tests & ops

- `tests/` + colocated `*_test.py` (~90 Python test files)
- `web/src/**/*.test.tsx` (11 frontend test files), `e2e/` (Playwright)
- `docs/` including ADRs; `systemd/`, `src-tauri/`, `justfile`, CI workflows

## Flags

- Security Focus: yes — this is a medical/PHI-handling system (applied by default
  given domain; no `--security-focus` flag was passed, but the reviewer agents
  should treat PHI/DICOM handling as security-critical)
- Performance Critical: no (not flagged; gateway throughput is relevant but not
  the stated focus)
- Strict Mode: no
- Framework: auto-detected — **FastAPI** (Python backend), **React 18 + Vitest +
  Playwright** (frontend), **Tauri** (desktop shell)

## Review Phases

1. Code Quality & Architecture
2. Security & Performance
3. Testing & Documentation
4. Best Practices & Standards
5. Consolidated Report
