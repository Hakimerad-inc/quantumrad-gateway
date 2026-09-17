# ADR-0005: DICOM Report Retrieval — SR + PDF First, DICOMweb Next

**Status:** Accepted
**Date:** 2026-08-30
**Deciders:** Product + Engineering
**Relates to:** PRD §13 (Q2), §14 (US-05, US-06), Sprint 05 S05-T7

## Context

PRD §13 Q2 asks: "Which DICOM report types does the gateway support at MVP?" Two options were
evaluated:

1. **DICOM SR (Structured Reporting) + Encapsulated PDF** — the two most common report formats
   in PACS environments today. SR is the native DICOM reporting standard; Encapsulated PDF is the
   most common way legacy RIS/PACS systems deliver rendered reports.
2. **DICOMweb / WADO-RS** — the web-based DICOM retrieval standard, used by newer PACS and
   cloud-native archives.

The refinement spec (§2.3, §3.1) added Encapsulated PDF alongside SR (the original PRD scope),
and explicitly deferred DICOMweb retrieval to a later sprint.

## Decision

**For MVP (v1.0), the gateway retrieves DICOM SR and Encapsulated PDF via C-FIND/C-MOVE only.**
DICOMweb / WADO-RS retrieval is deferred to Sprint 08.

Rationale:

- **C-FIND/C-MOVE is the universal baseline** — every DICOM-compliant PACS supports it. The
  gateway's target deployment is alongside existing PACS (both legacy and modern); mandating
  DICOMweb at MVP would exclude sites with older archives.
- **SR + PDF covers the clinical workflow** — the two formats together account for >95% of
  report delivery in the target market (per PRD §1 survey). SR enables downstream structured
  data extraction; PDF provides a universal human-readable format.
- **DICOMweb is additive, not a replacement** — WADO-RS is a future optimization for
  cloud-native PACS (Sprint 08). The C-FIND/C-MOVE transport is abstracted behind the
  `ReportFinder`/`ReportMover` interfaces in `reports/find.py` and `reports/move.py`;
  a DICOMweb transport can be slotted in without changing the status machine, poller, or
  render layer.

## Consequences

- `reports/find.py` and `reports/move.py` implement C-FIND/C-MOVE Study Root Q/R only.
- `ReportFinder` filters by SOP Class UID: SR (`1.2.840.10008.5.1.4.1.1.88.33`) and PDF
  (`1.2.840.10008.5.1.4.1.1.104.2`).
- `ReportRetriever` stores retrieved reports in `reports/{study_uid}/{sr|pdf}/{sop_uid}.dcm`.
- `RenderService` is dual-headed: `render_sr()` (structured text extraction) and
  `extract_pdf()` (raw PDF extraction from the encapsulated DICOM object).
- Sprint 08 will add a DICOMweb transport implementing the same `ReportFinder`/`ReportMover`
  protocols; no changes to the status machine, poller, or config are expected.
- The `reports.report_types` config option defaults to `["sr", "pdf"]` and controls which
  types are retrieved; a future DICOMweb transport would respect the same option.
