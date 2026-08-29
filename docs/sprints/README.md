# mercure Gateway — Sprint Plan

TDD-aligned delivery plan derived from [`mercure-gateway-PRD.md`](../../mercure-gateway-PRD.md)
(v1.0 draft). Scope: **Phase 0 + MVP (v1.0) + Phase 2 (v1.1)** — 9 two-week sprints, ~18 weeks
(PRD §9 estimates: Phase 0 2–3 wk, Phase 1 8–10 wk, Phase 2 6–8 wk).

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
| [01](sprint-01.md) | 1–2 | Phase 0 — spikes, Orthanc rig, demo chain | (enablers, Q1/Q6/Q7) | ☐ |
| [02](sprint-02.md) | 3–4 | Receiver SCP + spool file storage | US-01, US-02 | ☐ |
| [03](sprint-03.md) | 5–6 | Forwarding engine completion + app wiring | US-03, US-04 | ☐ |
| [04](sprint-04.md) | 7–8 | Audit hardening + encryption + operator console v0 | US-07, K5 | ☐ |
| [05](sprint-05.md) | 9–10 | Reports MVP (C-FIND/C-MOVE SR) | US-05, US-06 | ☐ |
| [06](sprint-06.md) | 11–12 | Desktop shell + wizard + Windows packaging | US-08, K4, K6 | ☐ |
| [07](sprint-07.md) | 13–14 | v1.1 destinations + forwarding rules | US-09 | ☐ |
| [08](sprint-08.md) | 15–16 | Hub reporting + pluggable report transports + Linux | US-10, US-11 | ☐ |
| [09](sprint-09.md) | 17–18 | Hardening, perf/security gates, release candidate | all | ☐ |

MVP = Sprints 01–06. Phase 2 (v1.1) = Sprints 07–09.

## Traceability

### PRD §2.3 feature list → sprints

**MVP (v1.0):**

| Feature | Sprint |
|---------|--------|
| DICOM C-STORE SCP receiver (pynetdicom), compressed syntax | 02 |
| Local encrypted SQLite spool, persist-before-forward | 02 (storage), 04 (encryption) |
| Forwarding to DICOM target(s), multiple destinations | 03 |
| Retry with exponential backoff + max attempts | 03 (core already green in `tests/test_forwarder.py`) |
| Tray icon + main window (queue/status/logs/settings) | 06 (queue/status earlier in 04 console) |
| Local audit log (encrypted SQLite + rotating text log) | 04 |
| Guided first-run wizard | 06 |
| Report retrieval MVP: DICOM SR via C-FIND/C-MOVE | 05 |
| Report viewer (SR render + text/PDF) | 05 |
| Manual re-forward and retry of failed tasks | 03 |
| Windows packaging (PyInstaller + Inno Setup), auto-start | 06 |

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

**Deliberately out of scope (PRD §2.5 non-goals / §4):** fleet management, edge anonymization,
scheduling engine, full study viewer, HL7 *integration*, mobile apps, cloud control plane, AI
features. v2.0 items are not scheduled.

### Acceptance criteria trace (PRD §14)

| US | Story (abridged) | Sprints | Key tests |
|----|------------------|---------|-----------|
| US-01 | Accept DICOM from any modality; ≥25 assoc; persist before ack; compressed; `.tags` | 02 | `tests/test_receiver.py`, `tests/test_receiver_concurrency.py` |
| US-02 | Persisted before forwarding; partial-study recovery scan | 02 | `tests/test_storage.py`, `tests/test_recovery.py` |
| US-03 | Auto-forward to hub/PACS within 2 s; per-destination status | 03 | `tests/test_forwarder.py`, `tests/test_dicom_scu_handler.py` |
| US-04 | Retry backoff ≤ retry_max; manual re-forward; copy never auto-deleted | 03 | `tests/test_forwarder.py`, `tests/test_retention.py` |
| US-05 | Report lookup by Accession/Study UID; SR via C-FIND/C-MOVE; status transitions | 05 | `tests/test_report_find.py`, `tests/test_report_move.py` |
| US-06 | On-demand "Request report" | 05 | `tests/test_reports_service.py` |
| US-07 | Tamper-evident audit; redacted export; encryption default | 04 | `tests/test_audit*.py` (chain exists), `tests/test_audit_export.py` |
| US-08 | Guided wizard ≤10 min; connectivity validated per step | 06 | `tests/test_wizard.py`, `tests/test_echo.py` |
| US-09 | Advanced rules + rule tester | 07 | `tests/test_rules.py` |
| US-10 | Hub registration + event streaming; reporting failure never blocks forwarding | 08 | `tests/test_hub_events.py` |
| US-11 | Linux builds; cross-platform CI green | 08 | CI matrix + `packaging/linux/` |

### KPI trace (PRD §1)

| KPI | Target | Built in | Measured/verified in |
|-----|--------|----------|----------------------|
| K1 | ≥99.9% persisted before forwarding | 02 (store-before-ack) | 09 chaos suite (`tests/chaos/`) |
| K2 | ≥99% delivered within retry_max | 03 (retry/backoff) | 09 chaos suite |
| K3 | ≥95% reports within 5-min SLA | 05 (+08 transports) | 09 perf gates |
| K4 | Guided setup ≤10 min | 06 wizard | 06 UAT checklist (`docs/qa/`) |
| K5 | 100% of events audited | 04 (event coverage) | 04 `tests/test_audit_coverage.py` |
| K6 | Installer ≤250 MB; idle RAM ≤150 MB | 06 (installer) | 06 size gate, 09 perf gates |

### Open questions (PRD §13) → resolving sprint

| Q | Question | Resolved |
|---|----------|----------|
| Q1 | PySide6 vs Tauri shell | S01 spike (ADR-0002), implemented S06 |
| Q2 | Report transport priority after DICOM SR | S05 (SR first), S08 (DICOMweb → HL7/FHIR exp.) |
| Q3 | Tray app vs Windows service | S06 (tray app per PRD default); service mode = backlog |
| Q4 | Edge anonymization | Not scheduled (v2 non-goal) |
| Q5 | Auto-update/code-signing vendor | S08 decision (ADR), S09 auto-update impl |
| Q6 | Licensing | S01 confirm (MIT, `LICENSE` already present) |
| Q7 | Bookkeeper API shape | S01 confirm with hub team; impl S08 |
| Q8 | Minimum OS versions | S01 document Win10/11 x64 assumption; QA matrix S09 |

## Exit-criteria checklists

**MVP exit (PRD §9 Phase 1):**

- [ ] All MVP acceptance criteria green: US-01…US-08 (per-sprint files carry the checkboxes)
- [ ] Installer tested on a clean Windows VM (S06, evidence link in `sprint-06.md`)
- [ ] KPI gates: K1, K2, K5 unit/chaos-verified; K4 UAT; K6 size gate (S06/S09)
- [ ] Docs: user guide + admin guide (S09-T6)

**Phase 2 exit (PRD §9 Phase 2):**

- [ ] Cross-platform CI green — Ubuntu + Windows matrix with Linux artifacts (S08)
- [ ] Rules feature acceptance tested (S07-T6)
- [ ] v1.1 AC green: US-09…US-11

## Repository baseline (start of Sprint 01)

Single scaffold commit; green suite of 31 tests (`test_config`, `test_audit`, `test_spool`,
`test_forwarder`). Implemented: config models (all 8 destination types), spool DB + state machine,
audit chain, forwarder dispatch loop (**uncommitted** — S01-T1 commits it). Stubs: receiver
transport, report retrieval, DICOM file storage, UI shell, at-rest encryption.
