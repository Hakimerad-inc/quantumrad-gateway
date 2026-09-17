# mercure Gateway — USB Dongle Quick-Start (Sprint 10)

## What is the USB dongle variant?

The USB dongle is a **portable gateway** that runs from a standard USB 3.0
flash drive. It supports two modes:

- **Linux mode** — boot a PC from the USB drive (the gateway starts
  automatically and runs as a self-contained appliance).
- **Windows mode** — plug the USB drive into a running Windows PC; the gateway
  launches automatically and runs alongside the Windows session.

## Prerequisites

- A USB 3.0 flash drive (≥32 GB recommended; 16 GB minimum)
- A PC with USB boot support (Linux mode) or Windows 10/11 (Windows mode)
- The USB flashing script (`scripts/flash_usb.sh`) and the gateway AppImage
  (Linux) or installer (Windows), obtained from the release page

## First-time setup

1. **Flash the USB drive** — run the flashing script with the gateway build
   artifact and the target USB device (e.g. `/dev/sdb`). **This erases the
   drive.**
2. **Boot (Linux mode)** — insert the USB, reboot, and select the USB drive
   from the boot menu. The gateway starts automatically.
3. **Auto-launch (Windows mode)** — insert the USB into a running Windows PC;
   the autorun starts the gateway. The web admin panel opens at
   `http://localhost:8080`.

## What happens on the first boot

The gateway runs a **recovery scan** to reconcile any files on the USB's spool
partition with the database. Delivered studies from a previous session are
retained and re-queued if needed. The LED shows:

- **Solid green** — gateway running, no issues
- **Blinking green** — forwarding in progress
- **Solid red** — disk full or critical error
- **Blinking red** — shutdown in progress (hot-unplug detected)

## Operating safely

- **Always shut down the gateway before unplugging the USB** — use the web
  admin panel's **Stop** button, or wait for the LED to go solid amber
  (shutdown complete). The gateway flushes the spool and writes a shutdown
  marker.
- **Hot-unplug protection** — if the USB is removed unexpectedly, the gateway
  detects it, writes a best-effort shutdown marker, and stops the receiver and
  forwarder. On the next boot, the recovery scan picks up any interrupted
  studies.
- **Storage budget** — the USB partition enforces a capacity budget (default
  20 GB). When the warning threshold is reached (default 90%), the gateway
  emits a warning. When `purge_on_disk_full` is enabled, the oldest delivered
  studies are purged automatically.

## Recovery after a crash

If the USB is removed without a clean shutdown (power loss, USB yanked):

1. Insert the USB into any PC and boot (Linux mode) or auto-launch (Windows).
2. The gateway detects the missing shutdown marker and runs the **recovery
   scan**.
3. Any study that was in-flight (received but not yet forwarded) is re-queued.
4. The database is verified and repaired if needed.

## Performance targets

| Metric | Target |
|--------|--------|
| Cold boot → gateway ready (Linux) | ≤30 seconds |
| Cold boot → gateway ready (Windows) | ≤15 seconds |
| Hot-unplug flush (LED to shutdown) | ≤10 seconds |
| Recovery scan (1000 studies) | ≤30 seconds |

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| PC won't boot from USB | Check BIOS boot order; disable Secure Boot; try a different USB port |
| Windows auto-launch doesn't fire | Double-click `autorun.exe` on the USB; check Windows AutoPlay settings |
| LED stays red | Disk full — connect the web admin panel and purge delivered studies |
| Gateway won't start (Linux) | Check `dmesg` for USB errors; re-flash the USB drive |
| Gateway won't start (Windows) | Re-run the installer from the USB; check Windows Event Viewer |
