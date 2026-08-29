# Sprint 03 — Forwarding Engine Completion + App Wiring (Weeks 5–6)

**Goal:** Turn the (uncommitted, green) forwarder dispatch loop into the real outbound pipeline: a
DICOM SCU destination handler, app wiring in `main.py`, manual re-forward, and fail-safe retention
— completing US-03 and US-04.

**Exit criteria:** Configured destinations reachable → studies sent within 2 s of completion;
per-destination status tracked (US-03); retry schedule honored, FAILED tasks manually retryable,
local copy never auto-deleted (US-04).

**PRD refs:** §5.2 steps 3–4, §3.3, §3.4-5, §14 US-03/US-04.

**Per-sprint gate:** `forwarder/__init__.py` (dispatch loop, retry, `DestinationHandler` protocol)
and its 5 tests land in S01-T1 — this sprint **extends** them.

| ID | Task | PRD ref | RED → GREEN | DoD | Status |
|----|------|---------|-------------|-----|--------|
| S03-T1 | **DICOM C-STORE SCU handler:** `DicomScuHandler` implementing `DestinationHandler` (pynetdicom SCU per ADR-0001; plan DCMTK `dcmsend` fallback); sends all study files, honors AET config | §2.3, §5.2-3 | `tests/test_dicom_scu_handler.py` — sends to fake local SCP; wrong AET/port → error result; multi-file study all delivered | Real handler registered for `dicom` targets | ☐ |
| S03-T2 | **Forwarder↔spool event audit:** emit `FORWARD_START`/`FORWARD_COMPLETE`/`FORWARD_ERROR` to `AuditLog` on every transition | §5.2-6, §5.4 | `tests/test_forwarder.py` additions — audit rows exist per transition, chained hash intact | K5 coverage for forwarding path | ☐ |
| S03-T3 | **App wiring in `main.py`:** config-driven lifecycle — receiver → spool → forwarder (with DICOM handler) → reports hooks; graceful shutdown; replaces the smoke-demo start/stop | §5.2 | `tests/test_main.py` — `main()` with test config runs one full cycle against in-process fakes and exits 0 | Single process runs the whole loop | ☐ |
| S03-T4 | **Manual re-forward / retry:** public re-forward service (used by UI later) re-enqueues FAILED studies; audit `RETRY_MANUAL` event | US-04, §2.2 Flow C | `tests/test_forwarder.py` additions — FAILED task → re-forward → SENDING → SENT; attempt counter reset semantics documented | FAILED tasks retryable programmatically | ☐ |
| S03-T5 | **Retention & cleanup:** only *delivered* studies eligible after `retention_delivered_days`; undelivered never auto-deleted; spool-size guard warns | §3.4-5, §5.5 | `tests/test_retention.py` — delivered+expired purged; FAILED untouched; max-spool guard emits warning event | US-04 "never auto-delete" AC green | ☐ |
| S03-T6 | **(no TDD) Load soak on test rig:** Orthanc destination, 100-study run on the S01 rig; measure forwarding latency vs §5.6 (≤2 s begin-forward) | §5.6 | soak report in `docs/qa/soak-03.md` | Latency gate recorded; regressions filed | ☐ |

**Evidence:** _(links to commits/PRs when done)_

**Notes:**
- S03-T3 un-stubs the `main.py` banner demo; the smoke run is replaced by a real lifecycle but must
  stay runnable without a destination (disabled destinations are skipped — existing `enqueue`
  behavior).
- `RetryPolicy` defaults (5.0 s base, 5 attempts) come from config in T3; add a RED test that
  config values flow into the policy.
