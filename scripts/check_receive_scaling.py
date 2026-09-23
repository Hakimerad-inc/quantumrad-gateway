#!/usr/bin/env python
"""US-01 receive-scaling measurement — 1 vs 5 concurrent associations.

The companion to ``check_perf_gates.py``.  That script measures the
*forwarding* path (claim → delivery); this one measures the *receive* path
(C-STORE → durable spool), which is where US-01's concurrency acceptance
criterion actually lives: 5 simultaneous modalities must all be served.

The number that matters is the *scaling ratio* — throughput at 5
associations divided by throughput at 1.  Perfect linear scaling would give
5×.  The receive path cannot achieve that, and deliberately: every instance
pays one file ``fsync`` plus up to three directory ``fsync`` and one
synchronous database transaction before it is acknowledged, because that is
the store-before-ack guarantee (PRD §3.4, review H1).  Those barriers are
the reason a power cut cannot lose a study, and they are what a concurrent
load serializes through.

**This script reports; it does not gate.**  ``.full-review/phase3-3A-testing.md``
proposed asserting ``inst_s_25 / inst_s_1 >= 3.0`` (``SCALING_FLOOR``), but
the measurement that proposal was written against reported **1.24×** at 25
associations — 25× the offered load yielding 1.24× the throughput.  That
proposal predates the sizing decision to move US-01's concurrency criterion
to 5 associations, where the same barrier measures far healthier (about
2.2×).  Even so, the floor as documented would have failed on the shipped
code at the old sizing, and the reason is the durability barrier above, not
a bug.  Gating a floor derived from an architectural ceiling bakes that
ceiling in as though it were a target — the existing 5 items/s throughput
floor has exactly that flaw, and its own docstring calls it "a sanity floor,
not a target".  So this measures and prints; a floor belongs here only once
we have enough CI runs to know the variance, and a *better* ratio belongs in
an ADR that changes the durability posture, not in a residue item.

The benchmark behind the original 1.24× figure was never committed (it lived
at ``.full-review/.perfbench/store_bench.py``, which is absent from the tree
and from git history), so this script also re-establishes a reproducible
measurement in the repo.  Its numbers supersede the phase3-3A table.

Measurements use an **on-disk** database (``open_database``, not
``mem_database``) and a real temp spool directory, because the whole point is
the fsync path — an in-memory database would measure nothing.  Associations
are real pynetdicom SCUs against a real in-process Receiver, so the socket
and the DIMSE negotiation are exercised too.

Usage:
    python scripts/check_receive_scaling.py             # default: 1 and 5
    python scripts/check_receive_scaling.py --assoc 1 5
    python scripts/check_receive_scaling.py --per-assoc 200

Always exits 0 once the measurement completes; a failure to measure exits 1.
"""

from __future__ import annotations

import argparse
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

from pydicom.dataset import Dataset

# US-01's concurrency criterion.
_DEFAULT_ASSOC = (1, 5)
# Enough instances per association that per-instance cost dominates
# association setup, and few enough that a run stays inside CI's patience.
# The phase3-3A benchmark used 200; that took minutes, so default lower and
# let --per-assoc raise it for a local soak.
_INSTANCES_PER_ASSOC = 60

_CT_CONTEXT = "1.2.840.10008.5.1.4.1.1.2"  # CT Image Storage


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _make_dataset(study_uid: str, sop_uid: str) -> Dataset:
    """A minimal valid CT dataset with a real file meta header.

    The receiver validates the UIDs and the handler negotiates a presentation
    context against the file meta's transfer syntax, so this has to be a
    genuinely sendable dataset, not a bare ``Dataset()``.
    """
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import UID, CTImageStorage, ExplicitVRLittleEndian

    ds = Dataset()
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = f"{study_uid}.1"
    ds.SOPInstanceUID = sop_uid
    ds.SOPClassUID = CTImageStorage
    ds.PatientName = "BENCH^SCALE"
    ds.PatientID = "BENCH"
    ds.Modality = "CT"
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = CTImageStorage
    ds.file_meta.MediaStorageSOPInstanceUID = UID(sop_uid)
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    return ds


def _is_success(status: object) -> bool:
    return getattr(status, "Status", None) == 0x0000


def _run_associations(n_assoc: int, per_assoc: int, root: Path) -> dict[str, float]:
    """Drive ``n_assoc`` concurrent SCUs through the real Receiver + Spool.

    Each association sends ``per_assoc`` instances of its own study, so the
    studies do not collide and every store takes the full durability path.
    """
    from pydicom.uid import generate_uid
    from pynetdicom import AE

    from mercure_gateway.config import ReceiverConfig
    from mercure_gateway.receiver import Receiver
    from mercure_gateway.spool import Spool
    from mercure_gateway.spool.db import open_database

    work = root / f"a{n_assoc}"
    work.mkdir(parents=True, exist_ok=True)

    # Spool with no GatewayConfig. That is how auto-enqueue is kept out of the
    # measurement — and note what *not* to do here: setting
    # ``auto_enqueue_delay_sec = 0`` does not disable it, it makes it
    # synchronous (spool/__init__.py:476), so every store would then pay a
    # second full transaction inline. A positive delay arms a per-study timer
    # that still fires inside the run. Passing config=None makes
    # ``_arm_auto_enqueue`` return before any of that, so the timing covers the
    # store path and nothing else.
    spool = Spool(open_database(work / "gw.db"), spool_dir=work / "spool")
    recv = Receiver(
        ReceiverConfig(
            ae_title="BENCHRCV",
            port=_free_port(),
            max_associations=max(32, n_assoc + 4),
        ),
        spool,
    )
    recv.start()
    latencies: list[float] = []
    errors: list[int] = []
    lock = threading.Lock()

    def send_one(a: int) -> None:
        study = f"1.2.826.0.1.3680043.10.{a}"
        ae = AE(ae_title=f"BENCHSCU{a}")
        ae.add_requested_context(_CT_CONTEXT)
        try:
            assoc = ae.associate("127.0.0.1", recv.port)
        except Exception:
            with lock:
                errors.append(a)
            return
        if not assoc.is_established:
            with lock:
                errors.append(a)
            return
        try:
            for _i in range(per_assoc):
                sop = generate_uid()
                t0 = time.perf_counter()
                status = assoc.send_c_store(_make_dataset(study, sop))
                dt = time.perf_counter() - t0
                with lock:
                    latencies.append(dt)
                    if not _is_success(status):
                        errors.append(a)
        finally:
            assoc.release()

    threads = [threading.Thread(target=send_one, args=(a,)) for a in range(n_assoc)]
    start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=300)
    elapsed = time.perf_counter() - start

    try:
        recv.stop()
    finally:
        spool.stop()

    total = n_assoc * per_assoc
    latencies.sort()
    p50_ms = latencies[len(latencies) // 2] * 1e3 if latencies else float("nan")
    p95_ms = (
        latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] * 1e3
        if latencies
        else float("nan")
    )
    # Count the .dcm files actually on disk rather than trusting the send
    # loop's status codes — store-before-ack means a durable file is the
    # ground truth, and this is the number the ratio is divided by.
    stored = sum(1 for _ in (work / "spool").rglob("*.dcm"))
    return {
        "inst_s": total / max(elapsed, 1e-9),
        "p50_ms": p50_ms,
        "p95_ms": p95_ms,
        "stored": float(stored),
        "expected": float(total),
        "errors": float(len(errors)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--assoc",
        type=int,
        nargs="+",
        default=list(_DEFAULT_ASSOC),
        help="association counts to measure (default: 1 and 5)",
    )
    parser.add_argument(
        "--per-assoc",
        type=int,
        default=_INSTANCES_PER_ASSOC,
        help="instances per association (default: 60)",
    )
    args = parser.parse_args()

    counts = sorted(set(args.assoc))
    if 1 not in counts:
        print("note: adding assoc=1 — the scaling ratio needs a 1-association baseline")
        counts = [1, *counts]

    results: dict[int, dict[str, float]] = {}
    with tempfile.TemporaryDirectory(prefix="recv-scaling-") as tmp:
        root = Path(tmp)
        for n in counts:
            print(f"measuring {n} association(s) x {args.per_assoc} instances ...", flush=True)
            results[n] = _run_associations(n, args.per_assoc, root)

    print()
    header = (
        f"{'assoc':>6} {'inst/s':>10} {'per-assoc':>11} "
        f"{'p50 ms':>10} {'p95 ms':>10} {'stored':>12} {'errors':>7}"
    )
    print(header)
    print("-" * len(header))
    for n in counts:
        r = results[n]
        per_assoc = r["inst_s"] / n
        print(
            f"{n:>6} {r['inst_s']:>10.2f} {per_assoc:>11.3f} "
            f"{r['p50_ms']:>10.1f} {r['p95_ms']:>10.1f} "
            f"{r['stored']:>10.0f}/{r['expected']:<.0f} {r['errors']:>7.0f}"
        )

    if len(counts) >= 2:
        top, base = results[counts[-1]], results[counts[0]]
        ratio = top["inst_s"] / max(base["inst_s"], 1e-9)
        print()
        print(f"scaling ratio inst_s_{counts[-1]} / inst_s_{counts[0]} = {ratio:.2f}x")
        print(f"  (linear would be {counts[-1] / counts[0]:.0f}x)")
        print()
        print("report-only — no floor asserted. See the module docstring for why.")
        if base["errors"] or top["errors"] or top["stored"] != top["expected"]:
            print()
            print(
                "ERROR: some instances did not land; the measurement is unreliable.",
                file=sys.stderr,
            )
            return 1

    print()
    print("If these numbers supersede the phase3-3A table, record them in")
    print(".full-review/P2-P3-BACKLOG.md and cite this run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
