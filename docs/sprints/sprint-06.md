# Sprint 06 — Tauri Shell + Web Admin Panel + Windows Packaging (Weeks 11–12)

**Goal:** Ship the MVP envelope: Tauri desktop shell (tray + window) wrapping a FastAPI + React/Vue
SPA web admin panel, web-based guided first-run wizard (US-08), and a Windows installer that passes
the K6 size gate — reaching the PRD §9 Phase 1 exit.

**Exit criteria:** Non-technical user completes web-based setup ≤10 min with per-step connectivity
validation (US-08 AC); installer tested on a clean Windows VM; K6 gates pass; web admin panel
functional for queue/status/logs/config/reports.

**PRD refs:** §2.2 Flow A, §3.2 (desktop shell), §5.1 packaging, §9 Phase 1, §13 Q1/Q3, §14 US-08.
**Refinement refs:** product-refinement-spec.md §2.1 (Tauri+FastAPI architecture), §2.4 (config setup),
§7 (API surface), §2.5 (credentials).

**Per-sprint gate:** ADR-0002 (S01-T4) names Tauri + FastAPI + React/Vue SPA as the architecture.
S01-T7 validated FastAPI + SPA baseline. Q3 (tray vs service) defaults to tray app.

| ID | Task | PRD ref | RED → GREEN | DoD | Status |
|----|------|---------|-------------|-----|--------|
| S06-T1 | **Tauri shell skeleton:** Tauri app with native tray icon (idle/sending/error per Flow B/C) + main window hosting the webview; Tauri↔FastAPI communication wired per ADR-0002 | §3.2, §2.2, refinement §2.1 | `tests/test_tauri_integration.py` — tray state derives from spool/forwarder status (logic RED-tested); FastAPI responds to Tauri commands; webview loads SPA | Tauri shell runs against wired core; tray icon works | ☐ |
| S06-T2 | **FastAPI REST API:** full API surface for config, queue, reports, audit, system (per refinement §7); all endpoints RED-tested; OpenAPI docs auto-generated | refinement §7 | `tests/test_api.py` — every endpoint tested (GET/PUT/POST); pagination, filtering, error responses correct; OpenAPI schema valid | API contract stable; web UI has data source | ☐ |
| S06-T3 | **Web admin panel — queue view:** paginated study list (10k rows ≤500 ms gate) with per-destination status + retry/re-forward buttons; React/Vue SPA component | §5.6, US-04, refinement §7 | `tests/test_queue_view.py` — pagination service tests (page size, filters, latency budget); UI smoke | §5.6 queue gate green; web UI functional | ☐ |
| S06-T4 | **Web admin panel — config, logs, reports, audit tabs:** full admin interface with config editing, log viewer, report list/viewer, audit log viewer + chain verification | refinement §7 | service tests green; manual UI smoke on `docs/qa/` checklist | Operators have full visibility via web UI | ☐ |
| S06-T5 | **Web-based guided wizard:** step-by-step setup in the SPA — receiver settings (defaults 11112/`GATEWAY`), destinations, optional reports/hub reporting, connectivity validation per step, save to `mercure-gateway.json`; replaces the desktop wizard | §2.2 Flow A, US-08, refinement §2.4 | `tests/test_wizard.py` — wizard state machine (steps, back/next, validation gates); echo/validate service (`tests/test_echo.py`) against fake SCP | Wizard logic RED-tested; US-08 AC green | ☐ |
| S06-T6 | **Connectivity echo:** C-ECHO SCU to each destination, green/red per wizard step | Flow A-7 | `tests/test_echo.py` — echo OK/timeout/refused mapped to UI statuses | Per-step validation AC green | ☐ |
| S06-T7 | **Config import/export via USB:** export `mercure-gateway.json` from web UI (download); import via file upload or file drop on data partition; config version field for migration | refinement §2.4, usb-dongle-spec §8.2 | `tests/test_config_import_export.py` — export valid JSON; import validates and loads; version field checked | Air-gapped config deployment works | ☐ |
| S06-T8 | **Web UI auth (optional):** when `web_ui.auth_enabled: true`, require password on web UI access; bcrypt password hash in config; localhost-only by default (no auth needed) | refinement §7 | `tests/test_web_auth.py` — auth enabled → 401 without password; auth disabled → 200; localhost default works | Shared-machine security ready | ☐ |
| S06-T9 | **Windows packaging (no TDD):** Tauri build + PyInstaller for Python backend + Inno Setup installer; auto-start option; installer size gate (≤250 MB) in CI | §5.1, K6, §13 Q3 | CI job builds installer; size gate script fails build if >250 MB | Installer artifact on clean Windows VM | ☐ |
| S06-T10 | **Clean-VM UAT (no TDD):** scripted walkthrough — install, web wizard, first study forwarded, report pulled; time non-technical user run | K4, §9 Phase 1 exit | UAT checklist + timing recorded in `docs/qa/uat-06.md` | K4 (≤10 min) demonstrated; Phase 1 exit criteria checked | ☐ |
| S06-T11 | **MVP exit sweep:** run the README MVP exit checklist (US-01…US-08), file gaps, fix or descope explicitly | §9 Phase 1 | all checklist items ticked in `docs/sprints/README.md` | MVP formally closed | ☐ |

**Evidence:** _(links to commits/PRs when done)_

**Notes:**
- **Major refinement change:** This sprint replaces the original PySide6/Tauri desktop shell with a
  **Tauri shell + FastAPI backend + React/Vue SPA** web admin panel. The Tauri window hosts a
  webview loading `localhost:8080` (FastAPI serving the SPA). This gives us: native tray icon
  (Tauri), web-based config/management (FastAPI + SPA), and cross-platform compatibility.
- The web wizard (S06-T5) replaces the desktop wizard entirely. It runs in the browser at
  `localhost:8080/setup` — the same web UI that provides ongoing management.
- S06-T7 adds config import/export for USB deployment (air-gapped sites copy config via USB).
- S06-T8 adds optional web UI authentication for shared machines (not needed for localhost-only).
- UI logic (wizard state machine, tray state derivation, queue pagination service) is RED→GREEN;
  widgets themselves are smoke-tested — matching PRD §10's "prefer UI-level scripted flows" stance
  and the repo's logic-testing pattern.
- If the Tauri + FastAPI communication prototype (S01-T4) revealed issues, fall back to standalone
  FastAPI + browser (no Tauri shell) — the web UI still works, just without native tray.
- The FastAPI backend serves both the SPA static files and the REST API. No separate web server
  needed.
