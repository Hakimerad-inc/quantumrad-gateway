# mercure Gateway — Lightweight Desktop DICOM Ingestion Node

**Planning Document & Product Requirements Document (PRD)**

| Field | Value |
|-------|-------|
| **Product** | mercure Gateway (working title: `mercure-gateway`) |
| **Type** | Lightweight desktop DICOM gateway application |
| **Target platforms** | Windows (MVP), Linux (v1.1) |
| **Deployment model** | Small clinics / single sites |
| **Core role** | DICOM ingestion node → forwards to a central mercure hub or vendor PACS; retrieves study reports from PACS |
| **Version** | v1.0 draft |
| **Status** | Proposed |

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [User Experience & Functionality](#2-user-experience--functionality)
3. [Architecture & Data Flow](#3-architecture--data-flow)
4. [AI System Requirements](#4-ai-system-requirements)
5. [Technical Specifications](#5-technical-specifications)
6. [Security & Privacy](#6-security--privacy)
7. [Compliance & Audit](#7-compliance--audit)
8. [Integration with mercure Hub](#8-integration-with-mercure-hub)
9. [Implementation Plan (Phased Roadmap)](#9-implementation-plan-phased-roadmap)
10. [Testing & Validation Strategy](#10-testing--validation-strategy)
11. [Risks & Mitigations](#11-risks--mitigations)
12. [Success Metrics](#12-success-metrics)
13. [Open Questions / TBD](#13-open-questions--tbd)
14. [Appendix — User Stories & Acceptance Criteria](#14-appendix--user-stories--acceptance-criteria)

---

## 1. Executive Summary

### Problem Statement

Small clinics, private practices, and single imaging sites often lack the IT staff to run a full server-side DICOM orchestration stack (mercure hub, PACS, or both). They still need a reliable way to (a) receive DICOM studies from their modalities, (b) forward them to a central mercure hub or an existing vendor PACS, and (c) pull study reports back from the PACS — all from a simple application that installs and runs on a normal desktop PC (Windows first).

### Proposed Solution

A **lightweight desktop DICOM gateway** (`mercure-gateway`) built in **Python (core) with a Qt/Tauri desktop shell**, that:
- Acts as a local DICOM **C-STORE SCP (receiver)** on the workstation,
- **Forwards** received studies automatically to configured destinations (a central mercure hub, vendor PACS, or other DICOM/SFTP/rsync targets),
- **Retrieves study reports** from PACS via a flexible retrieval layer (DICOM SR via C-FIND/C-MOVE, with pluggable support for other report transports),
- Maintains a **local tamper-evident audit log** (chained SHA-256) with **optional reporting back to a mercure hub/bookkeeper**,
- Runs with a simple tray/desktop UI, minimal configuration, and zero server hardware.

### Success Criteria (KPIs)

| # | KPI | Target |
|---|-----|--------|
| K1 | Studies received without data loss | ≥ 99.9% of accepted studies persisted locally before forwarding |
| K2 | Forwarding reliability | ≥ 99% of studies delivered to configured destination within `retry_max` attempts |
| K3 | Report retrieval success rate | ≥ 95% of requested study reports retrieved within the SLA window (default 5 min) |
| K4 | Time-to-first-study-forwarded after install (non-technical user) | ≤ 10 minutes from guided setup |
| K5 | Audit completeness | 100% of events (receive, forward, retrieve, error) written to local audit log |
| K6 | Deployment size / footprint | Installer ≤ 250 MB; idle RAM ≤ 150 MB; no external server dependency |

---

## 2. User Experience & Functionality

### 2.1 User Personas

| Persona | Description | Needs |
|---------|-------------|-------|
| **Clinic Radiographer/Tech** | Runs imaging devices; not technical. Installs and configures the gateway on a workstation. | Simple guided setup, clear status, minimal maintenance. |
| **Practice Administrator** | Manages a small clinic; cares about where data goes and whether it arrived. | Dashboard, forwarding history, alerts, report retrieval status. |
| **Practice Radiologist** | Reads studies; wants reports available. | Reliable report retrieval; knows when a report is ready. |
| **Mercure Hub Admin** | Runs the central mercure hub that receives clinic data. | Gateways register/report to the hub; receives studies reliably. |
| **Support Engineer** | Troubleshoots remote sites. | Remote diagnostics, structured logs, re-forward tooling. |

### 2.2 Primary User Flows

#### Flow A — First-run setup (guided wizard)
1. Launch gateway → guided wizard (Windows Installer runs app post-install).
2. Choose role: **"Send to mercure hub"**, **"Send to PACS"**, or **"Send to multiple destinations"**.
3. Enter local receiver settings (port, local AET — defaults pre-filled: port 11112, AET `GATEWAY`).
4. Enter destination(s): mercure hub URL/port + AET, or PACS host/port/AET (reuse target type templates from mercure: DICOM, DICOM-TLS, DICOMweb, SFTP, rsync, Folder, S3, XNAT).
5. (Optional) Enable **report retrieval** and configure the PACS query source.
6. (Optional) Enable **mercure reporting** and enter hub bookkeeper/registration details.
7. Validate connectivity (echo to each destination) → green/red status → Save.

#### Flow B — Daily operation
1. Modality sends DICOM → gateway receiver accepts and persists to local spool.
2. Gateway evaluates forwarding rules → sends to each configured destination.
3. Status reflects in tray icon (idle/sending/error) and main window queue view.
4. Reports: gateway polls/retrieves reports for completed studies (per configuration) → stores locally and/or forwards.
5. All events written to local audit log; optionally streamed to mercure hub.

#### Flow C — Failure & recovery
1. Destination offline → task enters retry with backoff (`retry_delay`, `retry_max`).
2. After `retry_max`, study flagged in error; local copy **retained** (never deleted automatically).
3. Tray icon shows error state + notification; user opens UI, sees failed tasks, clicks **Retry** or **Re-forward**.
4. Disk-spool protection: configurable max spool size; oldest-cleared only when retention expired and delivered.

#### Flow D — Report viewing
1. User opens **Reports** tab.
2. Lists studies with retrieval status (pending / retrieved / failed).
3. Opens retrieved report (DICOM SR rendered or PDF/text viewer).
4. Manual "Refresh report" and "Request report" actions for on-demand pulls.

### 2.3 Feature List (MVP → Later)

**MVP (v1.0):**
- DICOM C-STORE SCP receiver (pynetdicom), **all** compressed syntaxes (JPEG 2000, JPEG-LS, RLE) with selective decompression
- Local SQLite spool, key-guarded (persist before forward — "store-and-forward")
- Forwarding to DICOM target(s) (pynetdicom C-STORE SCU) — mercure hub or PACS
- Multiple destinations per study (configurable); **concurrent forwarding workers** (configurable limit)
- **Basic modality include/exclude routing rules** (MVP); advanced rules in v1.1
- Retry with exponential backoff + max attempts
- **Tauri + FastAPI + React/Vue SPA** web admin panel (localhost:8080); replaces desktop shell
- **Web-based guided first-run wizard** (in SPA browser)
- Local audit log (chained SHA-256 hash + rotating text log)
- Report retrieval MVP: **DICOM SR + Encapsulated PDF** via C-FIND/C-MOVE (query by accession/study UID)
- Report viewer (SR render + embedded PDF viewer)
- Manual re-forward and retry of failed tasks
- **Encrypted config file** with master password (air-gapped credential storage)
- **Config import/export** via USB (JSON file)
- **Recovery scan** on startup (reconcile spool files with DB)

**v1.1 (Windows polish + Linux):**
- Linux builds (AppImage/deb)
- SFTP / rsync / Folder / S3 / XNAT / DICOMweb destination types (reuse mercure target handlers)
- Advanced forwarding rules (by modality, patient, accession, series description)
- Automatic mercure hub registration & reporting (event streaming to bookkeeper API)
- Report retrieval pluggable transports: DICOMweb QIDO/WADO, HL7/FHIR endpoint (experimental), email-to-folder
- Auto-update mechanism
- Remote diagnostics bundle export (for support)
- **Windows service mode** (headless always-on)
- **OS keyring integration** (upgrade from encrypted config file)

**USB Dongle Variant (Sprint 10):**
- Dual-mode USB (bootable Linux + Windows auto-launch)
- **Hot-unplug detection** with graceful shutdown sequence
- Recovery scan on USB boot (reconcile interrupted studies)
- LED status indicator (if hardware supports)
- USB-specific storage budget and aggressive retention
- USB flashing documentation + script

**v2.0 (later, non-goals for MVP):**
- Multi-gateway fleet management/central config push
- Built-in anonymization engine (defer to mercure hub processing unless explicitly requested)
- Scheduling engine (off-peak forwarding windows)
- Embedded viewer of full DICOM studies (reports only in v1)

### 2.4 User Stories

See [Appendix — User Stories & Acceptance Criteria](#14-appendix--user-stories--acceptance-criteria) for the full backlog.

### 2.5 Non-Goals (MVP)

- ❌ Not a full PACS or viewer — no comprehensive study viewing/annotation.
- ❌ Not a replacement for the mercure hub — gateway is a light edge node; heavy routing/processing stays centralized.
- ❌ No multi-tenant fleet management server in v1.
- ❌ No built-in HL7 integration in v1 (report retrieval transports pluggable; HL7 deferred to v1.1).
- ❌ No mobile apps.
- ❌ No cloud-hosted control plane in v1.

---

## 3. Architecture & Data Flow

### 3.1 High-Level Architecture

```
┌───────────────────────────  Desktop PC (Windows/Linux) ───────────────────────────┐
│                                                                                  │
│   ┌─────────────┐   ┌───────────────────────┐   ┌────────────────────────────┐   │
│   │  Modalities  │──▶│  mercure-gateway      │──▶│  Central mercure hub       │   │
│   │  (CT/MR/US,  │   │                       │   │  (or vendor PACS)          │   │
│   │  any C-STORE)│   │  Receiver (C-STORE    │   │  ┌──────────────────────┐  │   │
│   └─────────────┘   │  SCP) ──▶ Local spool  │   │  │ mercure receiver /   │  │   │
│                     │  (store-and-forward)   │──▶│  │ router / processor /  │  │   │
│                     │          │             │   │  │ dispatcher            │  │   │
│                     │          ▼             │   │  └──────────────────────┘  │   │
│                     │  Forwarding engine     │   └────────────────────────────┘   │
│                     │  (multi-destination,   │   ┌────────────────────────────┐   │
│                     │   retry, backoff)      │──▶│  Vendor PACS (direct)      │   │
│                     │          │             │   └────────────────────────────┘   │
│                     │          ▼             │   ┌────────────────────────────┐   │
│                     │  Report retrieval      │◀──│  PACS query source         │   │
│                     │  (C-FIND/C-MOVE, SR)   │   │  (C-FIND/C-MOVE, DICOMweb) │   │
│                     │          │             │   └────────────────────────────┘   │
│                     │          ▼             │                                     │
│                     │  Local audit log (SQL) │                                     │
│                     │  + optional hub report │                                     │
│                     └───────────────────────┘                                     │
└──────────────────────────────────────────────────────────────────────────────────┘
```

### 3.2 Component Interaction

| Component | Responsibility | Key technology |
|-----------|----------------|----------------|
| **Receiver (SCP)** | Accepts C-STORE associations from modalities; writes received DICOM + extracted tags to spool | `pynetdicom` (or DCMTK `storescp` via wrapper) |
| **Spool** | Local store-and-forward queue (study metadata + file paths + state machine) | SQLite + key-required HMAC verifier (ADR-0004); filesystem storage of DICOM |
| **Forwarding Engine** | Reads spool; sends to configured destinations; tracks per-destination status; retries with backoff | Reuse mercure `target_types` handler pattern (`dcmsend`, pynetdicom, SFTP, etc.) |
| **Report Retrieval** | Queries PACS for reports (DICOM SR via C-FIND/C-MOVE; pluggable transports) | pynetdicom; later: DICOMweb QIDO/WADO, HL7/FHIR |
| **Audit Log** | Append-only event log (receive/forward/retrieve/error); tamper-evident via chained SHA-256 | SQLite + rotating log; optional streaming to mercure bookkeeper |
| **Desktop Shell / UI** | Tray icon, main window (queue/status/logs/settings/reports), guided wizard | Qt (PySide6) or Tauri shell wrapping the Python core |
| **Config Store** | Validated local configuration (JSON, mirrors mercure config style) | `pydantic`-validated JSON |
| **Hub Reporter** | Optional: registers gateway + streams events to mercure hub bookkeeper | mercure `bookkeeper` API / REST |

### 3.3 State Machine (Study lifecycle in gateway)

```
RECEIVING → RECEIVED → QUEUED → SENDING → SENT (per destination)
                              │
                              └─▶ ERROR → RETRY (backoff) → ... → FAILED (retained, manual retry)
Reports: PENDING → RETRIEVING → RETRIEVED → (viewable) / RETRIEVAL_FAILED
```

### 3.4 Key Design Principles

1. **Store-and-forward first** — a study is persisted to the local spool *before* acknowledgment/forwarding; no data loss if destination is down.
2. **Single source of truth** — the local SQLite spool is authoritative; UI, forwarding, audit, and reporting all read/write it.
3. **Reuse mercure concepts** — target types, config style (`mercure.json`-like), AET semantics, event vocabulary → low cognitive load for existing mercure users and shared code where feasible.
4. **Zero external runtime dependency** for base operation — no database server, no container runtime; a self-contained desktop app.
5. **Fail-safe retention** — never auto-delete undelivered data; retention applies only to *delivered* studies (mirrors mercure cleaner semantics).
6. **Lightweight** — target ≤ 150 MB idle RAM, ≤ 250 MB installer, minimal CPU.

---

## 4. AI System Requirements

The **core gateway has no AI** in v1. However, optional AI-adjacent features are scoped for later:

| Feature | Version | Tooling | Evaluation |
|---------|---------|---------|------------|
| Report text summarization / triage (optional, experimental) | v2.0+ | Local LLM or rule-based extractor | Precision@k on 50 annotated reports; human review of 10% sample |
| Auto-routing suggestion by content (optional) | v2.0+ | Classifier on DICOM tags + report text | ≥ 90% agreement with manually configured rules on benchmark set |

These are **explicitly non-goals for MVP** and are listed only to bound scope. If pursued, they will follow the evaluation strategy below.

### Evaluation Strategy (if AI features are added)
- **Benchmark set**: 50 annotated study/report pairs per modality.
- **Pass rate**: ≥ 90% correct output on benchmark for any shipped AI feature.
- **Human-in-the-loop**: every AI suggestion is advisory; no autonomous destructive action.
- **Privacy**: no PHI leaves the machine; inference runs locally.

---

## 5. Technical Specifications

### 5.1 Tech Stack (decided with stakeholder)

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Language | **Python 3.11+** | Matches mercure core; ecosystem (pynetdicom, dcmtk bindings, pydantic) |
| DICOM I/O | **pynetdicom** (SCP + SCU), DCMTK tools (`dcmsend`, `storescp`, `getdcmtags`) | Mercure uses DCMTK for the receiver; pynetdicom for flexible SCP/SCU |
| Desktop shell | **Tauri** shell wrapping **FastAPI** backend serving **React/Vue SPA** on localhost:8080 | Native tray + web admin panel; SPA works in any browser; ADR-0002 |
| Web backend | **FastAPI** + **uvicorn** (Python) — REST API + SPA static server | Async, auto-generated OpenAPI docs, pydantic integration |
| Storage | **SQLite** for spool + audit; filesystem for DICOM blobs | Zero-config, embedded, robust |
| Config | **JSON + pydantic** (reuse mercure `Config`/`Target`/`Rule` models where sensible) | Consistency with mercure |
| Packaging | **Tauri bundler** + Inno Setup (Windows); AppImage/deb (Linux) | Self-contained desktop app |
| Networking to hub | **REST/HTTPS** to mercure bookkeeper & (optional) DICOM to mercure receiver | Matches mercure architecture |

> **Decision (ADR-0002):** Tauri + FastAPI + React/Vue SPA chosen over PySide6. Communication via localhost HTTP (method 1 per ADR-0002); Tauri webview loads `http://127.0.0.1:8080`. Benchmark: ~22ms request latency, ~36MB FastAPI idle RSS, total ~70-90MB estimated (within K6 ≤150MB target).

### 5.2 Data Flow (end-to-end)

1. Modality opens C-STORE association to gateway receiver (port 11112 default).
2. Receiver validates AE title (allow-list or any), stores DICOM files to `spool/{studyUID}/{seriesUID}/`, writes `*.tags` (via `getdcmtags`) and a spool DB row (`state=RECEIVED`).
3. Forwarding engine picks up `RECEIVED` rows → for each enabled destination, sends via destination handler → updates per-destination status → when all destinations done: `SENT`, files moved to delivered retention area.
4. If a destination fails: `ERROR` → schedule retry (`retry_delay` backoff × `retry_max`) → `FAILED`; local copy retained; UI shows actionable failure.
5. Report retrieval: for studies marked "report wanted" (by rule/config), gateway issues C-FIND/C-MOVE to the configured PACS query source by Accession/Study UID → retrieves DICOM SR → stores to `reports/` → updates report status → visible in UI.
6. Every transition → audit event (local + optional hub stream).

### 5.3 Integration Points

| Integration | Direction | Protocol | Purpose |
|-------------|-----------|----------|---------|
| Modalities | Inbound | DICOM C-STORE | Receive studies |
| mercure hub | Outbound | DICOM C-STORE (to hub receiver) | Forward studies for routing/processing |
| Vendor PACS | Outbound | DICOM C-STORE (or SFTP/rsync/DICOMweb) | Direct forward |
| PACS query source | Bidirectional | C-FIND/C-MOVE (DICOM SR); later DICOMweb QIDO/WADO | Report retrieval |
| mercure bookkeeper | Outbound (optional) | REST/HTTPS | Register gateway, stream events (monitoring) |
| Auto-update server | Outbound (v1.1) | HTTPS | App updates |

### 5.4 Database Schema (SQLite — spool & audit)

**studies**
| Column | Type | Notes |
|--------|------|-------|
| id | PK | |
| study_uid | TEXT | |
| accession | TEXT | indexed |
| mrn | TEXT | |
| patient_name | TEXT | |
| modality | TEXT | |
| study_description | TEXT | |
| study_date | TEXT | |
| num_series / num_instances | INT | |
| state | TEXT | RECEIVED/QUEUED/SENDING/SENT/ERROR/FAILED |
| created_at / updated_at | DATETIME | |
| retention_delivered_at | DATETIME | when eligible for deletion |

**destinations / task_routing**
| Column | Type | Notes |
|--------|------|-------|
| id | PK | |
| study_id | FK | |
| target_name | TEXT | |
| target_type | TEXT | dicom/sftp/rsync/... |
| status | TEXT | waiting/sending/complete/error |
| attempts | INT | |
| last_error | TEXT | |

**reports**
| Column | Type | Notes |
|--------|------|-------|
| id | PK | |
| study_id | FK | |
| accession / study_uid | TEXT | |
| report_type | TEXT | SR / PDF / TEXT |
| status | TEXT | pending/retrieving/retrieved/failed |
| file_path | TEXT | |
| retrieved_at | DATETIME | |

**audit_events**
| Column | Type | Notes |
|--------|------|-------|
| id | PK | |
| ts | DATETIME | |
| event | TEXT | RECEIVED/FORWARD_START/FORWARD_COMPLETE/FORWARD_ERROR/REPORT_RETRIEVED/ERROR |
| detail | JSON | |
| user | TEXT | (for UI actions) |
| hash | TEXT | chained hash for tamper-evidence (audit) |

### 5.5 Configuration Model (draft)

```jsonc
// mercure-gateway.json
{
  "general": {
    "appliance_name": "Gateway-CLI-01",
    "locale": "en",
    "log_level": "INFO"
  },
  "receiver": {
    "ae_title": "GATEWAY",
    "port": 11112,
    "accept_compressed": true,
    "decompress_common": true,          // NEW: decompress JPEG/J2K/J-LS on receive
    "allowed_ae_titles": []            // empty = accept any
  },
  "destinations": [
    // Reuse mercure target shape; examples:
    { "name": "hub", "type": "dicom", "host": "mercure.example.org", "port": 11112,
      "aet_target": "MERCURE", "aet_source": "GATEWAY", "enabled": true },
    { "name": "pacs", "type": "dicom", "host": "pacs.local", "port": 104, "aet_target": "PACS", ... }
  ],
  "forwarding": {                       // NEW: concurrent forwarding settings
    "concurrency": 3,                   // max concurrent forwarding workers
    "queue_poll_interval_ms": 500       // how often workers poll for new tasks
  },
  "forwarding_rules": [                 // optional, v1.1 advanced
    { "rule": "modality:CT", "targets": ["hub"], "priority": "normal" }
  ],
  "reports": {
    "enabled": false,
    "query_source": { "type": "dicom", "host": "pacs.local", "port": 104, "aet": "PACS" },
    "poll_interval_sec": 300,
    "on_retrieval": "store",            // store | store_and_forward (to hub)
    "report_types": ["sr", "pdf"]      // NEW: which report SOP classes to retrieve
  },
  "audit": {
    "local": true,
    "encrypt": true,                     // RESERVED — accepted but not yet wired; see §6.1
    "hub_reporting": { "enabled": false, "bookkeeper_url": "", "api_key": "" }
  },
  "storage": {
    "spool_dir": "C:\\mercure-gateway\\spool",
    "max_spool_gb": 20,
    "retention_delivered_days": 3,
    "disk_full_warning_pct": 90,        // NEW: capacity warning threshold
    "purge_on_disk_full": false         // NEW: auto-purge oldest delivered on disk full
  },
  "web_ui": {                            // NEW: web admin panel settings
    "host": "127.0.0.1",
    "port": 8080,
    "auth_enabled": false,               // enable for shared machines
    "auth_password_hash": ""             // pbkdf2$iters$salt$key; --set-web-password
  },
  "credentials": {                       // NEW: encrypted credential storage
    "encrypted": true,                   // AES-256-GCM encrypted blocks
    "entries": {}                        // per-destination credential blocks
  },
  "usb_mode": {                          // NEW: USB dongle variant settings
    "enabled": false,                    // auto-detected when spool is on removable media
    "storage_budget_gb": 18,             // max data partition usage
    "retention_delivered_hours": 24,     // aggressive cleanup for USB
    "hot_unplug_safe": true,             // graceful shutdown on USB removal
    "auto_start_on_boot": true,          // auto-start on USB boot/plug-in
    "led_enabled": false,                // hardware LED support
    "led_pin": "GPIO18"                  // GPIO pin for LED (Linux BCM numbering)
  }
}
```

### 5.6 Performance Requirements

| Metric | Requirement |
|--------|-------------|
| Receiver throughput | Accept ≥ 5 concurrent C-STORE associations; sustained ≥ 30 MB/s write |
| Forwarding latency | Begin forwarding ≤ 2 s after study marked complete (no queue backlog) |
| UI responsiveness | Tray/menu actions ≤ 200 ms; queue view renders ≤ 500 ms for 10k rows (paginated) |
| Memory (idle) | ≤ 150 MB RSS |
| Disk | Spool + SQLite overhead minimal; installer ≤ 250 MB |

---

## 6. Security & Privacy

### 6.1 Data Handling

- **At rest**: plain SQLite, guarded by a key-required HMAC verifier — a database that was opened with a key stores a `salt:hmac` verifier in its `db_meta` table and refuses to open without the same key (`DatabaseEncryptionError`; ADR-0004). This is a plaintext-open guard, not a cipher: the database itself is not SQLCipher-encrypted (that upgrade was formally evaluated and declined on 2026-09-09 — ADR-0004 amendment). DICOM files on disk are plaintext per deployment norm; OS-level full-disk encryption (BitLocker/LUKS/FileVault) on the data volume is the site requirement for the single-user, single-workstation model (§6.2). Secrets — destination passwords, the hub API key, the web UI password hash — are stored as AES-256-GCM blocks in the config vault under a master password (§6.2).
- **In transit**: DICOM forwarding supports DICOM-TLS target type; SFTP/rsync over SSH; DICOMweb over HTTPS; hub reporting over HTTPS.
- **PHI**: gateway inherently handles PHI (DICOM studies). No PHI in UI-external telemetry; audit detail includes metadata but configurable PHI scoping.

### 6.2 Authentication & Authorization

- Local app access: Windows user session (single-user by design in v1); no remote admin.
- Hub reporting: API key (`BOOKKEEPER_API_KEY`-style) over HTTPS.
- Outbound destinations: per-target credentials (SFTP/rsync/S3 keys) stored encrypted in local credential store (OS keyring, fallback encrypted file).

### 6.3 Threat Model (v1 scope)

| Threat | Mitigation |
|--------|------------|
| Rogue modality floods receiver | AE allow-list option, rate limiting, max-spool guard |
| Data loss on crash | Store-and-forward; fsync before ack; recovery scan on start |
| Unauthorized local access | OS user session + encrypted store |
| Man-in-the-middle to PACS/hub | TLS (DICOM-TLS, HTTPS) support |
| Tampered audit trail | Chained hash over audit events |

### 6.4 Privacy & Compliance Notes

- Designed to help sites meet HIPAA/GDPR data-movement obligations; **not** a substitute for a full compliance program.
- PHI scoping configurable in audit/reporting; default = minimal metadata only.
- Local-only operation mode (no hub reporting) fully supported for privacy-sensitive sites.

---

## 7. Compliance & Audit

- **Audit log**: append-only, chained-hash (tamper-evident, not encrypted — see §6.1); records every receive/forward/retrieve/error with timestamps.
- **Export**: one-click bundle export (config redacted + structured log) for support/audit.
- **Regulatory**: align with mercure's posture; document responsibilities in end-user agreement (data processor/controller split depends on deployment).
- **Log retention**: configurable (default 1 year local).

---

## 8. Integration with mercure Hub

### 8.1 Forwarding into mercure

- Gateway C-STORE → mercure **receiver** (same as any modality). mercure then applies its normal routing/rules/processing. Gateway is deliberately thin; all routing intelligence stays in the hub.

### 8.2 Optional hub registration & reporting (v1.1)

- On first run (if configured), gateway registers itself with the hub (`POST /register-gateway`) with name, version, contact.
- Streams lifecycle events (`RECEIVED`, `FORWARD_*`, `REPORT_*`) to mercure bookkeeper → visible in hub monitoring/dashboards.
- Keeps hub operators aware of edge-site health without the gateway being a full mercure instance.

### 8.3 Reuse opportunities

- Reuse mercure's `target_types` handlers (dicom, sftp, rsync, folder, s3, dicomweb, xnat) as a shared library where practical.
- Reuse mercure `Config`/`Target`/`Rule` pydantic models and tag-extraction (`getdcmtags`/`*.tags`) approach.

---

## 9. Implementation Plan (Phased Roadmap)

### Phase 0 — Discovery & Prototype (2–3 weeks)
- Spike: **Tauri + FastAPI + React/Vue SPA** integration (ADR-0002); measure cold-start, RAM, communication methods.
- Spike: pynetdicom SCP throughput + DCMTK storescp comparison.
- Spike: USB boot compatibility (Linux + Windows auto-launch).
- Confirm report-retrieval transport priorities (DICOM SR + PDF).
- **Exit criteria:** working 15-min demo: modality → gateway → mercure hub; report pulled back from test PACS (Orthanc); Tauri+FastAPI integration validated.

### Phase 1 — MVP (v1.0, ~8–10 weeks)
- Core: receiver (SCP), spool (SQLite+files), forwarding to DICOM target(s), **concurrent workers**, retry/backoff, fail-safe retention.
- **Web admin panel**: FastAPI REST API + SPA (queue/status/logs/config/reports/audit).
- **Web-based guided first-run wizard** (in SPA browser).
- Audit: local tamper-evident log (chained SHA-256 hash; at-rest protection is OS-level FDE plus the key-required DB guard — §6.1).
- Reports MVP: **DICOM SR + Encapsulated PDF** via C-FIND/C-MOVE, report viewer (SR render + embedded PDF).
- **Encrypted config file** with master password; config import/export via USB.
- **Recovery scan** on startup (reconcile spool files with DB).
- Windows packaging (Tauri bundler + Inno Setup), auto-start option.
- Docs: user guide section, admin guide.
- **Exit criteria:** all MVP acceptance criteria (§14) green; installer tested on clean Windows VM.

### Phase 2 — Windows polish + Linux (v1.1, ~6–8 weeks)
- Linux builds (AppImage/deb), packaging automation.
- Additional destination types (SFTP/rsync/Folder/S3/XNAT/DICOMweb).
- Forwarding rules (by modality/patient/accession/description) — reuse mercure rule syntax.
- Report retrieval pluggable transports: DICOMweb, HL7/FHIR (experimental).
- mercure hub registration + event streaming.
- Auto-update; diagnostics bundle export.
- **Windows service mode** (headless always-on).
- **OS keyring integration** (upgrade from encrypted config file).
- **Exit criteria:** cross-platform CI green; rules feature acceptance tested.

### USB Dongle Variant (~2 weeks)
- Dual-mode USB (bootable Linux + Windows auto-launch).
- **Hot-unplug detection** with graceful shutdown sequence.
- Recovery scan on USB boot.
- LED status indicator (if hardware supports).
- USB-specific storage budget and aggressive retention.
- USB flashing documentation + script.
- **Exit criteria:** USB boots on 3+ PC models; hot-unplug safe; K9/K10 perf gates met.

### Phase 3 — Hardening & scale-out (v2.0+, later)
- Fleet management / central config push (server component — new scope).
- Optional AI features (summarization/triage) per §4 — advisory only.
- Anonymization module (defer unless demanded).
- **Exit criteria:** pilot at 2–3 real sites for 1 month.

### Team & Skills Needed
- Python backend (pynetdicom/DCMTK, FastAPI, async)
- Desktop UI (Tauri + React/Vue SPA)
- DICOM domain knowledge
- Windows packaging + code signing
- (Later) CI/CD, fleet server dev

---

## 10. Testing & Validation Strategy

| Layer | Approach |
|-------|----------|
| **Unit** | pytest on receiver, spool state machine, retry logic, config validation, audit hashing |
| **Integration** | Real DICOM: Orthanc (test PACS) ↔ gateway ↔ mercure dev stack; pynetdicom SCU fixtures |
| **E2E** | Playwright for Qt/Tauri UI? → prefer UI-level: scripted flows with fake SCP/SCU (assert spool→forward→report chain) |
| **Destructive/failure** | Kill destination mid-transfer → assert retry + retained copy; disk-full simulation; crash-recovery scan |
| **Perf** | Throughput + memory benchmark suite; installer size gate |
| **Security** | Dependency audit (pip-audit), TLS test, AE allow-list test, tamper-evidence test on audit chain |
| **Manual/UAT** | Guided install on clean Windows VM; wizard walkthrough with non-technical user; report retrieval with real SR |

### CI
- GitHub Actions: lint (ruff/flake8), typecheck (mypy), unit+integration (in containers with Orthanc + mercure hub), packaging smoke test.
- Reuse mercure's existing CI patterns.

---

## 11. Risks & Mitigations

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| Desktop packaging/UI complexity slows MVP | Schedule | Med | Prototype first; keep UI thin (tray + basic window); defer polish |
| pynetdicom SCP throughput insufficient | Performance | Low-Med | DCMTK `storescp` fallback (mercure-proven) behind an abstraction |
| Report retrieval heterogeneity across PACS | Scope creep | High | Pluggable transports; MVP only DICOM SR; document per-PACS caveats |
| Security review findings (PHI handling) | Compliance | Med | Key-guarded DB + encrypted credential vault (ADR-0004), TLS, audit chain, redacted exports, external security review before v1.0 release |
| Fragmentation with mercure core (two codebases) | Maintenance | Med | Reuse models/handlers where practical; document boundary; align event vocabulary |
| Small team capacity for Windows + Linux | Schedule | Med | Ship Windows MVP first (per decision); Linux in v1.1 |
| TLS/credential management complexity on Windows | Security | Med | OS keyring integration; documented fallback encrypted file |

---

## 12. Success Metrics

- **Adoption**: ≥ 5 pilot sites by end of Phase 2; ≥ 1 non-technical user completes guided install unaided.
- **Reliability**: K1 (≥99.9% no-loss), K2 (≥99% delivered), K5 (100% audited).
- **Reports**: K3 (≥95% retrieved within SLA); ≥ 50 real DICOM SR retrievals verified.
- **Performance**: K6 footprint/latency targets met.
- **Support**: p50 time-to-resolve support tickets ≤ 1 business day (via diagnostics export).

---

## 13. Open Questions / TBD

| # | Question | Impact | Resolved |
|---|----------|--------|----------|
| 1 | **Shell choice**: PySide6 (Qt) vs Tauri web shell | UI dev speed, RAM | **Resolved:** Tauri + FastAPI + React/Vue SPA (ADR-0002). Benchmark: ~22ms latency, ~70-90MB total RSS. |
| 2 | Report retrieval transport priority after DICOM SR | Scope | DICOM SR + PDF first (MVP); DICOMweb next (v1.1); HL7/FHIR experimental |
| 3 | Should gateway run as Windows **service** (headless) or user-tray app? | Ops | Tray app (MVP); service mode v1.1 option |
| 4 | Anonymization at the edge (v2)? | Scope | Defer to hub unless a site demands it |
| 5 | Auto-update signing/code-signing vendor | Ops/Security | Decide before v1.1 release |
| 6 | Licensing model for the app (MIT like mercure?) | Business | MIT, consistent with mercure |
| 7 | Hub reporting API shape (new bookkeeper endpoints vs reuse) | Integration | Reuse/extend mercure bookkeeper; confirm in Phase 0 |
| 8 | Minimum supported OS versions (Win10/11? Win Server?) | QA matrix | Win10/11 x64; Win Server documented-only |
| 9 | **Tauri ↔ FastAPI communication method** | Performance | Localhost HTTP (method 1, ADR-0002); sidecar upgrade path for v1.1 |
| 10 | **React vs Vue** for SPA | Dev velocity | React baseline; Vue alternative documented |
| 11 | **USB device type** (flash drive vs custom embedded) | BOM cost | Standard USB 3.0 flash for MVP; custom board v1.1+ |
| 12 | **Standalone hardware** (Raspberry Pi, external power) | Scope | Deferred to v1.1+ |

---

## 14. Appendix — User Stories & Acceptance Criteria

### P1 (MVP) — Receiver & Spool

- **US-01** — As a clinic tech, I want the gateway to accept DICOM from any of our modalities so studies arrive automatically.
  - AC: Receiver binds configured port/AET; accepts ≥ 5 concurrent associations; stores all instances before ack; **handles ALL compressed syntaxes** (JPEG 2000, JPEG-LS, RLE) with selective decompression; writes `.tags`.
- **US-02** — As an admin, I want studies persisted before forwarding so nothing is lost if a destination is down.
  - AC: On receiver failure mid-transfer, partial study retained and marked incomplete; **recovery scan on startup** reconciles spool files with DB.

### P1 — Forwarding

- **US-03** — As an admin, I want studies forwarded to the mercure hub (or PACS) automatically.
  - AC: Configured destinations reachable → studies sent within 2 s of completion; per-destination status tracked; **concurrent workers** (configurable `forwarding.concurrency`).
- **US-04** — As an admin, I want automatic retries with backoff and manual re-forward of failures.
  - AC: Retry schedule honored (`retry_delay` × attempts ≤ `retry_max`); FAILED tasks retryable from UI/web admin; local copy never auto-deleted.

### P1 — Reports

- **US-05** — As a radiologist, I want to retrieve reports from the PACS for a study.
  - AC: Report lookup by Accession/Study UID; **DICOM SR + Encapsulated PDF** retrieved via C-FIND/C-MOVE; status transitions (pending→retrieved/failed) correct; retrieved report viewable (SR rendered + embedded PDF viewer).
- **US-06** — As a radiologist, I want on-demand report refresh.
  - AC: Manual "Request report" triggers immediate retrieval; supports **type-specific request** (SR, PDF, or both); result surfaced in web admin.

### P1 — Audit & UI

- **US-07** — As an admin, I want a complete, tamper-evident local audit log.
  - AC: Every event recorded with **chained SHA-256 hash**; log exportable (redacted) via web admin; the spool database's key-required at-rest guard on by default (ADR-0004 — a guard, not SQLCipher; see §6.1).
- **US-08** — As a clinic tech, I want a guided first-run wizard.
  - AC: Non-technical user completes **web-based setup** ≤ 10 min; connectivity validated at each step; status clearly shown in SPA.

### P1 — Web Admin & Config

- **US-08b** — As an admin, I want a web-based admin panel for managing the gateway.
  - AC: FastAPI REST API with 17+ endpoints; SPA with dashboard, queue, reports, audit, config tabs; served on localhost:8080; accessible via browser or Tauri shell.
- **US-08c** — As an admin, I want encrypted credential storage for air-gapped deployments.
  - AC: Credentials stored as AES-256-GCM encrypted blocks in config; master password on startup; config import/export via USB.

### P2 (v1.1)

- **US-09** — As an admin, I want advanced forwarding rules (by modality/patient/accession/description).
  - AC: Rules evaluated on tags; multiple targets per rule; priority respected; rule tester included.
- **US-10** — As a hub operator, I want gateways to register and stream events to the mercure hub.
  - AC: Registration succeeds; events appear in hub monitoring; failure of reporting does not block forwarding.
- **US-11** — As a Linux user, I want the same gateway on Linux.
  - AC: AppImage/deb build; same feature set as Windows v1.1; CI cross-platform green.

### USB Dongle Variant

- **US-12** — As a clinic tech, I want to run the gateway from a USB device plugged into any workstation.
  - AC: USB boots in **dual mode** (Linux boot + Windows auto-launch); gateway starts from USB; web admin accessible; config persisted on shared data partition.
- **US-13** — As an admin, I want the USB gateway to handle safe removal and recovery.
  - AC: **Hot-unplug detection** triggers graceful shutdown (stop receiver → flush → marker); recovery scan on next boot reconciles interrupted studies; LED status indicator (if hardware supports).

---

*End of PRD — mercure Gateway v1.0 draft. Feedback requested on: architecture (§3), roadmap (§9), and open questions (§13).*
