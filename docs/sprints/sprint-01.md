# Sprint 01 — Phase 0: Discovery, Spikes & Test Rig (Weeks 1–2)

**Goal:** Resolve the PRD's Phase 0 unknowns (PRD §9) with measurable spikes, stand up the Orthanc
test rig, commit the outstanding forwarder work, and prototype the Tauri + FastAPI + React/Vue SPA
architecture — exiting with the PRD's 15-minute demo chain: modality → gateway → mercure hub,
report pulled from a test PACS (Orthanc).

**Exit criteria (PRD §9 Phase 0):** working 15-min demo: modality → gateway → mercure hub; report
pulled back from test PACS (Orthanc).

**PRD refs:** §9 Phase 0, §13 (Q1, Q6, Q7), §10 (integration strategy).
**Refinement refs:** product-refinement-spec.md §2.1 (Tauri+FastAPI architecture), §2.5 (credentials).

**Per-sprint gate:** `forwarder/__init__.py` (dispatch loop, retry, `DestinationHandler` protocol)
and its 5 tests exist — this sprint commits them (S01-T1).

| ID | Task | PRD ref | RED → GREEN | DoD | Status |
|----|------|---------|-------------|-----|--------|
| S01-T1 | **Housekeeping:** commit the uncommitted forwarder dispatch loop + `tests/test_forwarder.py` as one logical change; fix the stale "skeleton" module docstring | — | n/a — suite already green | Working tree clean; CI green on `main` | ☐ |
| S01-T2 | **Orthanc test rig (no TDD):** docker-compose with Orthanc (test PACS), a fake modality SCU script, and a `make`-style task runner; document in `docs/dev/test-rig.md` | §10 | rig runs `docker compose up` and demo script passes | Any dev can run the full demo chain from README | ☐ |
| S01-T3 | **Receiver spike (no TDD):** pynetdicom C-STORE SCP vs DCMTK `storescp` — throughput (MB/s), concurrency (25 assoc), RAM; write ADR-0001 | §9, §11 | benchmark numbers recorded in ADR-0001 | Decision recorded with data; abstraction interface sketched | ☐ |
| S01-T4 | **Tauri + FastAPI + SPA spike (no TDD):** prototype Tauri shell wrapping a FastAPI backend serving a React/Vue SPA on localhost; measure: cold-start time, idle RAM vs K6 (≤150 MB), tray support, Tauri↔FastAPI communication options (localhost HTTP, sidecar, command bridge); write ADR-0002 | §13 Q1, refinement §2.1, Q9/Q10 | benchmark numbers recorded; hello-world tray + SPA in Tauri; communication method chosen | ADR-0002 names the architecture and communication method | ☐ |
| S01-T5 | **(no TDD) Confirm Q6 (MIT license), Q7 (bookkeeper API shape) with hub team; record answers in PRD §13 table** | §13 | — | PRD updated; notes in ADR-0003 (decisions log) | ☐ |
| S01-T6 | **Demo chain (no TDD):** scripted 15-min demo — send study from fake modality → spool → forward to Orthanc-as-hub; C-FIND report pull from Orthanc using existing `ReportRetriever.request_report` + a throwaway SCU | §9 | demo script runs end-to-end unattended | Demo script committed under `demo/`; exit criteria met | ☐ |
| S01-T7 | **FastAPI + SPA prototype (no TDD):** boot FastAPI on localhost; serve a minimal React/Vue SPA; measure cold-start time and memory footprint; validate REST API surface for config/queue/reports/audit | refinement §7 | SPA loads in browser; REST endpoints respond; cold-start + RAM measured | FastAPI+SPA baseline numbers recorded; API contract drafted | ☐ |
| S01-T8 | **USB dongle prototype (no TDD):** test auto-launch on Windows (autorun.inf or scheduled task); test Linux boot from USB on 3 PC models (UEFI + legacy BIOS); measure boot → gateway ready time vs K9 (≤30 s Linux, ≤15 s Windows) | usb-dongle-spec §4, §10 | USB boots in both modes on test hardware; auto-launch works on Windows 10/11 | K9 baseline recorded; boot compatibility matrix documented | ☐ |

**Evidence:** _(links to commits/PRs when done)_

**Notes:**
- S01-T1 commits the uncommitted forwarder work — the 5 green tests in `tests/test_forwarder.py`
  and the dispatch loop in `forwarder/__init__.py`.
- S01-T4 replaces the original PySide6 vs Tauri shell spike with the refined architecture decision:
  **Tauri + FastAPI + React/Vue SPA** (per product-refinement-spec.md §2.1). The spike validates
  this choice with real benchmarks.
- S01-T7 is new — validates the FastAPI backend + SPA frontend before Sprint 06 builds the full
  web admin panel.
- S01-T8 is new — validates USB boot compatibility before Sprint 10 builds the full USB variant.
- ADR-0001's receiver abstraction interface becomes the RED contract for Sprint 02-T1 — write the
  spike's interface sketch so it can drop into `src/mercure_gateway/receiver/`.
- If the pynetdicom spike fails the 30 MB/s gate (PRD §5.6), ADR-0001 must name the DCMTK fallback
  and Sprint 02 tasks swap transport accordingly (plan is transport-agnostic by design).
- If Tauri + FastAPI communication prototype (S01-T4) reveals unacceptable latency, fall back to
  standalone FastAPI + browser (no Tauri shell) — the web UI still works, just without native tray.
