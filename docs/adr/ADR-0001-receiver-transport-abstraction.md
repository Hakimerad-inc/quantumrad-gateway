# ADR-0001: Receiver Transport Abstraction

**Status:** Accepted
**Date:** 2026-08-29
**Deciders:** Product + Engineering
**Relates to:** PRD §9 Phase 0 (spike), §5.2 step 1-2, §5.6, Sprint 01 S01-T3 spike, Sprint 02 S02-T1/T2

> **Amendment (2026-09-24):** US-01's concurrency criterion was re-sized from
> ≥25 to **≥5 concurrent associations** (PRD §5.6, §US-01). The spike below was
> genuinely run at 25, so those numbers stand as measured; the shipped default
> is now `max_associations = 5`, still configurable. pynetdicom handled 25 at
> spike time, so 5 is comfortably inside the validated envelope — the change
> is a sizing decision, not a transport one, and this ADR's decision is
> unaffected.

## Context

The gateway's inbound half is a DICOM C-STORE SCP: it must accept associations
from any modality, persist every instance to the spool before acknowledging
(store-before-ack, PRD §3.4), and handle **all** compressed transfer syntaxes
(JPEG, JPEG 2000, JPEG-LS, RLE, Deflated — refinement spec §2.1) at the
concurrency level required by US-01 (≥25 simultaneous associations).

Two transport backends were viable:

1. **pynetdicom** — pure-Python DIMSE implementation of the DICOM standard,
   SCP and SCU support, all standard presentation contexts.
2. **DCMTK `storescp`** — battle-tested C++ store SCP, maximum raw throughput,
   but an external process (needs bundling/process management on Windows).

The PRD (§5.6) set a throughput gate (~30 MB/s) that needed to be validated by
a spike before committing to a transport.

## Decision

**Use pynetdicom as the C-STORE SCP transport for the MVP**, behind a small
`ReceiverTransport` protocol so DCMTK remains a drop-in fallback.

The transport contract (start/stop/on-study callback) was sketched during the
Phase 0 spike and became the RED contract for Sprint 02-T1:

```python
class ReceiverTransport(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    # per-study callback fired once per received study
```

## Spike Results (S01-T3, measured)

| Metric | Target (PRD §5.6) | pynetdicom | Verdict |
|--------|-------------------|------------|---------|
| Throughput (MB/s) | ~30 MB/s | validated over localhost, PDU 128 KiB | ✅ |
| Concurrent associations | ≥25 | 25+ handled (default raised to 25, configurable) | ✅ |
| Idle RAM | — | pure Python, low overhead | ✅ |

The spike's abstraction sketch dropped directly into
`src/mercure_gateway/receiver/` for Sprint 02-T1; the pynetdicom SCP landed as
S02-T2 (`tests/test_receiver_wire.py`).

## Why not DCMTK `storescp` for the MVP

- **Process management complexity** — an external binary needs lifecycle
  management, bundling, and its own logging/error handling; pynetdicom runs
  in-process.
- **Purity** — pynetdicom keeps the gateway a single Python codebase, which
  simplifies Windows packaging (S06) and the Tauri sidecar story (ADR-0002).
- pynetdicom met the throughput and concurrency gates on localhost; no
  measurable reason to take on the extra process.

DCMTK `storescp` remains the **documented fallback**: if a field deployment
hits throughput or codec issues pynetdicom cannot resolve, S02-T2's RED tests
target the transport protocol, not pynetdicom — the tests are unchanged when
the backend swaps.

## Consequences

- `Receiver` (SCP) is built on pynetdicom; the transport contract lives in
  `tests/test_receiver_transport.py`.
- `allowed_ae_titles` (empty = accept any) and `max_associations` (default 5,
  per the 2026-09-24 amendment above; 25 at spike time)
  map directly onto pynetdicom AE settings.
- All compressed transfer syntaxes are accepted via
  `pynetdicom.uid.AllTransferSyntaxes`; selective decompression happens at
  storage time (`receiver.decompress_common`), not at the transport.
- The fake modality SCU in `demo/fake_modality.py` (S01-T2) is the mirror-image
  SCU used by the test rig and demo chain.
