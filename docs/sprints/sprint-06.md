# Sprint 06 — Desktop Shell, Wizard & Windows Packaging (Weeks 11–12)

**Goal:** Ship the MVP envelope: desktop shell (tray + main window), guided first-run wizard
(US-08), and a Windows installer that passes the K6 size gate — reaching the PRD §9 Phase 1 exit.

**Exit criteria:** Non-technical user completes setup ≤10 min with per-step connectivity validation
(US-08 AC); installer tested on a clean Windows VM; K6 gates pass.

**PRD refs:** §2.2 Flow A, §3.2 (desktop shell), §5.1 packaging, §9 Phase 1, §13 Q1/Q3, §14 US-08.

**Per-sprint gate:** ADR-0002 (S01-T4) names the shell — tasks below assume the winner; swap UI
framework details if the spike surprised us. Q3 (tray vs service) defaults to tray app.

| ID | Task | PRD ref | RED → GREEN | DoD | Status |
|----|------|---------|-------------|-----|--------|
| S06-T1 | **Shell skeleton:** tray icon (idle/sending/error per Flow B/C) + main window shell (queue/status/logs/settings/reports tabs) completing `ui/__init__.py` | §3.2, §2.2 | `tests/test_ui_state.py` — tray state derives from spool/forwarder status (logic RED-tested; visual shell smoke-tested) | Shell runs against wired core | ☐ |
| S06-T2 | **Queue view:** paginated study list (10k rows ≤500 ms gate) with per-destination status + retry/re-forward buttons calling the S03 service | §5.6, US-04 | `tests/test_queue_view.py` — pagination service tests (page size, filters, latency budget); UI smoke | §5.6 queue gate green | ☐ |
| S06-T3 | **Guided wizard:** role choice → receiver settings (defaults 11112/`GATEWAY`) → destinations → optional reports/hub reporting → connectivity validation per step → save | §2.2 Flow A, US-08 | `tests/test_wizard.py` — wizard state machine (steps, back/next, validation gates); echo/validate service (`tests/test_echo.py`) against fake SCP | Wizard logic RED-tested | ☐ |
| S06-T4 | **Connectivity echo:** C-ECHO SCU to each destination, green/red per wizard step | Flow A-7 | `tests/test_echo.py` — echo OK/timeout/refused mapped to UI statuses | Per-step validation AC green | ☐ |
| S06-T5 | **Windows packaging (no TDD):** PyInstaller spec + Inno Setup installer; auto-start option; installer size gate (≤250 MB) in CI | §5.1, K6, §13 Q3 | CI job builds installer; size gate script fails build if >250 MB | Installer artifact on clean Windows VM | ☐ |
| S06-T6 | **Clean-VM UAT (no TDD):** scripted walkthrough — install, wizard, first study forwarded, report pulled; time non-technical user run | K4, §9 Phase 1 exit | UAT checklist + timing recorded in `docs/qa/uat-06.md` | K4 (≤10 min) demonstrated; Phase 1 exit criteria checked | ☐ |
| S06-T7 | **MVP exit sweep:** run the README MVP exit checklist (US-01…US-08), file gaps, fix or descope explicitly | §9 Phase 1 | all checklist items ticked in `docs/sprints/README.md` | MVP formally closed | ☐ |

**Evidence:** _(links to commits/PRs when done)_

**Notes:**
- UI logic (wizard state machine, tray state derivation, queue pagination service) is RED→GREEN;
  widgets themselves are smoke-tested — matching PRD §10's "prefer UI-level scripted flows" stance
  and the repo's logic-testing pattern.
- If ADR-0002 picked Tauri, T1/T3 build against the Python core over a local IPC surface; the
  service tests are unchanged.
