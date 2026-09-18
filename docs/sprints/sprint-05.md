# Sprint 05 — Reports MVP: DICOM SR + PDF via C-FIND/C-MOVE (Weeks 9–10)

**Goal:** Complete the report loop — query the configured PACS by Accession/Study UID, retrieve
DICOM SR **and** Encapsulated PDF, store them, surface them — with on-demand retrieval (US-05, US-06),
and settle Q2 (DICOM SR + PDF first).

**Exit criteria:** Report lookup by Accession/Study UID; SR + PDF retrieved via C-FIND/C-MOVE;
status transitions correct; retrieved reports viewable (SR rendered + PDF embedded); manual
"Request report" works (US-05/US-06 AC).

**PRD refs:** §5.2 step 5, §3.3 (report states), §5.4 reports table, §13 Q2, §14 US-05/US-06.
**Refinement refs:** product-refinement-spec.md §2.3 (SR + PDF reports), §3.1 (MVP reports).

**Per-sprint gate:** `reports/__init__.py` has `request_report()` → PENDING row (implemented) and
`retrieve()` raising `NotImplementedError` (stub) — this sprint implements the transport and the
loop.

| ID | Task | PRD ref | RED → GREEN | DoD | Status |
|----|------|---------|-------------|-----|--------|
| S05-T1 | **Report query transport (C-FIND):** pynetdicom C-FIND SCU against query source; filter by Accession/StudyUID **+ SOP Class UID** to distinguish SR (1.2.840.10008.5.1.4.1.1.88.33) from Encapsulated PDF (1.2.840.10008.5.1.4.1.1.104.2) | §5.2-5, US-05, refinement | `tests/test_report_find.py` — fake C-FIND SCP returns SR matches; fake C-FIND SCP returns PDF matches; no match → status stays pending; connection error → `RETRIEVAL_FAILED`; SOP class filter works | Query path green against fake SCP; SR + PDF distinguished | ✅ |
| S05-T2 | **Report retrieve transport (C-MOVE):** negotiate C-MOVE to gateway's own store SCP; land SR files under `reports/{study_uid}/sr/`; land PDF files under `reports/{study_uid}/pdf/`; update row with `sop_class_uid` and `file_name` | §5.2-5, §5.4, refinement | `tests/test_report_move.py` — C-MOVE from fake SCP lands SR file; C-MOVE lands PDF file; `file_path` set; status `RETRIEVED` + `retrieved_at`; correct subdirectory used | US-05 retrieval AC green for both SR and PDF | ✅ |
| S05-T3 | **Report status machine wiring:** PENDING → RETRIEVING → RETRIEVED / RETRIEVAL_FAILED transitions in `ReportRetriever`; audit events per transition | §3.3, §5.2-6 | `tests/test_reports_service.py` — full transition walk; illegal transition rejected; audit rows present | Status transitions correct per US-05 | ✅ |
| S05-T4 | **Polling scheduler:** `poll_interval_sec`-driven loop over PENDING reports; SLA window (5 min default) tracked for K3 | §5.5, K3 | `tests/test_reports_poller.py` — interval honored (injected clock), SLA expiry flags metric/event | K3 mechanism in place | ✅ |
| S05-T5 | **On-demand "Request report":** manual trigger bypasses poll wait; re-request allowed after failure; user can request specific type (SR, PDF, or both) | US-06, refinement | `tests/test_reports_service.py` additions — immediate retrieval attempt on request; failed → re-request → RETRIEVED; type filter works | US-06 AC green | ✅ |
| S05-T6 | **Report rendering + viewer data contract:** DICOM SR → structured text render; **Encapsulated PDF → raw PDF extraction**; render service gets RED tests (UI surface in S06 web admin) | §2.2 Flow D, refinement | `tests/test_report_render.py` — SR dataset → readable report text; PDF extraction → valid PDF file; malformed SR → error not crash | Report viewable; render logic tested for both types | ✅ |
| S05-T7 | **Q2 decision (no TDD):** confirm DICOM SR + PDF first, DICOMweb next (S08); record in PRD §13 + ADR | §13 Q2 | — | Decision logged; S08 scope confirmed | ✅ |

**Evidence:**
- **266 tests** (all pass), mypy strict clean, ruff clean
- ADR-0005: DICOM SR + PDF first, DICOMweb deferred to S08
- All 7 tasks ✅ — Sprint 05 exit criteria met

**Notes:**
- S05-T2 needs the gateway to act as a C-STORE SCP for the C-MOVE destination — reuse the S02
  receiver transport on an internal port rather than new code.
- Orthanc on the S01 rig is the integration target for T1/T2; keep fake SCPs for unit tests per §10.
- **Retrospective (review P0-6/P0-7, 2026-09-18):** the S05-T2/T5 ✅ rows were
  earned by tests against *fake* finder/mover callables, and US-06's AC was
  checked the same way — so the sprint exited green while the production path
  was dead. `main()` constructed `ReportRetriever` without injecting
  `finder`/`mover`, and every real retrieval failed. The exit criterion that
  would have caught it is a test that drives the composition root, now
  `tests/test_main_report_retrieval.py` (real in-process C-FIND/C-MOVE SCPs,
  report reaches `RETRIEVED` with the file on disk). That test also found a
  second bug the fakes hid: received datasets have no file meta, so
  `move._save` raised on any genuine instance. See the ADR-0005 corrigendum.
  **Change to sprint exit criteria:** any feature claimed ✅ must have at least
  one test that reaches it from the composition root, not only from its units.
- **Refinement change (S05-T1/T2/T6):** The original spec specified DICOM SR only. The refinement
  adds **Encapsulated PDF** support. S05-T1 filters by SOP Class UID in C-FIND queries to
  distinguish SR from PDF. S05-T2 stores to separate subdirectories (`sr/` vs `pdf/`). S05-T6
  extracts raw PDF from the encapsulated DICOM object. The `reports.report_types` config option
  (default: `["sr", "pdf"]`) controls which types are retrieved.
- **Refinement change (S05-T5):** On-demand request now supports type filtering — user can request
  SR only, PDF only, or both. The `request_report()` method gains a `report_type` parameter.
