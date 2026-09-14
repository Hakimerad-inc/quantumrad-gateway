# S09-T9: USB Performance Baseline — Sprint 09 (for Sprint 10 comparison)

**Status:** partial — dev-box proxy measurements recorded 2026-09-14; USB-hardware
rows (flash-throughput, boot modes) require the physical rig and are pre-registered
below. Targets from `usb-dongle-gateway-spec.md` §10 (K9/K10).

**Measured on:** this Linux dev box (NVMe-backed fs, no loop-device perms for
non-root). These are *proxy* upper-bound numbers: a USB 3.0 flash stick
(Samsung BAR Plus class, spec §3 table: ~300/60 MB/s) is roughly 3–5× slower
on sustained writes than the dev NVMe, so the flash rows carry a derating note.

## K10 — hot-unplug flush ≤ 10 s

| Measurement | Target | Proxy result (dev NVMe) | USB stick |
|---|---|---|---|
| 64 × 2 MiB store+fsync burst (worst-case in-flight instances at unplug) | ≤ 10 s total | **1.33 s** | est. ~3–7 s @ 60 MB/s sustained — within budget, re-measure on rig |
| Flush bounded by 10 s timeout (forced-removal path) | mechanism proven | `tests/test_hot_unplug.py` (5 tests) ✅ | — |
| Marker write fsynced before shutdown | mechanism proven | `test_marker_write_is_fsynced` ✅ | — |

The K10 *safety* guarantee is the timeout bounding, which is test-covered
regardless of hardware; the numbers above say the flush finishes comfortably
inside 10 s rather than relying on the escape hatch.

## Recovery scan (unclean removal boot)

| Measurement | Target | Proxy result (dev NVMe) | USB stick |
|---|---|---|---|
| Scan 200 studies / 1000 spool files, reconcile DB ↔ disk | sub-second per §7.3 spirit | **2.18 s** | est. ~5–10 s (exFAT metadata is slow); recovery runs before receiver opens the DICOM port? — verify on rig, PRD §3.4 ordering |
| Recovery contract (interrupted study → RECEIVED, marker cleared, logged) | mechanism proven | `tests/test_usb_recovery.py` (4 tests) ✅ | — |

## K9 — boot → gateway ready

| Measurement | Target | Result |
|---|---|---|
| Linux mode (P1 Alpine boot → gateway listening) | ≤ 30 s | ⏳ hardware — needs flashed stick + PC (S10-T2) |
| Windows mode (plug-in → gateway ready) | ≤ 15 s | ⏳ hardware — needs flashed stick + Win10/11 (S10-T3) |

## Flash write throughput (DICOM ingest to P3)

| Measurement | Target | Result |
|---|---|---|
| Sustained C-STORE ingest to exFAT partition | KPI: no ack stalls > K10 flush bound | ⏳ hardware — `e2e` rig sends synthetic burst once flashed |

## How these were measured (rerunnable)

Proxy rows: the 64×2 MiB fsync burst and `recover()` over a 200-study/1000-file
synthetic spool (zero-byte files, `mem_database`) in one-off scripts — commands
in the S09-T9 session record; formalize as a `--perf` marked test in S10 if the
rig wants CI-tracked numbers.

Hardware rows: on the flashed dongle (S10-T1 `scripts/flash_usb.sh` — layout
plan is unit-tested in `tests/test_usb_partition.py`; the privileged loop-device
e2e skips without root), run the same burst against the real mount and fill the
"USB stick" columns here and in `docs/qa/usb-perf-10.md` (S10-T10).

## Verdict so far

- **K10 mechanism:** proven in CI (test-covered), proxy timing comfortable.
- **K9:** cannot be claimed without the rig — tracked in sprint-10 T2/T3.
- Sprint 10 comparison baseline: use the dev-proxy columns as the sanity floor;
  a flash result worse than ~3× the proxy suggests a bad stick/driver, not the
  gateway.
