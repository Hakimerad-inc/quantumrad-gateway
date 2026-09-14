# S10-T11: USB Dongle UAT — Scripted Walkthrough (usb-dongle-spec §10)

**Status:** template — pre-registered; every step is hardware-dependent (a
flashed 32 GB+ USB 3.0 stick + a bootable PC + Windows 10/11 host). The
software under test is complete and CI-covered (`tests/test_usb_partition.py`
plan tests, `test_hot_unplug`, `test_usb_recovery`, `test_disk_full_ui`, LED
state machine); this sheet captures the on-hardware evidence Sprint 10 needs.

**Date:** _fill in_ · **Tester:** _fill in_ · **Stick model:** _fill in_
(capacity: 32/64 GB) · **PC models booted:** _≥3 per spec, note UEFI/legacy_
**flash_usb.sh version:** _git SHA_ · **Gateway version:** _1.1.0-rcX_

Timing capture: `time` around the marked steps; enter seconds. K9 gate:
Linux-mode ready ≤30 s; Windows mode ≤15 s. K10: hot-unplug flush ≤10 s
(flush row also in `docs/qa/usb-perf-09.md`).

## 1. Flash (admin workflow — target <15 min, spec §8.2)

| Step | Expected | Result |
|---|---|---|
| `bash scripts/flash_usb.sh --device /dev/sdX --appimage <file>` on Linux host | Plan printed; confirmation; wipe+partition+mkfs completes | ☐ |
| Elapsed wall time <15 min | recorded: ___ s | ☐ |
| Guide (`docs/guides/usb-quickstart.md`) followed by a non-technical colleague, no verbal help | completed unaided | ☐ |
| Windows: run the same flash from a Windows 10/11 machine (WSL2 or the documented manual path in the guide) | completes / documented workaround works | ☐ |

## 2. Linux mode — boot to gateway (K9)

| Step | Expected | Result |
|---|---|---|
| PC1 (UEFI): boot from USB | Alpine boots; gateway systemd unit auto-starts | ☐ |
| Time: power-on → `GET /api/system/health` ok | ≤30 s; recorded: ___ s | ☐ |
| PC2 (legacy BIOS), PC3: same | boots on all 3+ models | ☐ |
| Web admin at :8080 on the PC's LAN IP reachable from another host | dashboard loads | ☐ |

## 3. First study forward (Linux mode)

| Step | Expected | Result |
|---|---|---|
| Complete wizard via web UI (destination = test Orthanc at `test-rig`) | config saved to P3 `mercure-gateway.json` | ☐ |
| Modality C-STOREs one study to the gateway | receiver accepts; study in queue | ☐ |
| Study forwarded to hub | state SENT; audit events chained | ☐ |
| spool/ files land on the exFAT P3 partition | visible from Windows host too (mode-switch proof) | ☐ |

## 4. Hot-unplug (K10) — the critical safety test

| Step | Expected | Result |
|---|---|---|
| While a second study is mid-forward, pull the stick | gateway flushes + writes shutdown marker (LED ⚪ if fitted); PC does not hang | ☐ |
| Time pull → last fsync (measure with `dmesg -w` / rig logger) | ≤10 s; recorded: ___ s | ☐ |
| Re-plug (or boot the stick again) | recovery scan runs; interrupted study marked RECEIVED and re-forwarded automatically | ☐ |
| No data loss: first study remains SENT in DB after recovery | ✓ | ☐ |

## 5. Windows mode (K9)

| Step | Expected | Result |
|---|---|---|
| Plug stick into running Windows 10 | scheduled-task launcher fires (no `autorun.inf`); gateway starts | ☐ |
| Time plug-in → health endpoint ok | ≤15 s; recorded: ___ s | ☐ |
| Tray icon present; web UI opens | dashboard loads | ☐ |
| Forward a study Windows→hub from P3 config | SENT; same audit chain continues | ☐ |
| Windows 11: repeat | same results | ☐ |

## 6. Storage budget (S10-T7 on real exFAT)

| Step | Expected | Result |
|---|---|---|
| Fill P3 with synthetic studies past 90% capacity | web banner + LED 🟡; oldest DELIVERED purged | ☐ |
| FAILED study present during purge | never removed (PRD §3.4) | ☐ |

## Verdict block (fill at end)

- K9 Linux: ___ s / 30 s · K9 Windows: ___ s / 15 s · K10: ___ s / 10 s
- Defects found: _list or "none"_
- Pass / Fail: ☐ · Signed off by: __
