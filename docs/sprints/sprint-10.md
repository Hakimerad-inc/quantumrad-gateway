# Sprint 10 — USB Dongle Variant (Weeks 19–20)

**Goal:** Deliver the portable USB-based gateway variant: dual-mode USB (Linux boot + Windows
auto-launch), hot-unplug safety, LED status indicator, USB-specific storage management, and
recovery scan — completing US-12 and US-13.

**Exit criteria:** USB boots in Linux mode on 3+ PC models; Windows auto-launch works on Win10/11;
hot-unplug detection triggers graceful shutdown; recovery scan handles interrupted studies; LED
reflects gateway status; disk full management prevents OOM; K9/K10 perf gates met.

**PRD refs:** N/A (new variant).
**Spec refs:** usb-dongle-gateway-spec.md (all sections).

**Per-sprint gate:** S01-T8 validated USB boot compatibility; S09-T9 established perf baselines;
S06-T7 provides config import/export for USB deployment.

| ID | Task | Spec ref | RED → GREEN | DoD | Status |
|----|------|----------|-------------|-----|--------|
| S10-T1 | **USB partition layout tool (no TDD):** script to create dual-partition USB — P1 (Linux boot, 4GB ext4), P2 (Windows auto-launch, 8GB NTFS), P3 (shared data, exFAT); tested on Windows 10/11 + Linux | usb-dongle-spec §4 | `tests/test_usb_partition.py` — partition layout validated; sizes within bounds; script creates bootable USB | USB can be flashed with one command; admin can create USB in <15 min | ☐ |
| S10-T2 | **Linux boot environment (no TDD):** Alpine Linux minimal + gateway binary + Python 3.12 runtime on P1; systemd service auto-starts gateway on boot; network config via DHCP or static | usb-dongle-spec §4.1, §5.1 | Boot test on 3+ PC models (UEFI + legacy BIOS); gateway starts and web UI accessible | Linux mode boots and starts gateway in ≤30 s (K9) | ☐ |
| S10-T3 | **Windows auto-launch (no TDD):** portable Python + gateway exe + scheduled task (not autorun.inf — blocked by default since Win7) on P2; auto-start on USB plug-in; tray icon in system tray | usb-dongle-spec §4.1, §5.1 | Auto-launch test on Windows 10/11; gateway starts without reboot; tray icon visible | Windows mode starts in ≤15 s (K9) | ☐ |
| S10-T4 | **Hot-unplug detection:** udev rule (Linux) monitoring `/sys/block/sdX/device/remove`; `WM_DEVICECHANGE` (Windows) monitoring `DBT_DEVICEREMOVECOMPLETE`; triggers graceful shutdown sequence | usb-dongle-spec §7 | `tests/test_hot_unplug.py` — removal triggers shutdown sequence (receiver.stop → flush → fsync → shutdown marker); timeout after 10s allows forced removal | Graceful shutdown works in both modes; flush ≤10 s (K10) | ☐ |
| S10-T5 | **USB-specific config defaults:** `usb_mode` section in `GatewayConfig` model; `usb_mode.enabled` auto-detected; `retention_delivered_hours: 24` (aggressive); `storage_budget_gb: 18`; `hot_unplug_safe: true`; `led_enabled: false` | usb-dongle-spec §6 | Config model tests pass; USB defaults applied when `usb_mode.enabled=true`; auto-detection works | Config auto-detects USB mode | ✅ |
| S10-T6 | **LED status indicator:** GPIO control for Linux mode (BCM pin from config); USB HID control if device supports; state machine: 🔵 idle, 🟢 receiving/forwarding, 🟡 error/retry, 🔴 critical, ⚪ safe to remove, ⚫ off | usb-dongle-spec §3.3 | `tests/test_led.py` — LED state changes per gateway status; GPIO pin configurable; graceful fallback when no LED hardware | LED reflects receiver/forwarder status (or no-op if no hardware) | ☐ |
| S10-T7 | **Disk full management:** 90% capacity warning (web UI banner + LED 🟡); auto-purge oldest DELIVERED studies when capacity exceeded; undelivered/FAILED studies never purged; capacity dashboard in web admin | usb-dongle-spec §5.3 | `tests/test_disk_full.py` — purge triggered at 90%; delivered studies removed oldest-first; undelivered retained; web UI shows capacity | USB doesn't run out of space; PRD §3.4 retention preserved | ☐ |
| S10-T8 | **Recovery scan on USB boot:** detect previous shutdown marker; scan spool dir vs DB; mark incomplete studies as RECEIVED (eligible for re-forward); clear shutdown marker; log recovery actions | usb-dongle-spec §7.3 | `tests/test_usb_recovery.py` — interrupted study recovered; partial study marked incomplete; marker cleared; recovery logged | USB survives unclean removal; studies recoverable | ☐ |
| S10-T9 | **USB flashing documentation + script (no TDD):** step-by-step guide for creating USB dongles; includes Linux boot, Windows auto-launch, and shared data setup; template config for common modality/hub combos | usb-dongle-spec §8.2 | Docs complete; script tested on Windows 10/11; template configs validated | Admin can create USB dongle in <15 min; guide reviewed by non-technical user | ☐ |
| S10-T10 | **USB-specific perf tests (no TDD):** benchmark boot → gateway ready (Linux + Windows); hot-unplug flush time; recovery scan time; DICOM write throughput to USB; record against K9/K10 | usb-dongle-spec §10 | perf report in `docs/qa/usb-perf-10.md`; K9/K10 gates pass or decision filed | Performance targets met or documented | ☐ |
| S10-T11 | **USB variant UAT (no TDD):** scripted walkthrough — flash USB, plug into PC, boot Linux mode, wizard, first study forwarded, hot-unplug, re-plug, recovery, switch to Windows mode | usb-dongle-spec §10 | UAT checklist + timing in `docs/qa/usb-uat-10.md` | USB variant formally validated | ☐ |

**Evidence:** _(links to commits/PRs when done)_

- S10-T5 ✅ — `feat:` commit (config S10-T5): `is_removable_volume()` / `detect_usb_mode()`
  (Linux: `/proc/mounts` → `/sys/class/block/<dev>/removable`; Windows: `GetDriveTypeW`),
  `apply_usb_defaults()` (purge-on-disk-full, 90 % warning, spool budget = `storage_budget_gb`),
  hour-granular retention (`usb_mode.retention_delivered_hours` wins in `Spool.purge_delivered`),
  main.py auto-detect on startup before the pipeline is built. 10 new config tests + 2 new
  retention tests — suite 530 passed / 4 skipped; `ruff check .` and `mypy .` (56 files) clean.

**Notes:**
- S10-T1/T2/T3 are packaging/environment tasks — they don't change the gateway Python code, only
  how it's deployed. The gateway code from Sprints 01–09 runs unchanged on the USB.
- S10-T4 (hot-unplug) is the critical safety feature. The udev/WM_DEVICECHANGE monitoring runs as
  a background thread in the gateway process. The shutdown sequence is: stop receiver → wait for
  in-flight (10s timeout) → flush audit → fsync → write marker → allow removal.
- S10-T5 adds the `usb_mode` config section to the existing `GatewayConfig` model. When
  `usb_mode.enabled=true`, the gateway applies USB-specific defaults (aggressive retention, storage
  budget, hot-unplug monitoring). Auto-detection checks if the spool directory is on a removable
  volume.
- S10-T6 (LED) is best-effort — most off-the-shelf USB drives don't have LEDs. The implementation
  is a no-op when no LED hardware is detected. Custom embedded boards (v1.1+) will have proper LED
  support.
- S10-T7 (disk full management) is critical for the 32-64GB USB constraint. The 90% threshold
  triggers proactive purging. The web admin panel shows a capacity gauge.
- S10-T8 (recovery) reuses the existing recovery scan from Sprint 02 (S02-T6) with USB-specific
  shutdown marker detection.
- Standalone hardware mode (Raspberry Pi, external power) is **not** in this sprint — deferred to
  v1.1+ per the refinement spec.
