# mercure Gateway — Product Brief

**Version:** 0.1.0-dev (Sprint 09/10 in progress; targeting v1.1-RC)
**Branch:** `docs/sprint-plan`
**License:** MIT
**Last Updated:** 2026-09-07

---

## 1. Elevator Pitch

A **lightweight desktop DICOM gateway** that runs on a standard Windows/Linux PC in small clinics and imaging sites. It receives DICOM studies from modalities (CT, MR, US, etc.), stores them locally with encryption, forwards them reliably to a central **mercure hub** or **vendor PACS**, and retrieves study reports (DICOM SR + PDF) back from the PACS — all with a web-based admin UI, zero server infrastructure, and guided setup in under 10 minutes.

---

## 2. Problem & Market

| Problem | Solution |
|---------|----------|
| Small clinics lack IT staff for server-side DICOM stacks | Runs on a desktop PC; no server hardware |
| Vendor PACS / cloud hubs need reliable edge ingestion | Store-and-forward with encrypted local spool + retry/backoff |
| Report retrieval is manual or missing | Automated C-FIND/C-MOVE (SR + PDF) + pluggable transports (DICOMweb, FHIR) |
| Compliance requires tamper-evident audit trails | Chained SHA-256 audit log + optional hub streaming |
| Deployment must be simple | Web-based setup wizard, Windows installer + Linux AppImage/deb, auto-update |

**Target users:** Small clinics, private practices, single imaging sites, teleradiology spokes, USB dongle deployments.

---

## 3. Core Value Proposition

| Capability | Description |
|------------|-------------|
| **DICOM C-STORE SCP** | Accepts all transfer syntaxes (JPEG, JPEG 2000, JPEG-LS, RLE); selective decompression |
| **Encrypted Local Spool** | SQLite (SQLCipher) + filesystem; persist-before-ack; recovery scan on startup |
| **Multi-Destination Forwarding** | Concurrent workers; DICOM, DICOM-TLS, DICOMweb, SFTP, rsync, Folder, S3, XNAT |
| **Smart Routing Rules** | Modality include/exclude (MVP); advanced by patient/accession/series (v1.1) |
| **Report Retrieval** | DICOM SR + Encapsulated PDF via C-FIND/C-MOVE; DICOMweb QIDO/WADO (v1.1); HL7/FHIR experimental |
| **Web Admin Panel** | React + Vite + TypeScript SPA served by FastAPI; localhost:8080; tray/desktop via Tauri v2 |
| **Audit & Compliance** | Chained SHA-256 hash log; redacted export; encrypted config; HIPAA-aware design |
| **Hub Integration** | Auto-registration + event streaming to mercure bookkeeper API (isolation invariant) |
| **Cross-Platform** | Windows (MVP, Inno Setup), Linux AppImage/deb (v1.1), USB dongle variant (Sprint 10) |

---

## 4. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Desktop PC (Windows/Linux)                          │
│  ┌─────────┐    ┌─────────────────────┐    ┌────────────────────────────┐  │
│  │Modalities│───▶│  mercure-gateway    │───▶│  Central mercure Hub       │  │
│  │(C-STORE)│    │                     │    │  (or Vendor PACS)          │  │
│  └─────────┘    │  Receiver (SCP)     │    │  ┌──────────────────────┐  │  │
│                 │  → Local Spool      │───▶│  │ Receiver/Router/     │  │  │
│                 │  (SQLite + Files)   │    │  │ Processor/Dispatcher │  │  │
│                 │         │           │    │  └──────────────────────┘  │  │
│                 │         ▼           │    └────────────────────────────┘  │
│                 │  Forwarding Engine  │    ┌────────────────────────────┐  │
│                 │  (Concurrent,       │───▶│  Vendor PACS (Direct)      │  │
│                 │   Retry/Backoff)    │    └────────────────────────────┘  │
│                 │         │           │    ┌────────────────────────────┐  │
│                 │         ▼           │◀───│  Report Source (PACS)      │  │
│                 │  Report Retrieval   │    │  C-FIND/C-MOVE, DICOMweb   │  │
│                 │  (SR + PDF)         │    └────────────────────────────┘  │
│                 │         │           │                                     │
│                 │         ▼           │                                     │
│                 │  Audit Log (Chain)  │                                     │
│                 │  + Hub Streaming    │                                     │
│                 └─────────────────────┘                                     │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Tech Stack:**
- **Core:** Python 3.12+, `pynetdicom`, `pydicom`, `SQLCipher`, `FastAPI`, `uv`
- **Frontend:** React 19 + TypeScript + Vite + Tailwind/Shadcn UI
- **Desktop Shell:** Tauri v2 (Rust) — replaces earlier PySide6 approach
- **CI/CD:** GitHub Actions matrix (Ubuntu + Windows), K6 perf gates, pip-audit, coverage ≥80%
- **Packaging:** Tauri bundler (NSIS/Inno on Windows, AppImage/deb on Linux)

---

## 5. Sprint Progress (10 Sprint Plan)

| Sprint | Weeks | Theme | Status | Key Deliverables |
|--------|-------|-------|--------|------------------|
| 01 | 1–2 | Spikes, Orthanc rig, demo chain | 🔄 (T5/T8 blocked) | Architecture decisions (Tauri+SPA) |
| 02 | 3–4 | Receiver SCP + spool storage | ✅ | C-STORE SCP, all syntaxes, encrypted spool |
| 03 | 5–6 | Forwarding engine + app wiring | ✅ | Multi-dest, concurrent, retry/backoff |
| 04 | 7–8 | Audit hardening + encrypted config | ✅ | Chained audit, encrypted config, operator console |
| 05 | 9–10 | Reports MVP (SR + PDF) | ✅ | C-FIND/C-MOVE retrieval, SR render, PDF viewer |
| 06 | 11–12 | Tauri + Web Admin + Windows pkg | 🔄 (T9/T10 artifact pending) | SPA wizard, tray app, Windows installer |
| 07 | 13–14 | v1.1 destinations + rules | ✅ | 8 dest types, advanced rules, OS keyring |
| 08 | 15–16 | Hub reporting + Linux | ✅ | Hub client/events, DICOMweb transport, Linux CI |
| 09 | 17–18 | Hardening, gates, RC | 🔄 (6/9 done) | Security gates, perf gates, diagnostics, auto-update, docs |
| 10 | 19–20 | USB dongle variant | ☐ | Dual-mode boot, hot-unplug, LED, recovery |

**MVP = Sprints 01–06** | **Phase 2 (v1.1) = Sprints 07–09** | **USB Dongle = Sprint 10**

---

## 6. Current State (Sprint 09 — 6/9 tasks complete)

**Completed this sprint:**
- ✅ Security gates: pip-audit CI job + 9 TLS/AE/secrets/audit tests
- ✅ Web hardening: CSP/HSTS/XFO/nosniff headers + origin-based CSRF (7 tests)
- ✅ Diagnostics bundle: `GET /api/diagnostics/export` (5 tests)
- ✅ Auto-update: check/verify/apply/rollback with Ed25519 sig (10 tests)
- ✅ Perf gates: latency ≤2s, K8 throughput ≥5/s + CI job (3 tests)
- ✅ Documentation: user guide, admin guide, USB quick-start

**Blocked (infrastructure):**
- ⏳ Chaos suite (S09-T1) — needs Docker Orthanc rig
- ⏳ RC sweep + `v1.1.0-rc1` tag (S09-T7) — needs Windows VM
- ⏳ USB perf baseline (S09-T9) — needs USB hardware

**Test Health:** 470+ tests passing, mypy strict clean, ruff clean, 84% branch coverage

---

## 7. Key Differentiators

| Dimension | mercure Gateway | Typical Alternatives |
|-----------|-----------------|---------------------|
| **Deployment** | Desktop app (Windows/Linux) | Server VMs, Docker, cloud |
| **Setup Time** | ≤10 min guided web wizard | Hours–days IT effort |
| **Offline Resilience** | Encrypted local spool + retry | Often requires constant connectivity |
| **Report Retrieval** | Built-in SR + PDF + pluggable transports | Manual or vendor-specific |
| **Audit/Compliance** | Chained hash + HIPAA-aware | Often absent or bolted on |
| **Hub Integration** | Native mercure bookkeeper streaming | Custom integration required |
| **USB Variant** | Bootable dongle for zero-install sites | Not available |

---

## 8. Compliance & Security

- **HIPAA-aligned:** BAA-ready design, minimum necessary access, audit logging, encryption at rest
- **Audit Trail:** Chained SHA-256 hash per event; tamper-evident; redacted export for support
- **Encryption:** SQLCipher spool, encrypted config file (master password), OS keyring (v1.1)
- **Network:** TLS for all external connections; `verify` control per destination
- **Supply Chain:** pip-audit in CI; pinned dependencies via `uv.lock`
- **Auto-Update:** Ed25519-signed artifacts; SHA-256 verification; rollback on failure

---

## 9. Roadmap Highlights

| Horizon | Focus |
|---------|-------|
| **v1.0 MVP (S06 exit)** | Windows installer, web wizard, DICOM forward + SR/PDF reports |
| **v1.1 RC (S09 exit)** | Linux builds, 8 destination types, hub reporting, auto-update, perf/security gates |
| **v1.1 GA** | Windows service mode, OS keyring, chaos-validated reliability |
| **Sprint 10 (USB)** | Dual-mode bootable dongle, hot-unplug safe, LED status, disk-full management |
| **v2.0+** | Fleet management, anonymization, scheduling, full study viewer |

---

## 10. Repository Structure

```
mercure-gateway/
├── src/mercure_gateway/        # Python core (FastAPI, pynetdicom, spool, audit, hub, reports)
│   ├── main.py                 # App entry, wiring, lifecycle
│   ├── config.py               # Pydantic models (all 8 dest types)
│   ├── receiver/               # C-STORE SCP
│   ├── forwarder/              # Multi-dest, concurrent, retry
│   ├── spool/                  # SQLite + file storage, state machine
│   ├── audit/                  # Chained hash log + export
│   ├── hub/                    # Registration + event streaming
│   ├── reports/                # Retrieval (DICOM, DICOMweb, HL7/FHIR)
│   ├── rules/                  # Routing engine
│   └── web/                    # FastAPI routes + static SPA assets
├── web/                        # React + Vite + TypeScript SPA
│   ├── src/pages/              # Dashboard, Queue, Reports, Config, Audit, Logs, Pipeline
│   └── src/ui/                 # Flow diagrams, icons, shared components
├── src-tauri/                  # Tauri v2 Rust desktop shell
├── tests/                      # 470+ pytest tests (unit + integration)
├── docs/                       # PRD, sprint plans, ADRs, guides, UAT checklists
├── .github/workflows/ci.yml    # Quality, test×2 OS, build-spa, package-win/linux, perf, coverage
└── pyproject.toml              # uv project, deps, tool config (mypy strict, ruff, pytest)
```

---

## 11. Quick Links

| Artifact | Path |
|----------|------|
| Full PRD | `mercure-gateway-PRD.md` |
| Sprint Plan | `docs/sprints/README.md` |
| Sprint 09 Detail | `docs/sprints/sprint-09.md` |
| ADRs | `docs/adr/ADR-0001` … `ADR-0006` |
| User Guide | `docs/guides/user-guide.md` |
| Admin Guide | `docs/guides/admin-guide.md` |
| USB Quick-Start | `docs/guides/usb-quickstart.md` |
| Product Refinement | `product-refinement-spec.md` |
| USB Dongle Spec | `usb-dongle-gateway-spec.md` |

---

## 12. Contact / Ownership

**Team:** mercure imaging
**Repo:** `dicom-gateway` (private)
**Primary Branch:** `docs/sprint-plan`
**CI:** GitHub Actions (Ubuntu + Windows matrix)
