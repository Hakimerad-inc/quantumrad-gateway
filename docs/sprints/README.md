# mercure Gateway — Sprint Plan

TDD-aligned delivery plan derived from [`mercure-gateway-PRD.md`](../../mercure-gateway-PRD.md)
(v1.0 draft), refined by [`product-refinement-spec.md`](../../product-refinement-spec.md) and
[`usb-dongle-gateway-spec.md`](../../usb-dongle-gateway-spec.md).

Scope: **Phase 0 + MVP (v1.0) + Phase 2 (v1.1) + USB Dongle Variant** — 10 two-week sprints,
~20 weeks (PRD §9 estimates: Phase 0 2–3 wk, Phase 1 8–10 wk, Phase 2 6–8 wk; +USB 2 wk).

## How we work

Every build task follows the repo's established RED→GREEN loop (see `tests/test_forwarder.py`
for the pattern):

1. **RED** — write the failing test first, in the named file, using the repo's conventions:
   pytest fixtures (function-scoped), hand-written fakes over `unittest.mock`, typed signatures
   (`-> None`), `test_<behavior>` naming, one test module per source module.
2. **GREEN** — the minimal implementation that passes, matching module conventions
   (`from __future__ import annotations`, explicit `__all__`, PRD §-refs in docstrings, strict mypy).
3. **Refactor** — clean up while tests stay green.

**Universal Definition of Done (every task):** `uv run pytest` green · `uv run mypy` (strict)
clean · `uv run ruff check` clean · docstrings reference PRD sections · status updated in this
file's sprint board and the sprint file.

**Per-sprint gate:** before writing the first RED test, re-verify what is already implemented vs
stubbed — scaffold degrades as implementation lands. Anything pre-existing and green is extension
work, not new work.

**Status legend:** `☐` not started · `🔄` in progress · `✅` done (link the commit or PR in the
sprint file's Evidence line).

Spike/decision/packaging tasks are marked **(no TDD)** — their RED/GREEN equivalent is the named
decision artifact, benchmark, or CI gate. Everything else is explicit RED→GREEN.

## Sprint board

| Sprint | Weeks | Theme | User stories | Status |
|--------|-------|-------|--------------|--------|
| [01](sprint-01.md) | 1–2 | Phase 0 — spikes, Orthanc rig, demo chain | (enablers, Q1/Q6/Q7) | 🔄 (T5/T8 blocked: hub team, USB hardware) |
| [02](sprint-02.md) | 3–4 | Receiver SCP + spool file storage | US-01, US-02 | ✅ (148 passed, mypy+ruff clean) |
| [03](sprint-03.md) | 5–6 | Forwarding engine completion + app wiring | US-03, US-04 | ✅ (185 passed, mypy+ruff clean) |
| [04](sprint-04.md) | 7–8 | Audit hardening + encrypted config + operator console v0 | US-07, K5 | ✅ (237 passed, mypy+ruff clean) |
| [05](sprint-05.md) | 9–10 | Reports MVP (DICOM SR + PDF via C-FIND/C-MOVE) | US-05, US-06 | ✅ (266 passed, mypy+ruff clean) |
| [06](sprint-06.md) | 11–12 | Tauri shell + web admin panel + Windows packaging | US-08, K4, K6 | 🔄 (T1–T6 ✅, T9/T10 artifact pending, T7/T8 done in S09) |
| [07](sprint-07.md) | 13–14 | v1.1 destinations + forwarding rules | US-09 | ✅ (401 passed, mypy+ruff clean) |
| [08](sprint-08.md) | 15–16 | Hub reporting + pluggable report transports + Linux | US-10, US-11 | ✅ (436 passed, mypy+ruff clean; commit `b338053`) |
| [09](sprint-09.md) | 17–18 | Hardening, perf/security gates, release candidate | all | 🔄 (T1/T2/T3/T4/T5/T6/T8 ✅ — 518 passed; T7 Windows VM, T9 USB hw pending) |
| [10](sprint-10.md) | 19–20 | USB dongle variant (dual-mode, hot-unplug, LED) | US-12, US-13 | 🔄 (T5,T7 ✅ — 534 passed; T1–T4/T6/T8–T11 usb hw / Windows VM / packaging pending) |

MVP = Sprints 01–06. Phase 2 (v1.1) = Sprints 07–09. USB Dongle = Sprint 10.

## Traceability

### PRD §2.3 feature list → sprints

**MVP (v1.0):**

| Feature | Sprint | Notes |
|---------|--------|-------|
| DICOM C-STORE SCP receiver (pynetdicom), all compressed syntaxes | 02 | All compressed: JPEG, JPEG 2000, JPEG-LS, RLE; selective decompression |
| Local encrypted SQLite spool, persist-before-forward | 02 (storage), 04 (encryption) | |
| Forwarding to DICOM target(s), multiple destinations, concurrent | 03 | Configurable concurrency limit (refinement) |
| Retry with exponential backoff + max attempts | 03 (core already green in `tests/test_forwarder.py`) | |
| Tauri + FastAPI + React/Vue SPA (web admin panel) | 06 | Replaces PySide6/Tauri tray app decision (refinement) |
| Local audit log (chained SHA-256 hash + rotating text log) | 04 | Full chain hash from day one (refinement) |
| Web-based setup wizard (in SPA) | 06 | Replaces desktop wizard; accessible via localhost:8080 |
| Report retrieval MVP: DICOM SR + Encapsulated PDF via C-FIND/C-MOVE | 05 | PDF support added (refinement) |
| Report viewer (SR render + embedded PDF viewer) | 05 | |
| Manual re-forward and retry of failed tasks | 03 | |
| Windows packaging (Tauri bundler + Inno Setup), auto-start | 06 | |
| Basic modality include/exclude routing rules | 03 | MVP routing: modality filter (refinement) |

**v1.1:**

| Feature | Sprint |
|---------|--------|
| Linux builds (AppImage/deb), cross-platform CI | 08 |
| SFTP/rsync/Folder/S3/XNAT/DICOMweb destinations | 07 |
| Advanced forwarding rules (modality/patient/accession/description) | 07 |
| Hub registration + event streaming to bookkeeper | 08 |
| Report transports: DICOMweb QIDO/WADO, HL7/FHIR (exp.), email-to-folder | 08 |
| Auto-update mechanism | 09 |
| Remote diagnostics bundle export | 09 |
| OS keyring integration (credential storage upgrade) | 07 |
| Windows service mode | 07 |

**USB Dongle Variant (Sprint 10):**

| Feature | Sprint |
|---------|--------|
| Dual-mode USB (Linux boot + Windows auto-launch) | 10 |
| Hot-unplug detection + graceful shutdown | 10 |
| USB-specific config (storage budget, retention, LED) | 10 |
| Recovery scan on USB boot | 10 |
| LED status indicator | 10 |
| Disk full management (auto-purge) | 10 |
| USB flashing documentation + script | 10 |

**Deliberately out of scope (PRD §2.5 non-goals / §4):** fleet management, edge anonymization,
scheduling engine, full study viewer, HL7 *integration*, mobile apps, cloud control plane, AI
features. v2.0 items are not scheduled. Standalone USB hardware (Raspberry Pi etc.) deferred to v1.1+.

### Acceptance criteria trace (PRD §14 + refinements)

| US | Story (abridged) | Sprints | Key tests |
|----|------------------|---------|-----------|
| US-01 | Accept DICOM from any modality; ≥25 assoc; persist before ack; all compressed syntaxes; `.tags` | 02 | `tests/test_receiver.py`, `tests/test_receiver_concurrency.py` |
| US-02 | Persisted before forwarding; partial-study recovery scan | 02 | `tests/test_storage.py`, `tests/test_recovery.py` |
| US-03 | Auto-forward to hub/PACS within 2 s; per-destination status; concurrent workers | 03 | `tests/test_forwarder.py`, `tests/test_dicom_scu_handler.py`, `tests/test_concurrent_forwarder.py` |
| US-04 | Retry backoff ≤ retry_max; manual re-forward; copy never auto-deleted | 03 | `tests/test_forwarder.py`, `tests/test_retention.py` |
| US-05 | Report lookup by Accession/Study UID; SR + PDF via C-FIND/C-MOVE; status transitions | 05 | `tests/test_report_find.py`, `tests/test_report_move.py`, `tests/test_report_pdf.py` |
| US-06 | On-demand "Request report" | 05 | `tests/test_reports_service.py` |
| US-07 | Tamper-evident audit (chained hash); redacted export; encryption default | 04 | `tests/test_audit*.py`, `tests/test_audit_export.py` |
| US-08 | Web-based guided wizard ≤10 min; connectivity validated per step | 06 | `tests/test_wizard.py`, `tests/test_echo.py` |
| US-09 | Advanced rules + rule tester | 07 | `tests/test_rules.py` |
| US-10 | Hub registration + event streaming; reporting failure never blocks forwarding | 08 | `tests/test_hub_events.py` |
| US-11 | Linux builds; cross-platform CI green | 08 | CI matrix + `packaging/linux/` |
| US-12 | USB dongle: dual-mode boot (Linux + Windows auto-launch); gateway starts from USB | 10 | `tests/test_usb_boot.py`, `tests/test_usb_autolaunch.py` |
| US-13 | USB dongle: hot-unplug safe; recovery scan; LED status; disk full management | 10 | `tests/test_hot_unplug.py`, `tests/test_usb_recovery.py`, `tests/test_disk_full.py` |

### KPI trace (PRD §1 + refinements)

| KPI | Target | Built in | Measured/verified in |
|-----|--------|----------|----------------------|
| K1 | ≥99.9% persisted before forwarding | 02 (store-before-ack) | 09 chaos suite (`tests/chaos/`) |
| K2 | ≥99% delivered within retry_max | 03 (retry/backoff + concurrent) | 09 chaos suite |
| K3 | ≥95% reports within 5-min SLA (SR + PDF) | 05 (+08 transports) | 09 perf gates |
| K4 | Guided setup ≤10 min (web wizard) | 06 wizard | 06 UAT checklist (`docs/qa/`) |
| K5 | 100% of events audited | 04 (event coverage + chain hash) | 04 `tests/test_audit_coverage.py` |
| K6 | Installer ≤250 MB; idle RAM ≤150 MB | 06 (installer) | 06 size gate, 09 perf gates |
| K7 (NEW) | Cold-start ≤5 s (web UI loads) | 06 (Tauri + FastAPI) | 09 perf gates |
| K8 (NEW) | Concurrent forwarding throughput | 03 (concurrent workers) | 09 perf gates |
| K9 (NEW) | USB boot → gateway ready ≤30 s (Linux) / ≤15 s (Windows) | 10 | 10 USB perf tests |
| K10 (NEW) | USB hot-unplug flush ≤10 s | 10 | 10 USB perf tests |

### Open questions (PRD §13 + refinements) → resolving sprint

| Q | Question | Resolved |
|---|----------|----------|
| Q1 | PySide6 vs Tauri shell | S01 spike → **Tauri + FastAPI + React/Vue SPA** (refinement spec) |
| Q2 | Report transport priority after DICOM SR | S05 (SR + PDF first), S08 (DICOMweb → HL7/FHIR exp.) |
| Q3 | Tray app vs Windows service | S06 (tray app); service mode = S07 backlog |
| Q4 | Edge anonymization | Not scheduled (v2 non-goal) |
| Q5 | Auto-update/code-signing vendor | S08 decision (ADR), S09 auto-update impl |
| Q6 | Licensing | S01 confirm (MIT, `LICENSE` already present) |
| Q7 | Bookkeeper API shape | S01 confirm with hub team; impl S08 |
| Q8 | Minimum OS versions | S01 document Win10/11 x64 assumption; QA matrix S09 |
| Q9 (NEW) | Tauri ↔ FastAPI communication method | S01 spike (prototype localhost HTTP, sidecar, command bridge) |
| Q10 (NEW) | React vs Vue for SPA | S01 spike (React baseline) |
| Q11 (NEW) | USB device type (flash drive vs custom embedded) | S10 MVP: standard USB 3.0 flash; custom board v1.1+ |
| Q12 (NEW) | Standalone hardware (Raspberry Pi) | Deferred to v1.1+ (not MVP) |

## Exit-criteria checklists

**MVP exit (PRD §9 Phase 1):**

- [ ] All MVP acceptance criteria green: US-01…US-08 (per-sprint files carry the checkboxes)
- [ ] Installer tested on a clean Windows VM (S06, evidence link in `sprint-06.md`)
- [ ] KPI gates: K1, K2, K5 unit/chaos-verified; K4 UAT; K6 size gate (S06/S09)
- [ ] K7 (cold-start ≤5 s) and K8 (concurrent forwarding) verified in S09
- [ ] Tauri + FastAPI + React/Vue SPA fully functional (S06)
- [ ] DICOM SR + PDF report retrieval working (S05)
- [ ] Docs: user guide + admin guide (S09-T6)

**Phase 2 exit (PRD §9 Phase 2):**

- [ ] Cross-platform CI green — Ubuntu + Windows matrix with Linux artifacts (S08)
- [ ] Rules feature acceptance tested (S07-T6)
- [ ] v1.1 AC green: US-09…US-11
- [ ] OS keyring integration working (S07)

**USB Dongle exit (Sprint 10):**

- [ ] Dual-mode USB boots on 3+ PC models (Linux mode)
- [ ] Windows auto-launch works on Windows 10/11
- [ ] Hot-unplug detection + graceful shutdown in both modes
- [ ] Recovery scan handles interrupted studies
- [ ] USB storage budget enforced (disk full management)
- [ ] K9 (boot → ready) and K10 (hot-unplug flush) performance gates met
- [ ] USB flashing script + documentation complete

## Repository baseline (start of Sprint 01)

Single scaffold commit; green suite of 31 tests (`test_config`, `test_audit`, `test_spool`,
`test_forwarder`). Implemented: config models (all 8 destination types), spool DB + state machine,
audit chain, forwarder dispatch loop (**uncommitted** — S01-T1 commits it). Stubs: receiver
transport, report retrieval, DICOM file storage, UI shell, at-rest encryption.

## Refined spec references

| Spec | File | Key decisions |
|------|------|---------------|
| Product refinement | `product-refinement-spec.md` | Tauri+FastAPI+SPA, SR+PDF reports, concurrent forwarding, modality routing, encrypted config |
| USB dongle variant | `usb-dongle-gateway-spec.md` | Dual-mode USB, hot-unplug, LED, 3-partition layout, standalone deferred |
