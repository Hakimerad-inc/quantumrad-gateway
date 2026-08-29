# Sprint 05 — Reports MVP: DICOM SR via C-FIND/C-MOVE (Weeks 9–10)

**Goal:** Complete the report loop — query the configured PACS by Accession/Study UID, retrieve
DICOM SR, store it, surface it — with on-demand retrieval (US-05, US-06), and settle Q2 (DICOM SR
first).

**Exit criteria:** Report lookup by Accession/Study UID; SR retrieved via C-FIND/C-MOVE; status
transitions correct; retrieved report viewable; manual "Request report" works (US-05/US-06 AC).

**PRD refs:** §5.2 step 5, §3.3 (report states), §5.4 reports table, §13 Q2, §14 US-05/US-06.

**Per-sprint gate:** `reports/__init__.py` has `request_report()` → PENDING row (implemented) and
`retrieve()` raising `NotImplementedError` (stub) — this sprint implements the transport and the
loop.

| ID | Task | PRD ref | RED → GREEN | DoD | Status |
|----|------|---------|-------------|-----|--------|
| S05-T1 | **Report query transport (C-FIND):** pynetdicom C-FIND SCU against query source; filter by Accession/StudyUID/SR SOP class | §5.2-5, US-05 | `tests/test_report_find.py` — fake C-FIND SCP returns SR matches; no match → status stays pending; connection error → `RETRIEVAL_FAILED` | Query path green against fake SCP | ☐ |
| S05-T2 | **Report retrieve transport (C-MOVE):** negotiate C-MOVE to gateway's own store SCP; land SR file under `reports/`; update row | §5.2-5, §5.4 | `tests/test_report_move.py` — C-MOVE from fake SCP lands file; `file_path` set; status `RETRIEVED` + `retrieved_at` | US-05 retrieval AC green | ☐ |
| S05-T3 | **Report status machine wiring:** PENDING → RETRIEVING → RETRIEVED / RETRIEVAL_FAILED transitions in `ReportRetriever`; audit events per transition | §3.3, §5.2-6 | `tests/test_reports_service.py` — full transition walk; illegal transition rejected; audit rows present | Status transitions correct per US-05 | ☐ |
| S05-T4 | **Polling scheduler:** `poll_interval_sec`-driven loop over PENDING reports; SLA window (5 min default) tracked for K3 | §5.5, K3 | `tests/test_reports_poller.py` — interval honored (injected clock), SLA expiry flags metric/event | K3 mechanism in place | ☐ |
| S05-T5 | **On-demand "Request report":** manual trigger bypasses poll wait; re-request allowed after failure | US-06 | `tests/test_reports_service.py` additions — immediate retrieval attempt on request; failed → re-request → RETRIEVED | US-06 AC green | ☐ |
| S05-T6 | **Report rendering + viewer data contract:** DICOM SR → structured text render; PDF/TEXT pass-through; render service gets RED tests (UI surface in S06) | §2.2 Flow D | `tests/test_report_render.py` — SR dataset → readable report text; malformed SR → error not crash | Report viewable; render logic tested | ☐ |
| S05-T7 | **Q2 decision (no TDD):** confirm DICOMweb next, HL7/FHIR experimental; record in PRD §13 + ADR | §13 Q2 | — | Decision logged; S08 scope confirmed | ☐ |

**Evidence:** _(links to commits/PRs when done)_

**Notes:**
- S05-T2 needs the gateway to act as a C-STORE SCP for the C-MOVE destination — reuse the S02
  receiver transport on an internal port rather than new code.
- Orthanc on the S01 rig is the integration target for T1/T2; keep fake SCPs for unit tests per §10.
