#!/usr/bin/env python
"""USB dongle partition layout planning (usb-dongle-spec §4, S10-T1).

Pure string/arithmetic logic, deliberately free of privileged calls: the
partition table is computed here, consumed by ``scripts/flash_usb.sh`` via the
``--sfdisk-script`` / ``--partition-paths`` CLIs, and unit-tested in
``tests/test_usb_partition.py`` (real ``losetup``/``mkfs`` validation needs
root and is a clean-rig item, per the sprint notes).

Layout (GPT):
  P1  ext4   4 GB   Linux boot
  P2  NTFS   8 GB   Windows auto-launch
  P3  exFAT  rest   shared data (>= MIN_P3_GB or the plan is rejected)
"""

from __future__ import annotations

import argparse
import sys

__all__ = [
    "GB",
    "LayoutError",
    "PartitionPlan",
    "partition_paths",
    "plan_layout",
    "sfdisk_script",
]

GB = 1024**3
P1_BYTES = 4 * GB
P2_BYTES = 8 * GB
MIN_P3_BYTES = 1 * GB  # anything smaller is a mis-sized stick, not a layout

# GPT type UUIDs: Linux filesystem (generic — boot loader installs separately)
# and basic data (used for both the NTFS and exFAT partitions; the codes are
# hints, mkfs is authoritative). sfdisk(8) with a GPT label only accepts the
# key=value script form with full GUIDs — the MBR-style comma triplets
# (",4G,8300,Name") are rejected ("line 2: unsupported command"; caught by
# the root loop-device e2e, tests/test_usb_partition.py).
_TYPE_LINUX = "0FC63DAF-8483-477B-8E3F-431B0C1E2B01"  # Linux filesystem
_TYPE_DATA = "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"  # Microsoft basic data


class LayoutError(ValueError):
    """The requested size cannot host the three-partition layout."""


class PartitionPlan:
    """A computed three-partition layout for a stick of a given size."""

    def __init__(self, size_bytes: int) -> None:
        if size_bytes <= 0:
            raise LayoutError(f"disk size must be positive, got {size_bytes}")
        remainder = size_bytes - P1_BYTES - P2_BYTES
        if remainder < MIN_P3_BYTES:
            raise LayoutError(
                f"disk too small for 4 GB + 8 GB system partitions: "
                f"{size_bytes // GB} GB gives only {remainder // GB} GB data"
            )
        self.size_bytes = size_bytes
        self.p1_bytes = P1_BYTES
        self.p2_bytes = P2_BYTES
        self.p3_bytes = remainder

    @property
    def total_used(self) -> int:
        return self.p1_bytes + self.p2_bytes + self.p3_bytes

    def as_dict(self) -> dict[str, int]:
        return {
            "size": self.size_bytes,
            "p1": self.p1_bytes,
            "p2": self.p2_bytes,
            "p3": self.p3_bytes,
        }


def plan_layout(size_bytes: int) -> PartitionPlan:
    """Validate and describe the §4 layout for a *size_bytes* stick."""
    return PartitionPlan(size_bytes)


def sfdisk_script(plan: PartitionPlan) -> str:
    """sfdisk(8) GPT script (one partition per line; empty start = auto).

    Key=value form with full type GUIDs — the only thing sfdisk accepts for
    GPT labels (see ``_TYPE_LINUX`` note). Sizes carry an explicit ``G``
    suffix: a bare number means *sectors* (root loop e2e, 2026-09-15), and
    sfdisk's binary G equals this module's ``GB`` (1024³).
    """
    return (
        "label: gpt\n"
        f"size={plan.p1_bytes // GB}G, type={_TYPE_LINUX}, name=LinuxBoot\n"
        f"size={plan.p2_bytes // GB}G, type={_TYPE_DATA}, name=WindowsLaunch\n"
        f"size={plan.p3_bytes // GB}G, type={_TYPE_DATA}, name=SharedData\n"
    )


def partition_paths(device: str) -> list[str]:
    """The three partition device nodes for *device*.

    Kernel naming convention: base names ending in a digit (``/dev/nvme0n1``,
    ``/dev/mmcblk0``, ``/dev/loop3``) take a ``p`` separator (``nvme0n1p1``);
    SCSI/ATA-style names ending in a letter (``/dev/sdb``) do not (``sdb1``).
    """
    base = device.rstrip("/")
    separator = "p" if base and base[-1].isdigit() else ""
    return [f"{base}{separator}{i}" for i in (1, 2, 3)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--sfdisk-script",
        type=int,
        metavar="SIZE_BYTES",
        help="print the sfdisk script for a stick of SIZE_BYTES",
    )
    group.add_argument(
        "--partition-paths",
        metavar="DEVICE",
        help="print the 3 partition device nodes for DEVICE, one per line",
    )
    args = parser.parse_args(argv)
    if args.partition_paths is not None:
        for path in partition_paths(args.partition_paths):
            print(path)
        return 0
    try:
        plan = plan_layout(args.sfdisk_script)
    except LayoutError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(sfdisk_script(plan), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
