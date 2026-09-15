#!/usr/bin/env bash
# flash_usb.sh — create the dual-mode USB dongle layout (usb-dongle-spec §4, S10-T9).
#
# Partition layout (GPT, on a >=32GB USB 3.0 stick — the drive is ERASED):
#   P1  ext4   4 GB   Linux boot (Alpine + gateway binary; systemd auto-start)
#   P2  NTFS   8 GB   Windows auto-launch (portable Python + gateway + task)
#   P3  exFAT  rest   Shared data (config, spool/, reports/, audit/, logs/)
#
# Safety: refuses rotational disks, mounted targets, and system disks; the
# device must be given explicitly (no globs, no guesses). --dry-run plans the
# layout and prints every privileged command without touching any device.
#
# Usage:
#   scripts/flash_usb.sh --device /dev/sdX [--appimage FILE] [--force]
#   scripts/flash_usb.sh --device /dev/loop0 --dry-run
set -euo pipefail

# shellcheck disable=SC2015
log()  { printf '[flash_usb] %s\n' "$*" >&2; }
die()  { printf '[flash_usb] ERROR: %s\n' "$*" >&2; exit 1; }

P1_GB=4            # Linux boot (ext4)
P2_GB=8            # Windows auto-launch (NTFS)
MIN_DISK_BYTES=$((30 * 1024 * 1024 * 1024))   # >=32GB stick, 30GB tolerance
GB=$((1024 * 1024 * 1024))

DEVICE=""
APPIMAGE=""
DRY_RUN=0
FORCE=0

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --device)   DEVICE="${2:-}"; shift 2 ;;
    --appimage) APPIMAGE="${2:-}"; shift 2 ;;
    --dry-run)  DRY_RUN=1; shift ;;
    --force)    FORCE=1; shift ;;
    -h|--help)  usage 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

[ -n "$DEVICE" ] || die "--device is required (e.g. /dev/sdb); refusing to guess"
case "$DEVICE" in /dev/*) : ;; *) die "$DEVICE: pass an explicit /dev/... node" ;; esac

# run/print: privileged commands are echoed in dry-run, executed otherwise.
run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] %s\n' "$*" >&2
  else
    log "+ $*"
    "$@"
  fi
}

PLAN_BYTES=0
if [ "$DRY_RUN" -eq 0 ]; then
  [ -e "$DEVICE" ] || die "$DEVICE does not exist"
  BASE="$(lsblk -ndo PKNAME "$DEVICE" 2>/dev/null || true)"
  [ -n "$BASE" ] || BASE="$(basename "$DEVICE")"

  # Refuse rotational disks: this is a USB-flash design (S10-T7 budget assumes
  # flash latency); HDDs also take far too long to wipe+mkfs for a <15 min flash.
  ROTA="$(lsblk -dno ROTA "/dev/$BASE" 2>/dev/null || echo 1)"
  if [ "$ROTA" = "1" ] && [ "$FORCE" -ne 1 ]; then
    die "/dev/$BASE reports rotational — flash targets USB sticks; use --force to override"
  fi

  # Refuse any disk that currently backs the running system.
  if lsblk -nro MOUNTPOINT "/dev/$BASE" 2>/dev/null | grep -qE '^/$|/boot'; then
    die "/dev/$BASE hosts the running system — absolutely not"
  fi

  # Refuse mounted partitions of the target (unless --force).
  if [ "$FORCE" -ne 1 ] && lsblk -nro MOUNTPOINT "/dev/$BASE" 2>/dev/null | grep -q .; then
    die "/dev/$BASE has mounted partitions; unmount first or use --force"
  fi

  SIZE_BYTES="$(blockdev --getsize64 "/dev/$BASE" 2>/dev/null || echo 0)"
  if [ "$SIZE_BYTES" -lt "$MIN_DISK_BYTES" ] && [ "$FORCE" -ne 1 ]; then
    die "disk is $((SIZE_BYTES / GB)) GB; layout needs a >=32GB stick (or --force)"
  fi
  [ "$SIZE_BYTES" -gt 0 ] && PLAN_BYTES="$SIZE_BYTES"
fi
# Dry-run (or a device lsblk can't size): plan for a nominal 32 GB stick —
# usb_layout.py rejects anything below 4+8+1 GB regardless.
[ "$PLAN_BYTES" -gt 0 ] || PLAN_BYTES=$((32 * GB))
P3_GB=$(( (PLAN_BYTES - (P1_GB + P2_GB) * GB) / GB ))
[ "$P3_GB" -ge 1 ] || die "no room for the exFAT data partition on this size"

log "planned layout on $DEVICE:"
log "  P1 ext4  ${P1_GB}G (Linux boot) | P2 NTFS ${P2_GB}G (Windows) | P3 exFAT ${P3_GB}G (data)"

if [ -n "$APPIMAGE" ] && [ ! -f "$APPIMAGE" ]; then
  die "--appimage: $APPIMAGE not found"
fi

# ── wipe + partition (GPT via sfdisk; layout owned by scripts/usb_layout.py) ─

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LAYOUT_PY="$SCRIPT_DIR/usb_layout.py"
[ -f "$LAYOUT_PY" ] || die "missing $LAYOUT_PY (ships with this script)"

if [ "$DRY_RUN" -eq 1 ]; then
  printf '[dry-run] wipefs -a %s\n' "$DEVICE" >&2
  printf '[dry-run] sfdisk %s <<EOF (layout from usb_layout.py)\n' "$DEVICE" >&2
  python3 "$LAYOUT_PY" --sfdisk-script "$PLAN_BYTES" | sed 's/^/[dry-run]   /' >&2
else
  run wipefs -a "$DEVICE"
  log "partitioning $DEVICE"
  python3 "$LAYOUT_PY" --sfdisk-script "$PLAN_BYTES" | sfdisk -q "$DEVICE"
  partprobe "$DEVICE" 2>/dev/null || true
  udevadm settle 2>/dev/null || true
fi

# ── filesystems ─────────────────────────────────────────────────────────────

mapfile -t PARTS < <(python3 "$LAYOUT_PY" --partition-paths "$DEVICE")
P1="${PARTS[0]}"; P2="${PARTS[1]}"; P3="${PARTS[2]}"

# Labels ≤11 chars: the exFAT spec caps volume labels there, and exfatprogs
# hard-fails beyond it ("input string is too long" — root loop e2e 2026-09-15).
run mkfs.ext4 -q -L MERCUREBOOT "$P1"
run mkfs.ntfs -f -L MERCUREWIN  "$P2"   # -f: fast format (full-zero on 8G is slow)
run mkfs.exfat -n MERCUREDATA   "$P3"

if [ "$DRY_RUN" -eq 1 ]; then
  log "dry-run complete — no device was written"
  exit 0
fi

# ── populate P3 (shared data skeleton) ──────────────────────────────────────

MNT="$(mktemp -d)"
cleanup() { umount "$MNT" 2>/dev/null || true; rmdir "$MNT" 2>/dev/null || true; }
trap cleanup EXIT
mount "$P3" "$MNT"
mkdir -p "$MNT/spool" "$MNT/reports" "$MNT/audit" "$MNT/logs"
[ -f "$MNT/mercure-gateway.json" ] || cat > "$MNT/mercure-gateway.json" <<'JSON'
{
  "_comment": "USB dongle template — refine §6 defaults; wizard overwrites on first boot.",
  "general": {"appliance_name": "MERCURE-USB"},
  "storage": {"spool_dir": "spool", "max_spool_gb": 18},
  "usb_mode": {"enabled": true, "retention_delivered_hours": 24, "storage_budget_gb": 18}
}
JSON
log "data skeleton written to $P3 (spool/reports/audit/logs + template config)"

if [ -n "$APPIMAGE" ]; then
  mount "$P1" "$MNT.b" 2>/dev/null || { mkdir -p "$MNT.b"; mount "$P1" "$MNT.b"; }
  install -m 0755 "$APPIMAGE" "$MNT.b/mercure-gateway.AppImage"
  umount "$MNT.b"; rmdir "$MNT.b"
  log "AppImage installed on $P1"
fi

log "done. Boot P1 (UEFI/BIOS USB boot) for Linux mode, or plug into Windows for P2."
