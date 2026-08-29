# Sprint 08 — Hub Reporting, Report Transports & Linux (Weeks 15–16)

**Goal:** Close the mercure-hub integration story (US-10), add the pluggable report transports
beyond DICOM SR (Q2 sequence), and produce Linux builds with a cross-platform CI matrix (US-11).

**Exit criteria:** Registration succeeds; events appear hub-side; reporting failure never blocks
forwarding (US-10 AC); Linux AppImage/deb built; cross-platform CI green (US-11 AC).

**PRD refs:** §8.2, §2.3 v1.1, §13 Q2/Q5/Q7, §14 US-10/US-11.

| ID | Task | PRD ref | RED → GREEN | DoD | Status |
|----|------|---------|-------------|-----|--------|
| S08-T1 | **Hub registration client:** `POST /register-gateway` with name/version/contact on first run when configured | §8.2, US-10 | `tests/test_hub_client.py` — registration payload, retry on 5xx, disabled = no-op | Registration AC green | ☐ |
| S08-T2 | **Event streaming to bookkeeper:** lifecycle events → bookkeeper REST; **isolation invariant: any reporting failure must never block receive/forward** | §8.2, US-10, K2 | `tests/test_hub_events.py` — bookkeeper down/timeout/slow → forwarding unaffected (asserted explicitly); batching + backoff | US-10 non-blocking AC green | ☐ |
| S08-T3 | **Report transport plugin surface:** formalize `ReportTransport` protocol alongside the S05 DICOM implementation; retrieval selection by `query_source.type` | §2.3, Q2 | `tests/test_report_transports.py` — registry dispatch by type, unknown type error, SR transport unaffected | Pluggability contract green | ☐ |
| S08-T4 | **DICOMweb QIDO/WADO transport:** QIDO-RS query + WADO-RS SR fetch over HTTPS | §2.3, Q2 | `tests/test_report_dicomweb.py` — against fake DICOMweb server; pagination; TLS; error mapping | DICOMweb reports green | ☐ |
| S08-T5 | **HL7/FHIR transport (experimental, no TDD gate):** ORU^R01-to-folder email-to-folder style drop; feature-flagged, marked experimental in docs | §2.3 | smoke test + flag-off default | Experimental transport available, off by default | ☐ |
| S08-T6 | **Auto-update decision (no TDD):** update channel + signing approach; ADR-0005 (feeds S09-T4 impl) | §13 Q5 | — | Decision + signing vendor recorded | ☐ |
| S08-T7 | **Linux packaging + CI matrix (no TDD):** AppImage/deb build; CI test+quality jobs × ubuntu/windows; artifact gates | §2.3, US-11 | CI matrix green on both OSes; Linux artifact builds | US-11 AC green | ☐ |

**Evidence:** _(links to commits/PRs when done)_

**Notes:**
- T2's non-blocking invariant is the critical test: inject a bookkeeper that hangs/fails and assert
  the forwarder's delivery timing/count is untouched (mirrors US-10's "failure does not block
  forwarding" exactly).
- T5 (HL7/FHIR) is deliberately experimental and flag-gated per §2.5 (no built-in HL7 *integration*
  in v1; v1.1 marks it experimental).
