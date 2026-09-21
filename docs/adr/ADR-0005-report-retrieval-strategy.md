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

**For v1.0, the gateway retrieves DICOM SR and Encapsulated PDF.** C-FIND/C-MOVE was the
MVP transport; **DICOMweb / WADO-RS retrieval has since shipped** (Sprint 08, S08-T4 — see
the 2026-09-21 amendment below). Which transport runs is selected by
`reports.query_source.type` (`dicom` or `dicomweb`).

Rationale:

- **C-FIND/C-MOVE is the universal baseline** — every DICOM-compliant PACS supports it. The
  gateway's target deployment is alongside existing PACS (both legacy and modern); mandating
  DICOMweb at MVP would exclude sites with older archives.
- **SR + PDF covers the clinical workflow** — the two formats are the ones PRD §14
  US-05/US-06 names for the report user stories, and the only ones the shipped
  retrievers filter for. SR enables downstream structured data extraction; PDF
  provides a universal human-readable format. (The ">95% of report delivery"
  figure this bullet long cited was attributed to a PRD §1 survey that does not
  exist — the PRD's only ≥95% figure is the K3 retrieval-success SLA, which
  measures a different quantity. The claim is removed rather than re-sourced.)
- **DICOMweb is additive, not a replacement** — WADO-RS is an optimization for
  cloud-native PACS, offered alongside C-FIND/C-MOVE rather than instead of it. The
  transport seam is the `ReportTransport` protocol in `reports/transport.py` (S08-T3):
  C-FIND/C-MOVE is wrapped by `DICOMReportTransport` over `reports/find.py` and
  `reports/move.py`, and a DICOMweb transport slots in behind the same `find`/`retrieve`
  methods without changing the status machine, poller, or render layer.

## Consequences

- `reports/find.py` and `reports/move.py` implement C-FIND/C-MOVE Study Root Q/R only;
  they are the DIMSE backend, wrapped by `DICOMReportTransport` in `reports/transport.py`.
  `reports/dicomweb.py` is a second transport (QIDO-RS/WADO-RS over HTTPS) against the same
  `ReportTransport` protocol; both are registered in `transport.py`'s `_register_defaults()`.
- Both transports filter by SOP Class UID: SR (`1.2.840.10008.5.1.4.1.1.88.33`) and PDF
  (`1.2.840.10008.5.1.4.1.1.104.2`).
- Retrieved reports are stored under the spool's reports directory
  (`<spool_dir>/reports/{study_uid}/{sr|pdf}/{sop_uid}.dcm`) by both transports; the root is
  anchored to the spool, not the process cwd, so reports travel with a USB appliance.
- `RenderService` is dual-headed: `render_sr()` (structured text extraction) and
  `extract_pdf()` (raw PDF extraction from the encapsulated DICOM object).
- The DICOMweb transport shipped in Sprint 08 (S08-T4), implementing the `ReportTransport`
  protocol that the S08-T3 seam introduced. That seam *wraps* the C-FIND/C-MOVE classes
  (`ReportFinder` in `reports/find.py`, `ReportRetrieve` in `reports/move.py`) — both are
  still live production wiring, composed into `DICOMReportTransport` and constructed by the
  `dicom` factory at the composition root; the seam did not retire them. The status
  machine, poller and render layer were untouched; the config was not (`ReportQuerySource.type`
  was widened and a `path` field added — see the amendment).
- The `reports.report_types` config option defaults to `["sr", "pdf"]` and controls which
  types are retrieved; the DICOMweb transport honours the same option.

## Corrigendum (2026-09-18, review P0-6/P0-7)

This ADR's "Consequences" read as though the C-FIND/C-MOVE path was wired and
working. Until the review it was not. `main()` constructed `ReportRetriever`
directly, so `finder`/`mover` were never injected and every retrieval hit
`RuntimeError("report transports (finder/mover) not configured")`, was
swallowed to a warning, and marked the report FAILED. The units tested the
transports against fakes; nothing drove the composition root, so the gap was
invisible to the suite and this ADR's claims were unfalsifiable.

Two things changed. `main._build_report_retriever()` now builds the DICOM
transport through the `reports.transport` registry (the seam S08-T3 added for
exactly this) with the gateway's own AE title, store-SCP port and reports
directory; and `tests/test_main_report_retrieval.py` drives that wiring
against in-process pynetdicom C-FIND/C-MOVE SCPs so the full roundtrip — a
requested report reaching `RETRIEVED` with the instance on disk — is asserted
rather than assumed. Writing that test also surfaced a second latent bug the
fakes had hidden: a dataset arriving over DIMSE carries no file meta
(group 0002 is never transmitted), so `move._save`'s
`save_as(enforce_file_format=True)` raised on any *real* retrieved instance.
The receiver had the correct pattern; the mover now synthesizes file meta the
same way.

The lesson recorded for the transport seam: any new transport
(`query_source.type` other than `dicom`) must be wired at the composition root
or it is dead code — the registry dispatches by type but does not construct.

## Amendment (2026-09-21) — the deferred DICOMweb transport shipped

The Decision and Consequences above described DICOMweb as "deferred to Sprint 08"
and a DICOMweb transport as future work. That is no longer true, and the
forward-looking bullets have been corrected in place. This section records what
actually shipped, verified against the tree as of 2026-09-21.

**Shipped.** Sprint 08 marked S08-T4 (DICOMweb QIDO/WADO transport) DONE.
`reports/transport.py` registers the `dicomweb` transport class —
`DICOMwebReportTransport` from `reports/dicomweb.py` — alongside the `dicom` and
`fhir` classes in `_register_defaults()`. QIDO-RS locates report instances
filtered by StudyInstanceUID/AccessionNumber and SR/PDF SOP class, paging
through result sets via the RFC 5988 `Link: rel="next"` header; WADO-RS fetches each instance and saves it under
`<spool_dir>/reports/{study_uid}/{sr|pdf}/{sop_uid}.dcm`. HTTP failures raise
`DICOMwebError` rather than looping silently, mirroring the DIMSE transport.

**Wired at the composition root.** A class in the registry is not a working
feature. Commit `c4a5f82` added `register_factory("dicomweb", ...)` to
`main._build_report_retriever()`, because the registry's generic `cls(source)`
fallback cannot build this transport: its constructor takes keyword-only
`base_url` and `reports_dir`, neither of which `ReportQuerySource` carries. The
factory closes over the spool's reports directory and pins `verify_tls=True` —
QIDO/WADO carry report content over HTTPS, and plaintext HTTP to a PACS is a
downgrade this gateway should not offer by default. Before that commit the
transport existed, was unit-tested, and retrieved nothing: a valid `dicomweb`
query source fell through to the `TypeError` fallback and was swallowed to a
single boot-time error log. That is the second instance of the failure mode the
2026-09-18 corrigendum already records — a transport not constructed at the
composition root is dead code, and the unit tests did not notice because they
built the transport directly.

**Config did change.** The original prediction that a DICOMweb transport would
need "no changes to the status machine, poller, or config" held for two of the
three. The config did not: `ReportQuerySource.type` was widened to
`Literal["dicom", "dicomweb", "fhir", "hl7"]`, and a `path` field was added
(default `"dicomweb"`) holding the QIDO/WADO service root below `host:port`.
Orthanc serves it at `/dicomweb`, dcm4chee and cloud stores do not, and the
hardcoded prefix made the transport 404 for every server not laid out like the
one it was written against — with no operator setting to fix it.

**HL7/FHIR remains experimental and is now refused at boot.** The `fhir`
transport class (`HL7FHIRTransport` in `reports/hl7_fhir.py`) is registered, but
`ENABLED` is `False` and its `find`/`retrieve` raise `NotImplementedError`.
`_build_report_retriever()` therefore refuses a `fhir` or `hl7` query source
before dispatch, logging one boot-time error instead of letting the transport
build cleanly and fail every report at poll time. Nothing in v1 retrieves over
HL7 or FHIR.

**Unchanged and still true.** `reports.report_types` still defaults to
`["sr", "pdf"]` (`_DEFAULT_REPORT_TYPES` in `config/__init__.py`), and the
DICOMweb transport honours it. The SR/PDF SOP-class constants, the on-disk
layout, and `RenderService`'s `render_sr()`/`extract_pdf()` split are shared by
both transports — the render layer was genuinely untouched, as the seam
promised.
