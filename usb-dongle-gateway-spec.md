# mercure Gateway — USB Dongle Variant Spec

**Status:** Draft
**Date:** 2026-08-29
**Source:** Stakeholder interview rounds 5–7 on portable USB gateway variant
**Parent spec:** `product-refinement-spec.md`

---

## 1. Executive Summary

A **portable USB-based DICOM gateway** that runs the mercure-gateway software from a USB flash drive. The device operates in dual mode (bootable Linux + Windows auto-launch), connects to modalities via the hospital/clinic network, receives studies throughout the work day, and forwards them to a central hub or PACS. Designed for small clinics that cannot dedicate a PC to the gateway role.

**Core value proposition:** Plug the USB into any available PC → the gateway starts → receives studies from the modality network → forwards to hub/PACS → safely eject at end of day.

---

## 2. Product Variant Overview

| Field | Value |
|-------|-------|
| **Product** | mercure-gateway USB Dongle Edition |
| **Form factor** | USB 3.0 flash drive (32–64 GB) with dual partitions |
| **Boot modes** | 1) Bootable Linux (Alpine/Ubuntu minimal) — dedicated gateway environment |
| | 2) Windows auto-launch — runs alongside existing OS |
| **Deployment** | One USB device per site; plugs into any workstation |
| **Target users** | Same as parent spec (single person, multiple hats) |
| **Network** | Connects to modality via hospital LAN (Ethernet or WiFi via PC) |
| **Offline window** | Full work day (8+ hours); receives and buffers studies |
| **Standalone mode** | Deferred to v1.1 (no external hardware for MVP) |
| **LED indicator** | Hardware LED on USB device (if supported by device firmware) |

---

## 3. Hardware Requirements

### 3.1 USB Device Specifications

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| **Storage** | 32 GB USB 3.0 | 64 GB USB 3.0/3.1 |
| **Read speed** | ≥100 MB/s | ≥200 MB/s |
| **Write speed** | ≥50 MB/s | ≥100 MB/s |
| **USB standard** | USB 3.0 (Type-A) | USB 3.1 Gen 1 |
| **LED** | Single RGB LED (status indicator) | RGB LED + tactile button (safe eject) |
| **Form factor** | Standard USB flash drive | Compact, ruggedized |
| **Power** | USB bus-powered (≤900mA for USB 3.0) | — |

### 3.2 Recommended Devices (examples)

| Device | Storage | Speed | LED | Notes |
|--------|---------|-------|-----|-------|
| Samsung BAR Plus | 64 GB | 300/60 MB/s | No LED | Rugged, no LED |
| SanDisk Extreme Pro | 64 GB | 420/380 MB/s | No LED | Fastest available |
| Kingston IronKey D500S | 64 GB | 250/120 MB/s | No LED | Hardware encrypted |
| Custom embedded | 64 GB eMMC | 100/50 MB/s | Yes (custom) | Best for LED/button; requires custom PCB |

**Note:** For the LED indicator, a custom embedded device with ARM SoC + eMMC storage may be needed if off-the-shelf USB drives don't have LEDs. The MVP can ship without LED; LED is v1.1 enhancement.

### 3.3 LED Status Colors (if hardware supports)

| Color | State |
|-------|-------|
| 🔵 Blue (slow blink) | Idle / waiting for studies |
| 🟢 Green (solid) | Actively receiving/forwarding |
| 🟡 Yellow (fast blink) | Error / retry in progress |
| 🔴 Red (solid) | Critical error / disk full |
| ⚪ White (pulse) | Safe to remove |
| ⚫ Off | Device not powered |

---

## 4. USB Partition Layout

```
┌─────────────────────────────────────────────────────────────────┐
│ USB Flash Drive (32–64 GB)                                       │
├──────────────────┬──────────────────┬────────────────────────────┤
│ Partition 1      │ Partition 2      │ Partition 3                │
│ Linux Boot (EFI) │ Windows Auto-Launch│ Shared Data (FAT32/exFAT)│
│ 4 GB ext4        │ 8 GB NTFS        │ Remainder (exFAT)         │
│                  │                  │                            │
│ Alpine Linux     │ Portable Windows │ - mercure-gateway.json     │
│ + gateway binary │ environment +    │ - spool/ (DICOM files)     │
│ + Python runtime │ gateway exe      │ - reports/                 │
│ + config         │ + config         │ - audit/                   │
│                  │                  │ - logs/                    │
└──────────────────┴──────────────────┴────────────────────────────┘
```

### 4.1 Partition Details

| Partition | Filesystem | Size | Contents | Purpose |
|-----------|------------|------|----------|---------|
| **P1: Linux Boot** | ext4 | 4 GB | Alpine Linux minimal + gateway binary + Python 3.12 runtime | Dedicated gateway environment (no interference with host PC) |
| **P2: Windows Auto-Launch** | NTFS | 8 GB | Portable Python + gateway app + autorun.inf + config | Auto-launch on existing Windows; no reboot needed |
| **P3: Shared Data** | exFAT | Remainder | `mercure-gateway.json`, `spool/`, `reports/`, `audit/`, `logs/` | Shared between both modes; survives mode switches; accessible from any OS |

### 4.2 Boot Selection

| Scenario | Boot behavior |
|----------|---------------|
| PC BIOS set to boot from USB | P1 (Linux) boots; gateway runs in dedicated environment |
| PC boots normally (Windows) | P2 auto-launches gateway via `autorun.inf` or scheduled task |
| User presses USB button (if available) | Toggles default boot partition |
| USB plugged into Mac/Linux | P3 mounted as data; gateway can be run manually |

---

## 5. Architecture

### 5.1 Dual-Mode Gateway

```
┌─────────────────────────────────────────────────────────────────────┐
│ USB Dongle Gateway — Dual Mode                                      │
│                                                                     │
│  ┌─────────────────────┐    ┌─────────────────────┐                │
│  │ Mode A: Linux Boot  │    │ Mode B: Windows Auto │                │
│  │ (Dedicated env)     │    │ (Host OS)            │                │
│  │                     │    │                      │                │
│  │ Alpine Linux        │    │ Portable Python 3.12 │                │
│  │ Python 3.12         │    │ Gateway app          │                │
│  │ Gateway app         │    │ Tray icon / service  │                │
│  │ systemd service     │    │                      │                │
│  └─────────┬───────────┘    └──────────┬───────────┘                │
│            │                           │                            │
│            └───────────┬───────────────┘                            │
│                        ▼                                            │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ Shared Data (P3 — exFAT)                                     │   │
│  │                                                               │   │
│  │  mercure-gateway.json  ← config (encrypted credentials)       │   │
│  │  spool/               ← DICOM files (study storage)          │   │
│  │  reports/             ← retrieved reports                     │   │
│  │  audit/               ← audit log (chained hash)             │   │
│  │  logs/                ← application logs                      │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                        │                                            │
│                        ▼                                            │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ FastAPI Backend + Web UI (localhost:8080)                     │   │
│  │                                                               │   │
│  │  REST API  ←→  Receiver (C-STORE SCP)  ←→  Modalities        │   │
│  │  REST API  ←→  Forwarder (concurrent)   →→  Hub/PACS         │   │
│  │  REST API  ←→  ReportRetriever          ←←  PACS             │   │
│  │  REST API  ←→  AuditLog (chained hash)                       │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

### 5.2 Network Connectivity

The USB gateway uses the host PC's network stack:

| Connection | Mode A (Linux Boot) | Mode B (Windows Auto) |
|------------|---------------------|----------------------|
| **Ethernet** | Host PC's NIC (bridged or passthrough) | Host PC's NIC (shared) |
| **WiFi** | Host PC's WiFi adapter | Host PC's WiFi adapter |
| **DICOM SCP port** | Bound to 0.0.0.0:11112 or specific IP | Same |
| **Web UI** | localhost:8080 | localhost:8080 |

**Mode A (Linux boot):** The USB Linux environment takes over the host PC's network interfaces. The gateway binds directly to the NIC.

**Mode B (Windows auto-launch):** The gateway runs as a Windows process, sharing the host PC's network stack. May require Windows Firewall rule for DICOM port (11112).

### 5.3 Storage Budget

For a 32 GB USB with 8 GB system + 4 GB Linux:

| Item | Size | Notes |
|------|------|-------|
| System (Linux + Windows partitions) | 12 GB | Fixed |
| Available for data | ~20 GB | Shared data partition |
| Avg CT study | ~200 MB | 100 MB compressed |
| Avg MR study | ~400 MB | 200 MB compressed |
| Avg X-ray study | ~10 MB | — |
| Avg ultrasound | ~20 MB | — |
| **Studies at capacity** | ~50-100 | Depends on modality mix |

**Retention strategy:**
- Studies forwarded AND confirmed delivered → eligible for deletion after `retention_delivered_days` (default: 1 day for USB variant)
- Studies NOT forwarded → never auto-deleted (PRD §3.4 principle)
- Disk full warning at 90% capacity → oldest delivered studies purged first
- Configurable `max_spool_gb` (default: 18 GB for 32 GB USB)

---

## 6. Configuration Model Updates

> **Implementation status:** ✅ All USB config fields implemented in `USBModeConfig` model (7 fields); `StorageConfig` updated with disk-full settings.

### 6.1 New: USB-specific config section

```jsonc
// Addition to mercure-gateway.json
{
  // ... existing fields ...
  "usb_mode": {
    "enabled": false,              // true when running from USB
    "storage_budget_gb": 18,       // max data partition usage
    "retention_delivered_hours": 24, // aggressive cleanup for USB (hours, not days)
    "hot_unplug_safe": true,       // enable graceful shutdown on USB removal
    "led_enabled": false,          // hardware LED support
    "auto_start_on_boot": true,    // auto-start gateway on USB boot/plug-in
    "led_pin": "GPIO18"            // GPIO pin for LED (Linux mode; BCM numbering)
  }
}
```

### 6.2 Updated: storage section

```jsonc
"storage": {
  "spool_dir": "spool",           // relative to USB data partition
  "max_spool_gb": 18,             // USB-specific: aggressive limit
  "retention_delivered_days": 1,   // USB-specific: 1 day (24 hours)
  "disk_full_warning_pct": 90,    // warn at 90% capacity
  "purge_on_disk_full": true      // auto-purge oldest delivered on disk full
}
```

---

## 7. Hot-Unplug Safety System

> **Implementation status:** ✅ `HotplugDetector` class implemented in `src/mercure_gateway/hotplug.py` (daemon thread, cross-platform stat fallback, udev/sysfs on Linux). 10 tests passing.

### 7.1 Detection Mechanism

**Mode A (Linux boot):**
- Monitor `/sys/block/sdX/device/remove` uevent
- Alternatively: `udev` rule triggers on USB disconnect
- On detection: initiate graceful shutdown sequence

**Mode B (Windows auto-launch):**
- Monitor `WM_DEVICECHANGE` / `DBT_DEVICEREMOVECOMPLETE` Windows messages
- Alternatively: poll USB drive health via SMART (if supported)
- On detection: initiate graceful shutdown sequence

### 7.2 Shutdown Sequence

```
USB Removal Detected
  │
  ├─ 1. Set LED to ⚪ white pulse (if available)
  ├─ 2. Stop accepting new DICOM associations (receiver.stop())
  ├─ 3. Wait for in-flight studies to complete (timeout: 10s)
  ├─ 4. Flush any pending audit events
  ├─ 5. fsync all open file handles
  ├─ 6. Write shutdown marker to data partition
  ├─ 7. Allow USB removal
  │
  └─ If timeout (10s):
       ├─ Mark in-flight studies as INCOMPLETE
       ├─ Write recovery marker
       └─ Allow removal (fail-safe: PRD §3.4)
```

### 7.3 Recovery on Next Boot

> **Implementation status:** ✅ `recover()` function implemented in `src/mercure_gateway/recovery.py` (scans spool, reconciles DB, handles interrupted studies). 10 tests passing.

On startup, the gateway scans the data partition:

1. Check for shutdown marker from previous session
2. If present: run recovery scan (PRD §5.2 step 2)
3. Mark incomplete studies as RECEIVED (eligible for re-forwarding)
4. Clear shutdown marker
5. Resume normal operation

### 7.4 User Notification

| Method | Trigger | Message |
|--------|---------|---------|
| Web UI banner | USB removal detected | "Gateway shutting down safely. You may remove the USB device." |
| Tray icon (Mode B) | USB removal detected | Icon turns white; tooltip: "Safe to remove" |
| LED (if available) | Shutdown sequence | White pulse → off |

---

## 8. Auto-Configuration

### 8.1 First-Boot Wizard (Web UI)

When the USB is first plugged in (no `mercure-gateway.json` on data partition):

1. Gateway starts with defaults
2. Web UI opens `localhost:8080/setup`
3. Step-by-step wizard:
   - **Step 1:** Appliance name + network settings
   - **Step 2:** DICOM receiver config (port, AET — defaults: 11112, `GATEWAY`)
   - **Step 3:** Destination(s) — mercure hub or PACS (host/port/AET)
   - **Step 4:** Report retrieval (optional)
   - **Step 5:** Connectivity test (echo to each destination)
   - **Step 6:** Save config to data partition → done
4. Config persisted to `mercure-gateway.json` on P3 (shared data)
5. Subsequent boots skip wizard

### 8.2 Config Import/Export

| Feature | Implementation |
|---------|---------------|
| **Import** | Admin copies `mercure-gateway.json` to USB data partition from another source |
| **Export** | Web UI → Settings → Export downloads config as JSON |
| **USB-to-USB copy** | Admin copies config between USBs (same clinic, multiple gateways) |
| **Template configs** | Pre-built config templates on USB for common modality/hub combinations |

### 8.3 Network Auto-Detection

On startup, the gateway can optionally scan the local network:

1. Attempt DICOM C-ECHO to known hub/PACS addresses (from config)
2. If no config: listen for DICOM associations on port 11112 (any source)
3. Log discovered devices in web UI for admin to confirm/configure

---

## 9. Updated Sprint Impact

### 9.1 New Sprint: Sprint 10 — USB Dongle Variant (Weeks 19–20)

| ID | Task | PRD ref | RED → GREEN | DoD | Status |
|----|------|---------|-------------|-----|--------|
| S10-T1 | **USB partition layout tool:** script to create dual-partition USB (Linux boot + Windows auto + shared data) | New | `tests/test_usb_partition.py` — partition layout validated; sizes within bounds | USB can be flashed with one command | ☐ |
| S10-T2 | **Linux boot environment:** Alpine Linux minimal + gateway binary + Python runtime on P1 | New | Boot test on 3 different PC models | Linux mode boots and starts gateway | ☐ |
| S10-T3 | **Windows auto-launch:** Portable Python + gateway exe + autorun.inf on P2 | New | Auto-launch test on Windows 10/11 | Gateway starts on USB plug-in without reboot | ☐ |
| S10-T4 | **Hot-unplug detection:** udev (Linux) + WM_DEVICECHANGE (Windows) monitoring | New | `tests/test_hot_unplug.py` — removal triggers shutdown sequence; recovery on restart | Graceful shutdown works in both modes | ☐ |
| S10-T5 | **USB-specific config defaults:** `usb_mode` section in config model; aggressive retention; storage budget | New | Config model tests pass; USB defaults applied when `usb_mode.enabled=true` | Config auto-detects USB mode | ☐ |
| S10-T6 | **LED status indicator:** GPIO control (Linux mode); USB HID control (if device supports) | New | `tests/test_led.py` — LED state changes per gateway status | LED reflects receiver/forwarder status | ☐ |
| S10-T7 | **Disk full management:** 90% warning; auto-purge oldest delivered studies; capacity dashboard in web UI | New | `tests/test_disk_full.py` — purge triggered; delivered studies removed; undelivered retained | USB doesn't run out of space | ☐ |
| S10-T8 | **Recovery scan on USB boot:** detect previous shutdown; reconcile spool states; clear markers | New | `tests/test_usb_recovery.py` — interrupted study recovered; partial study marked incomplete | USB survives unclean removal | ☐ |
| S10-T9 | **USB flashing documentation + script:** step-by-step guide for creating USB dongles; includes Linux boot, Windows auto-launch, and shared data setup | New | Docs complete; script tested on Windows 10/11 | Admin can create USB dongle in <15 min | ☐ |

### 9.2 Updated Existing Sprints

| Sprint | Change |
|--------|--------|
| S01 (Phase 0) | Add: USB variant prototype — test auto-launch on Windows; test Linux boot on 3 PC models |
| S03 (Forwarder) | Update: concurrent forwarding must handle USB storage constraints (disk budget) |
| S06 (Desktop shell) | Update: web UI must detect USB mode and show USB-specific status (capacity, LED, hot-unplug) |
| S09 (Hardening) | Add: USB-specific perf tests — boot time, auto-launch time, hot-unplug recovery time |

---

## 10. Testing Strategy

### 10.1 USB-Specific Tests

| Test | Type | Description |
|------|------|-------------|
| USB boot test | Manual | Boot Linux from USB on 3+ PC models; verify gateway starts |
| Auto-launch test | Manual | Plug USB into Windows 10/11; verify gateway starts without reboot |
| Hot-unplug recovery | Automated | Simulate USB removal (device disconnect); verify recovery on restart |
| Disk full purge | Automated | Fill spool to 90%; verify oldest delivered purged; undelivered retained |
| Config persistence | Automated | Write config → reboot → config survives |
| Cross-mode config | Manual | Write config in Linux mode → boot Windows mode → config intact |
| LED state machine | Automated | Verify LED colors match gateway states |
| Storage budget | Automated | 50 studies on 32GB USB → verify no OOM; verify forwarding still works |
| Recovery scan | Automated | Create partial studies → simulate crash → verify recovery on restart |

### 10.2 Performance Gates (USB-specific)

| Metric | Target | Notes |
|--------|--------|-------|
| Linux boot → gateway ready | ≤30 s | From USB insertion to web UI accessible |
| Windows auto-launch → ready | ≤15 s | From USB insertion to web UI accessible |
| Hot-unplug flush time | ≤10 s | Time from detection to safe removal |
| Recovery scan time | ≤5 s | Time to scan and reconcile on restart |
| DICOM write throughput (USB) | ≥20 MB/s | Sustained write to USB storage |
| Idle RAM (Linux mode) | ≤100 MB | Must fit in 512MB RAM devices |
| Idle RAM (Windows mode) | ≤150 MB | Same as PRD §5.6 K6 |

---

## 11. Risks & Mitigations

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| USB write speed insufficient for high-volume studies | Performance | Med | Benchmark in Phase 0; recommend USB 3.1+ with ≥50 MB/s write |
| Auto-launch blocked by Windows security (UAC, SmartScreen) | UX | High | Sign the executable; document UAC bypass; provide manual start option |
| Linux boot fails on certain PC models (UEFI vs BIOS) | Compatibility | Med | Test on 5+ PC models; provide both UEFI and legacy BIOS boot options |
| exFAT shared partition has file locking issues (concurrent access) | Correctness | Low | Gateway is single-process; no concurrent file access across modes |
| USB device wears out from constant writes | Reliability | Med | Use industrial-grade USB with high TBW; monitor SMART health |
| Hot-unplug detection unreliable on some USB controllers | Safety | Med | Fallback: periodic fsync + shutdown marker; recovery scan handles crash |
| LED hardware not available on off-the-shelf USB drives | UX | High | LED is v1.1 enhancement; MVP works without LED |
| Windows autorun disabled by default (since Windows 7) | UX | High | Provide manual start option; document how to enable autorun via Group Policy |

---

## 12. Open Questions

| # | Question | Impact | Default |
|---|----------|--------|---------|
| 1 | **USB device selection** — standard flash drive vs. custom embedded board with LED/button? | BOM cost, timeline | Standard flash drive for MVP; custom board for v1.1 |
| 2 | **Linux distro** — Alpine (smallest) vs. Ubuntu minimal (better driver support)? | Boot size, compatibility | Alpine for 4GB partition; Ubuntu if driver issues |
| 3 | **Windows auto-launch method** — autorun.inf, scheduled task, or Windows service? | Reliability | Scheduled task (most reliable post-Windows 7) |
| 4 | **USB write endurance** — how many write cycles before the device wears out? | Reliability | Industrial USB (SLC/MLC NAND); monitor SMART in v1.1 |
| 5 | **Dual-mode config sync** — should both modes share the same config, or separate? | Complexity | Shared (P3); both modes read/write same `mercure-gateway.json` |
| 6 | **USB encryption** — should the data partition be encrypted (BitLocker, LUKS)? | Security | Optional; not MVP (interview round 2: nice-to-have) |
| 7 | **USB cloning** — how to mass-produce USB dongles for multiple clinics? | Operations | Disk image tool; document in S10-T9 |
| 8 | **Modality network discovery** — auto-detect modality IP/AET or manual config? | UX | Manual config (MVP); auto-discover in v1.1 |

---

*End of USB dongle variant spec. This document extends `product-refinement-spec.md` and should be used alongside it for implementation planning.*
