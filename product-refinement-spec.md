# mercure Gateway — Product Refinement Spec

**Status:** Draft
**Date:** 2026-08-29
**Source:** Analysis of `mercure-gateway-PRD.md` + stakeholder interview rounds 1–4

---

## 1. Interview Summary

Four rounds of clarifying questions were conducted to refine the PRD. Below are the decisions and their rationale.

### Round 1 — User & Deployment Model

| Question | Decision | Rationale |
|----------|----------|-----------|
| Primary user persona | **Single person, multiple hats** — one clinic staff member handles setup, monitoring, troubleshooting | Small clinics lack dedicated IT; the app must be self-service end-to-end |
| MVP report format | **DICOM SR + PDF** | Pilot sites use both; SR-only MVP would miss a critical use case |
| Deployment model | **USB/sneakernet** | Mixed air-gapped + networked environments; physical media is the primary delivery vector |
| Run mode | **Tray app (MVP) → Windows service (v1.1)** | Start simple; add service mode for always-on headless operation later |

### Round 2 — Network & Compliance

| Question | Decision | Rationale |
|----------|----------|-----------|
| Network environment | **Mixed** — some sites fully air-gapped, some have restricted internet | Architecture must work without any outbound connectivity; internet features are additive |
| Encryption at rest | **Nice-to-have** (not MVP-blocking) | Not regulatory-mandatory for pilot sites; encrypt when SQLCipher integration is straightforward |
| DICOM compressed syntaxes | **All compressed** — JPEG, JPEG 2000, JPEG-LS, RLE | Modalities produce varied syntaxes; rejecting any would break the receive path |
| MVP routing rules | **Basic include/exclude by modality** | Advanced tag-based rules deferred to v1.1; modality filter covers 80% of pilot needs |

### Round 3 — UI & Operations

| Question | Decision | Rationale |
|----------|----------|-----------|
| Configuration UI | **Web admin panel** (localhost) | Air-gapped sites need config UI that doesn't depend on external resources |
| IT support model | **Remote desktop** (RDP/SSH) | Support team can troubleshoot remotely; no on-site visits for routine issues |
| Report retrieval SLA | **Near-real-time (minutes)** — polling every ~5 min acceptable | Seconds not required; minutes is the clinical norm for non-emergency reports |
| Audit log consumer | **Internal clinic use** — ops troubleshooting, not external compliance | Simplifies MVP scope; compliance export deferred |

### Round 4 — Architecture & Technical

| Question | Decision | Rationale |
|----------|----------|-----------|
| UI architecture | **Tauri with embedded web UI** (replaces PySide6/Tauri tray app decision) | Tauri wraps a localhost web UI; native tray + web admin in one shell |
| Web stack | **FastAPI + React/Vue SPA** | Rich interactions needed for queue management, config, report viewer |
| Tauri ↔ FastAPI communication | **Prototype needed in Phase 0 spike** | Decide between localhost HTTP, Tauri sidecar, or command bridge based on measured performance |
| Credential storage | **Encrypted config file** (master password on startup) | OS keyring may not be available on air-gapped machines; file-based encryption is portable |
| Study volume | **Medium (50–200 studies/day)** | Mostly CT/MR with some X-ray/US; 20GB spool is sufficient |
| DICOM unknown syntax handling | **Accept + selective decompress** | Decompress common syntaxes (JPEG 2000, JPEG-LS); pass-through rare/proprietary ones |
| Audit log MVP | **Full chained SHA-256 hash** (from PRD §5.4) | Tamper-evidence is a core design principle; implement correctly from day one |
| Forwarding concurrency | **Concurrent with configurable limit** | Serial is too slow for multi-destination studies; configurable parallelism balances throughput and resource usage |

---

## 2. Architecture Changes from PRD

> **Implementation status:** ✅ Architecture decisions resolved (ADR-0002); FastAPI backend + SPA implemented; Tauri shell prototype validated.

### 2.1 Desktop Shell → Tauri + Web UI

The PRD (§5.1) lists PySide6 or Tauri as TBD. This spec resolves Q1:

**Decision:** Replace the PySide6/Tauri desktop shell decision with a **Tauri shell wrapping a FastAPI + React/Vue SPA served on localhost**.

**Rationale:**
- Web admin panel requirement (interview round 3) aligns naturally with a browser-based UI
- Tauri provides native tray icon, system notifications, and auto-start without a full Qt dependency
- FastAPI backend serves both the SPA and the REST API for configuration, queue management, and report retrieval
- Air-gapped sites get a fully functional UI with zero external dependencies

**Architecture update:**

```
┌───────────────────────────  Desktop PC (Windows/Linux) ───────────────────────────┐
│                                                                                   │
│   ┌─────────────┐   ┌─────────────────────────────────────────────────────────┐   │
│   │  Modalities  │──▶│  Tauri Shell (native tray + window)                     │   │
│   │  (CT/MR/US)  │   │    └─ Webview → localhost:8080 (React/Vue SPA)          │   │
│   └─────────────┘   │                                                         │   │
│                     │  ┌───────────────────────────────────────────────────┐   │   │
│                     │  │  FastAPI Backend (Python)                         │   │   │
│                     │  │    ├─ REST API (config, queue, reports, audit)     │   │   │
│                     │  │    ├─ Receiver (C-STORE SCP via pynetdicom)       │   │   │
│                     │  │    ├─ Forwarder (multi-dest, concurrent)          │──▶│──▶│ Hub/PACS
│                     │  │    ├─ ReportRetriever (C-FIND/C-MOVE, DICOMweb)   │   │   │
│                     │  │    └─ AuditLog (chained hash)                     │◀──│◀──│ PACS
│                     │  └───────────────────────────────────────────────────┘   │   │
│                     │         ▼                                                │   │
│                     │  Spool (SQLite + DICOM files)                            │   │
│                     └─────────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────────────────────────┘
```

**Tauri ↔ FastAPI communication** — TBD by Phase 0 spike (prototype localhost HTTP, Tauri sidecar, and command bridge; measure latency and RAM).

### 2.2 Concurrent Forwarding

> **Implementation status:** ✅ `ForwardingConfig` model implemented; concurrent worker design documented in sprint plan (S03-T7).

The PRD describes serial forwarding. This spec introduces **concurrent forwarding with a configurable concurrency limit**.

**Configuration addition (PRD §5.5):**

```jsonc
"forwarding": {
  "concurrency": 3,          // max concurrent forwarding workers
  "queue_poll_interval_ms": 500  // how often workers poll for new tasks
}
```

**Behavior:**
- Up to `concurrency` tasks are claimed and dispatched simultaneously
- Each task runs `_dispatch_with_retry` independently
- Study-level SENT transition still requires all destinations complete (existing logic)
- Configurable limit prevents overwhelming slow destinations (e.g., SFTP over WAN)

### 2.3 Report Retrieval: SR + PDF

> **Implementation status:** ✅ `ReportConfig.report_types` field implemented; sprint plan updated (S05-T1/T2).

The PRD MVP specifies DICOM SR only. This spec adds **PDF report support**.

**Report types:**

| Type | Source | Storage | Viewer |
|------|--------|---------|--------|
| DICOM SR | C-FIND/C-MOVE (SOP Class: 1.2.840.10008.5.1.4.1.1.88.33) | `reports/{study_uid}/sr/` | Rendered SR → HTML/text |
| Encapsulated PDF | C-FIND/C-MOVE (SOP Class: 1.2.840.10008.5.1.4.1.1.104.2) | `reports/{study_uid}/pdf/` | PDF viewer (embedded) |

**Configuration update (PRD §5.5):**

```jsonc
"reports": {
  "enabled": false,
  "query_source": { "type": "dicom", "host": "pacs.local", "port": 104, "aet": "PACS" },
  "poll_interval_sec": 300,
  "on_retrieval": "store",
  "report_types": ["sr", "pdf"]   // NEW: which report SOP classes to retrieve
}
```

**C-FIND query update:**
- Query by Accession Number or Study Instance UID
- Filter by `SOP Class UID` in the C-FIND dataset to distinguish SR from PDF
- C-MOVE retrieves the matching objects
- Store to appropriate subdirectory based on type

### 2.4 Config Setup for Air-Gapped Sites

> **Implementation status:** ✅ Web admin panel implemented (FastAPI + SPA); config import/export endpoints added.

The PRD assumes a guided wizard (PRD §2.2 Flow A). For mixed environments:

**MVP approach:**

1. **First run (no config file):** Launch web admin panel on `localhost:8080`
2. **Setup wizard in browser:** Step-by-step configuration (receiver, destinations, reports)
3. **Config import/export:** Admin can export `mercure-gateway.json` to USB for air-gapped deployment
4. **Config file drop:** For air-gapped sites, admin pre-creates config file on USB, drops it next to the executable
5. **Web panel always available:** Even after initial setup, `localhost:8080` provides full config/management UI

**No PySide6 wizard needed** — the web UI replaces the desktop wizard entirely.

### 2.5 Credential Storage for Air-Gapped Sites

> **Implementation status:** ✅ `CredentialsConfig` model with AES-256-GCM encrypted blocks implemented; OS keyring deferred to v1.1 (S07-T8).

**Decision:** Encrypted config file with master password on startup.

**Implementation:**
- `mercure-gateway.json` contains encrypted credential blocks (AES-256-GCM)
- On startup, if encrypted fields are detected, prompt for master password (via web UI or CLI flag)
- Master password derives encryption key via PBKDF2 (100k iterations, SHA-256)
- Credentials decrypted in memory only; never written to disk in plaintext
- For non-air-gapped sites, OS keyring integration is a v1.1 enhancement

---

## 3. Updated Scope Boundaries

### 3.1 MVP (v1.0) — What Changes

| PRD Feature | Original | Refined | Change |
|-------------|----------|---------|--------|
| Desktop shell | PySide6 or Tauri (TBD) | Tauri + FastAPI + React/Vue SPA | **Resolved** |
| Report formats | DICOM SR only | DICOM SR + Encapsulated PDF | **Expanded** |
| Routing rules | Send-all (no filtering) | Basic include/exclude by modality | **Expanded** |
| Forwarding | Serial | Concurrent (configurable limit) | **Enhanced** |
| Config UI | Guided wizard (desktop) | Web admin panel (localhost browser) | **Changed** |
| Credential storage | OS keyring | Encrypted config file + master password | **Changed** |
| Audit log | Chained hash (PRD spec) | Full chained SHA-256 hash | **Confirmed** |
| Compressed syntaxes | All (accept) | All (accept + selective decompress) | **Clarified** |

### 3.2 v1.1 — What Changes

| Feature | Original | Refined |
|---------|----------|---------|
| Windows service mode | v1.1 option | Confirmed v1.1 |
| Auto-update | v1.1 | Confirmed v1.1; must work with USB deployment model |
| Advanced forwarding rules | v1.1 | Confirmed; tag-based rules |
| Report transports (DICOMweb, HL7) | v1.1 | Confirmed; DICOMweb next priority |
| Hub registration + event streaming | v1.1 | Confirmed; additive to air-gapped mode |
| Linux builds | v1.1 | Confirmed |
| OS keyring integration | Not in PRD | Added to v1.1 |
| Diagnostics bundle export | v1.1 | Confirmed; critical for remote support model |

### 3.3 Non-Goals — Confirmed

- ❌ Full PACS or study viewer (reports only in v1)
- ❌ Multi-tenant fleet management (v2.0+)
- ❌ Built-in anonymization (defer to hub)
- ❌ Mobile apps
- ❌ Cloud control plane
- ❌ AI features (v2.0+ only, advisory only per PRD §4)
- ❌ HL7 integration (v1.1 experimental; not MVP)

---

## 4. Sprint Plan Impact

The existing 9-sprint plan (docs/sprints/README.md) requires updates to reflect these refinements:

### Sprint 01 — Phase 0 Spikes (Updated)

| Original Task | Refinement |
|---------------|------------|
| S01-T4: PySide6 vs Tauri shell spike | **Replace with:** Tauri + FastAPI spike — prototype localhost HTTP, sidecar, and command bridge; measure latency/RAM vs PRD §5.6 K6 targets |
| New: S01-T7 | **FastAPI + React/Vue spike:** boot FastAPI on localhost; serve a minimal SPA; measure cold-start time and memory footprint |

### Sprint 02 — Receiver (No Change)

| Task | Notes |
|------|-------|
| S02-T2: pynetdicom SCP transport | Must handle ALL compressed syntaxes (JPEG, JPEG 2000, JPEG-LS, RLE) — not just common ones |
| S02-T3: Store-before-acknowledge | Selective decompression for common syntaxes; pass-through for rare/proprietary |

### Sprint 03 — Forwarder (Updated)

| Original Task | Refinement |
|---------------|------------|
| S03-T1: DICOM SCU handler | Same, but now dispatches concurrently per `forwarding.concurrency` config |
| S03-T4: Manual re-forward | Same, but UI trigger comes from web admin panel, not Qt |
| New: S03-T7 | **Concurrent forwarding workers:** N workers claim tasks from spool; config-driven concurrency limit |

### Sprint 05 — Reports (Updated)

| Original Task | Refinement |
|---------------|------------|
| S05-T1: C-FIND/C-MOVE SR | **Expand to:** C-FIND/C-MOVE for both SR and Encapsulated PDF (filter by SOP Class UID) |
| S05-T2: Report viewer | **Expand to:** SR render + PDF viewer (embedded PDF viewer in web UI) |
| S05-T3: On-demand refresh | Same |

### Sprint 06 — Desktop Shell (Significantly Updated)

| Original Task | Refinement |
|---------------|------------|
| S06-T1: PySide6/Tauri shell | **Replace with:** Tauri shell + embedded webview loading FastAPI SPA |
| S06-T2: Tray icon | Tauri native tray icon (system tray integration) |
| S06-T3: Main window | Tauri window hosting the React/Vue SPA |
| S06-T4: Guided wizard | **Replace with:** Web-based setup wizard in the SPA (replaces desktop wizard) |
| S06-T5: Windows packaging | PyInstaller + Tauri bundler + Inno Setup; output is a single installer |
| S06-T6: Auto-start | Tauri auto-start plugin or Windows registry entry |

### New Sprint Tasks

| Sprint | New Task | Description |
|--------|----------|-------------|
| S01 | T7 | FastAPI + SPA spike (cold-start, RAM, localhost serving) |
| S03 | T7 | Concurrent forwarding workers with configurable limit |
| S06 | T7 | Web admin panel — config CRUD, queue viewer, report viewer, audit log viewer |
| S06 | T8 | Encrypted config file + master password prompt on startup |
| S06 | T9 | Config import/export via USB (JSON file) |

---

## 5. Database Schema Updates

### 5.1 reports table — Add PDF support

```sql
-- PRD §5.4 reports table update
ALTER TABLE reports ADD COLUMN sop_class_uid TEXT;  -- DICOM SOP Class UID
ALTER TABLE reports ADD COLUMN file_name TEXT;      -- original filename from C-MOVE

-- Index for SOP class filtering
CREATE INDEX idx_reports_sop_class ON reports(sop_class_uid);
```

### 5.2 New: forwarding_config table (optional, for runtime tuning)

```sql
CREATE TABLE forwarding_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
-- Stores runtime-adjustable settings: concurrency, poll interval, etc.
```

---

## 6. Configuration Model Updates

### 6.1 New: forwarding section

```jsonc
// Addition to mercure-gateway.json (PRD §5.5)
{
  // ... existing fields ...
  "forwarding": {
    "concurrency": 3,
    "queue_poll_interval_ms": 500
  }
}
```

### 6.2 Updated: reports section

```jsonc
"reports": {
  "enabled": false,
  "query_source": { "type": "dicom", "host": "pacs.local", "port": 104, "aet": "PACS" },
  "poll_interval_sec": 300,
  "on_retrieval": "store",
  "report_types": ["sr", "pdf"]   // NEW
}
```

### 6.3 Updated: receiver section

```jsonc
"receiver": {
  "ae_title": "GATEWAY",
  "port": 11112,
  "accept_compressed": true,
  "decompress_common": true,      // NEW: decompress JPEG/J2K/J-LS on receive
  "allowed_ae_titles": []
}
```

### 6.4 New: web_ui section

```jsonc
"web_ui": {
  "host": "127.0.0.1",
  "port": 8080,
  "auth_enabled": false,          // localhost-only by default; enable for shared machines
  "auth_password_hash": ""        // bcrypt hash; set via setup wizard
}
```

### 6.5 New: credentials section (encrypted)

```jsonc
"credentials": {
  "encrypted": true,
  "sftp_nas": {                   // per-destination credential block
    "type": "sftp",
    "username": "u",
    "password_encrypted": "AES256GCM:..."   // encrypted with master password
  }
}
```

---

## 7. API Surface (FastAPI)

> **Implementation status:** ✅ All 17 endpoints implemented in `src/mercure_gateway/web/routes.py` with 30+ RED tests.

The web admin panel needs a REST API. Key endpoints:

### 7.1 Configuration

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/config` | Get current configuration (redacted credentials) |
| PUT | `/api/config` | Update configuration |
| POST | `/api/config/import` | Import config from JSON file |
| GET | `/api/config/export` | Export config as JSON file |

### 7.2 Queue / Studies

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/studies` | List studies with pagination, filtering, sorting |
| GET | `/api/studies/{id}` | Study detail (routes, status, timestamps) |
| POST | `/api/studies/{id}/retry` | Re-forward a FAILED study |
| GET | `/api/studies/{id}/routes` | Per-destination routing status |
| GET | `/api/queue/stats` | Queue statistics (pending, sending, sent, failed counts) |

### 7.3 Reports

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/reports` | List reports with filtering |
| GET | `/api/reports/{id}` | Report detail |
| POST | `/api/reports/{id}/refresh` | Trigger on-demand report retrieval |
| GET | `/api/reports/{id}/content` | Get report content (SR rendered or PDF) |

### 7.4 Audit

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/audit` | List audit events with pagination |
| GET | `/api/audit/verify` | Verify audit chain integrity |
| GET | `/api/audit/export` | Export audit log (redacted) |

### 7.5 System

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/system/status` | Gateway status (receiver, forwarder, report retriever) |
| POST | `/api/system/start` | Start receiver + forwarder |
| POST | `/api/system/stop` | Graceful shutdown |
| GET | `/api/system/health` | Health check endpoint |

---

## 8. Risks Introduced by Refinements

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| Tauri + FastAPI integration complexity | Schedule | Med | Phase 0 spike validates before Sprint 06; fallback to standalone FastAPI + browser |
| PDF report viewer in web UI | Scope | Low | Use成熟 PDF.js library; wrap in React/Vue component |
| Concurrent forwarding edge cases (race conditions on study state) | Correctness | Med | Existing spool DB locking (BEGIN IMMEDIATE) provides atomicity; add concurrency tests |
| Encrypted config file UX (master password prompt on every start) | UX | Med | Optional; skip encryption for dev/test; auto-unlock via env var for CI |
| Air-gapped USB config deployment (file versioning, migration) | Operations | Low | Config version field; migration helper in main.py on startup |
| Web UI serving from Tauri webview (CORS, CSP) | Technical | Low | Tauri webview is same-origin; no CORS issues; CSP configurable in tauri.conf.json |

---

## 9. Open Questions (Post-Interview)

| # | Question | Impact | Default |
|---|----------|--------|---------|
| 1 | **Tauri ↔ FastAPI communication method** — localhost HTTP, sidecar, or command bridge? | Performance, architecture | Prototype in Phase 0 spike (S01-T4) |
| 2 | **React vs Vue** for the SPA? | Dev velocity, bundle size | Decide in Phase 0 spike; React baseline |
| 3 | **Web UI authentication for shared machines** — when is `auth_enabled: true` needed? | Security | Enable when multiple Windows users share a workstation |
| 4 | **Config migration strategy** — how to handle schema changes across gateway versions? | Operations | Version field + migration scripts in main.py |
| 5 | **PDF report size limits** — some PDFs are 50MB+; should there be a size gate? | Storage | Defer to v1.1; store all for MVP |
| 6 | **Report forwarding** — when `on_retrieval: "store_and_forward"`, should PDFs be forwarded as DICOM encapsulated or raw? | Integration | DICOM encapsulated PDF via C-STORE (standard) |
| 7 | **Multi-destination concurrency per study** — if study has 3 destinations and concurrency=3, does one study occupy all 3 workers? | Throughput | No; each destination is a separate task; one study can use multiple workers simultaneously |
| 8 | **Web UI offline capability** — should the SPA work if FastAPI backend is down? | UX | Show cached data + "backend offline" banner; no writes |

---

## 10. Success Criteria (Refined from PRD §1)

| KPI | Target | Refinement |
|-----|--------|------------|
| K1 | ≥99.9% persisted before forwarding | **Confirmed** — store-before-ack is non-negotiable |
| K2 | ≥99% delivered within retry_max | **Confirmed** — concurrent forwarding must not reduce reliability |
| K3 | ≥95% reports within SLA (5 min) | **Confirmed** — now includes both SR and PDF |
| K4 | Guided setup ≤10 min | **Refined:** Web-based wizard in browser; USB config import for air-gapped |
| K5 | 100% events audited | **Confirmed** — chained hash from day one |
| K6 | Installer ≤250 MB; idle RAM ≤150 MB | **Confirmed** — Tauri + FastAPI + React must fit within footprint |
| K7 (NEW) | Cold-start ≤5 s | **New:** Web UI must load within 5 seconds of app launch |
| K8 (NEW) | Concurrent forwarding throughput | **New:** ≥3 studies forwarded simultaneously without timeout increase |

---

*End of product refinement spec. This document should be reviewed against the original PRD and used to update the sprint plan (docs/sprints/).*
