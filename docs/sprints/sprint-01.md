# Sprint 01 — Phase 0: Discovery, Spikes & Test Rig (Weeks 1–2)

**Goal:** Resolve the PRD's Phase 0 unknowns (PRD §9) with measurable spikes, stand up the Orthanc
test rig, and commit the outstanding forwarder work — exiting with the PRD's 15-minute demo
chain: modality → gateway → mercure hub, report pulled from a test PACS.

**Exit criteria (PRD §9 Phase 0):** working 15-min demo: modality → gateway → mercure hub; report
pulled back from test PACS (Orthanc).

**PRD refs:** §9 Phase 0, §13 (Q1, Q6, Q7), §10 (integration strategy).

| ID | Task | PRD ref | RED → GREEN | DoD | Status |
|----|------|---------|-------------|-----|--------|
| S01-T1 | **Housekeeping:** commit the uncommitted forwarder dispatch loop + `tests/test_forwarder.py` as one logical change; fix the stale "skeleton" module docstring | — | n/a — suite already green | Working tree clean; CI green on `main` | ☐ |
| S01-T2 | **Orthanc test rig (no TDD):** docker-compose with Orthanc (test PACS), a fake modality SCU script, and a `make`-style task runner; document in `docs/dev/test-rig.md` | §10 | rig runs `docker compose up` and demo script passes | Any dev can run the full demo chain from README | ☐ |
| S01-T3 | **Receiver spike (no TDD):** pynetdicom C-STORE SCP vs DCMTK `storescp` — throughput (MB/s), concurrency (25 assoc), RAM; write ADR-0001 | §9, §11 | benchmark numbers recorded in ADR-0001 | Decision recorded with data; abstraction interface sketched | ☐ |
| S01-T4 | **Shell spike (no TDD):** PySide6 vs Tauri with Python core — startup time, idle RAM vs K6 (≤150 MB), tray support; write ADR-0002 | §13 Q1 | benchmark + hello-world tray in both, recorded | ADR-0002 names the S06 shell | ☐ |
| S01-T5 | **(no TDD) Confirm Q6 (MIT license), Q7 (bookkeeper API shape) with hub team; record answers in PRD §13 table** | §13 | — | PRD updated; notes in ADR-0003 (decisions log) | ☐ |
| S01-T6 | **Demo chain (no TDD):** scripted 15-min demo — send study from fake modality → spool → forward to Orthanc-as-hub; C-FIND report pull from Orthanc using existing `ReportRetriever.request_report` + a throwaway SCU | §9 | demo script runs end-to-end unattended | Demo script committed under `demo/`; exit criteria met | ☐ |

**Evidence:** _(links to commits/PRs when done)_

**Notes:**
- ADR-0001's receiver abstraction interface becomes the RED contract for Sprint 02-T1 — write the
  spike's interface sketch so it can drop into `src/mercure_gateway/receiver/`.
- If the pynetdicom spike fails the 30 MB/s gate (PRD §5.6), ADR-0001 must name the DCMTK fallback
  and Sprint 02 tasks swap transport accordingly (plan is transport-agnostic by design).
