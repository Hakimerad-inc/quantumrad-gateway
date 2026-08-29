# Sprint 02 — Receiver SCP + Spool File Storage (Weeks 3–4)

**Goal:** Deliver the inbound half of the store-and-forward loop: a real C-STORE SCP receiver
(persist-before-acknowledge) and DICOM file/blob storage in the spool — completing US-01 and US-02.

**Exit criteria:** Receiver binds configured port/AET, persists all instances before ack, handles
ALL compressed syntaxes (JPEG, JPEG 2000, JPEG-LS, RLE) with selective decompression, writes `.tags`;
partial studies retained and marked incomplete with a recovery scan on restart (US-01/US-02 AC).

**PRD refs:** §5.2 steps 1–2, §3.3, §5.6, §14 US-01/US-02.
**Refinement refs:** product-refinement-spec.md §2.1 (all compressed syntaxes, selective decompress).

**Per-sprint gate:** re-verify scaffold state first — `spool/__init__.py` currently has only
`spool_dir` path handling (no blob storage); `db.py` is at `SCHEMA_VERSION = 1`; `receiver/` is a
flag-only stub.

| ID | Task | PRD ref | RED → GREEN | DoD | Status |
|----|------|---------|-------------|-----|--------|
| S02-T1 | **Receiver transport abstraction:** `ReceiverTransport` protocol (start/stop/on_study callback) per ADR-0001; in-process fake transport for tests | §5.2 | `tests/test_receiver.py::test_*` — start/stop lifecycle, callback fires per study, clean stop under load | Receiver module no longer raises `NotImplementedError` for lifecycle | ☐ |
| S02-T2 | **pynetdicom C-STORE SCP transport:** bind port/AET from config; AE allow-list (`allowed_ae_titles`, empty = any); max associations; **ALL compressed transfer syntaxes accepted** (JPEG 2000 lossless/lossy, JPEG-LS, RLE, Deflated Explicit VR Little Endian) | §5.5, §6.3, refinement | `tests/test_receiver_transport.py` — real association from pynetdicom SCU over localhost; wrong-AET rejected; ≥25 concurrent associations pass; compressed syntax datasets accepted | US-01 concurrency AC met on localhost; all compressed syntaxes accepted | ☐ |
| S02-T3 | **Store-before-acknowledge:** persist every instance to `spool/{studyUID}/{seriesUID}/` before C-STORE response; **selective decompression**: decompress common syntaxes (JPEG 2000 lossless, JPEG-LS, RLE) on receive; pass-through rare/proprietary syntaxes; store original transfer syntax UID in metadata | §3.4-1, §5.2-2, refinement | `tests/test_storage.py` — file written before ack (fake transport asserts ordering); JPEG-2000/deflated synthetic datasets round-trip; selective decompress verified; original syntax preserved | K1 mechanism in place; all syntaxes handled | ☐ |
| S02-T4 | **`.tags` extraction:** write `*.tags` metadata sidecar per instance (pydicom tag dump in mercure `getdcmtags` style) | §5.2-2, §8.3 | `tests/test_tags.py` — sidecar exists per instance, contains Modality/Accession/StudyUID, matches DB row | Files + DB consistent | ☐ |
| S02-T5 | **Study ingest to spool state machine:** on first instance → `RECEIVING`; on study completion → `RECEIVED` + `enqueue()` to enabled destinations (reuses existing `Spool.receive/enqueue`) | §3.3, §5.2 | `tests/test_receiver.py` — multi-instance study ends `RECEIVED` with routes created; study-level timeout marks incomplete | End-to-end receive path green | ☐ |
| S02-T6 | **Partial study + recovery scan:** mid-transfer failure → retained partial study marked incomplete; on startup, scan spool dir vs DB, reconcile states | §6.3, US-02 | `tests/test_recovery.py` — kill fake transport mid-study; partial rows/files present; restart scan completes or flags study | US-02 AC green | ☐ |
| S02-T7 | **Schema migration pattern:** bump `SCHEMA_VERSION` to 2 (add file-path columns) + a tested migration helper (this establishes the pattern for future bumps) | §5.4 | `tests/test_spool.py` additions — v1 DB migrates to v2, data preserved, version stamped | Migration runs on `open_database()`; old spools survive upgrade | ☐ |

**Evidence:** _(links to commits/PRs when done)_

**Notes:**
- If ADR-0001 chose DCMTK `storescp`, S02-T2 becomes the wrapper transport; RED tests are unchanged
  (they target the transport protocol, not pynetdicom).
- Study-completion detection (T5): count-based per study from `.tags`/DB rows; document the
  heuristic in the module docstring — modalities don't always signal end-of-study.
- **Refinement change (S02-T2/T3):** The original spec only mentioned "compressed syntax support"
  generically. The refinement specifies **ALL** compressed syntaxes: JPEG 2000 (lossless + lossy),
  JPEG-LS, RLE, and Deflated. S02-T3 adds **selective decompression**: common syntaxes are
  decompressed to uncompressed for storage; rare/proprietary syntaxes are passed through as-is.
  The `receiver.decompress_common` config flag controls this behavior (default: true).
