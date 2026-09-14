"""S10-T1 (RED→GREEN): USB dongle partition layout planning.

The privileged half of the layout work (``losetup``/``sfdisk``/``mkfs`` on a
real or loop device) needs root and is validated on the clean USB rig
(usb-dongle-spec §4 "tested on Windows 10/11 + Linux boot matrix"). This
module tests the *plan*: sizes, sfdisk script text, partition-node naming, and
the flash script's argument-validation/safety paths — which together define
what the on-hardware run must produce.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LAYOUT = REPO / "scripts" / "usb_layout.py"
FLASH = REPO / "scripts" / "flash_usb.sh"

sys.path.insert(0, str(REPO / "scripts"))
import usb_layout  # noqa: E402

GB = usb_layout.GB


# ── plan_layout: sizes per usb-dongle-spec §4 ──────────────────────────────


def test_32gb_stick_layout() -> None:
    plan = usb_layout.plan_layout(32 * GB)
    assert plan.p1_bytes == 4 * GB          # Linux boot, ext4
    assert plan.p2_bytes == 8 * GB          # Windows auto-launch, NTFS
    assert plan.p3_bytes == 20 * GB         # remainder, exFAT (spec: ~20 GB)
    assert plan.total_used == 32 * GB


def test_64gb_stick_layout() -> None:
    plan = usb_layout.plan_layout(64 * GB)
    assert plan.p3_bytes == 52 * GB


@pytest.mark.parametrize("size", [10 * GB, 12 * GB, 4 * GB, 0, -1])
def test_too_small_rejected(size: int) -> None:
    # 12 GB is the spec's exact system-partition budget (4+8) — no room left
    # for data, so it must be rejected alongside nonsense sizes.
    with pytest.raises(usb_layout.LayoutError):
        usb_layout.plan_layout(size)


def test_13gb_is_the_minimum() -> None:
    plan = usb_layout.plan_layout(13 * GB)
    assert plan.p3_bytes == 1 * GB  # ≥1 GB data partition accepted


# ── sfdisk script text (consumed verbatim by flash_usb.sh) ─────────────────


def test_sfdisk_script_shape() -> None:
    plan = usb_layout.plan_layout(32 * GB)
    script = usb_layout.sfdisk_script(plan)
    lines = script.splitlines()
    assert lines[0] == "label: gpt"
    assert lines[1] == ",4G,8300,LinuxBoot"
    assert lines[2] == ",8G,0700,WindowsLaunch"
    assert lines[3] == ",20G,0700,SharedData"


# ── partition device-node naming (kernel convention) ───────────────────────


@pytest.mark.parametrize(
    ("device", "expected"),
    [
        ("/dev/sdb", ["/dev/sdb1", "/dev/sdb2", "/dev/sdb3"]),
        ("/dev/nvme0n1", ["/dev/nvme0n1p1", "/dev/nvme0n1p2", "/dev/nvme0n1p3"]),
        ("/dev/loop7", ["/dev/loop7p1", "/dev/loop7p2", "/dev/loop7p3"]),
        ("/dev/mmcblk0", ["/dev/mmcblk0p1", "/dev/mmcblk0p2", "/dev/mmcblk0p3"]),
    ],
)
def test_partition_paths(device: str, expected: list[str]) -> None:
    assert usb_layout.partition_paths(device) == expected


# ── CLI surface (both flash_usb.sh and the future rig call it this way) ────


def test_cli_sfdisk_script() -> None:
    out = subprocess.run(
        [sys.executable, str(LAYOUT), "--sfdisk-script", str(32 * GB)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.startswith("label: gpt\n,4G,8300,LinuxBoot")


def test_cli_rejects_small_disk() -> None:
    out = subprocess.run(
        [sys.executable, str(LAYOUT), "--sfdisk-script", str(8 * GB)],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 2
    assert "too small" in out.stderr


def test_cli_partition_paths() -> None:
    out = subprocess.run(
        [sys.executable, str(LAYOUT), "--partition-paths", "/dev/loop3"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.split() == ["/dev/loop3p1", "/dev/loop3p2", "/dev/loop3p3"]


# ── flash_usb.sh: safety paths that never touch a device ───────────────────
#
# The script is a bash POSIX utility (sfdisk/mkfs/lsblk live only on Linux);
# the Windows CI runner's Git-Bash shim reaches neither the error paths nor
# the device model these tests assert (first CI run: empty stderr). Skip the
# whole block off POSIX — the loop-device e2e below is already root-gated.
_FLASH_TESTS = pytest.mark.skipif(
    os.name != "posix", reason="flash_usb.sh is a POSIX bash tool (CI-Windows skips)"
)


def _flash(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(FLASH), *args], capture_output=True, text=True, timeout=30
    )


@_FLASH_TESTS
def test_flash_requires_device() -> None:
    out = _flash()
    assert out.returncode != 0
    assert "--device is required" in out.stderr


@_FLASH_TESTS
def test_flash_rejects_missing_device() -> None:
    out = _flash("--device", "/dev/definitely-not-here-99")
    assert out.returncode != 0
    assert "does not exist" in out.stderr


@_FLASH_TESTS
def test_flash_rejects_unknown_arg() -> None:
    out = _flash("--frobnicate")
    assert out.returncode != 0
    assert "unknown argument" in out.stderr


@_FLASH_TESTS
def test_flash_dry_run_is_nonprivileged_and_complete() -> None:
    """--dry-run on a non-existent path must still exit 0 with the full plan.

    (dry-run performs no lsblk/blockdev/mkfs calls by construction: every
    privileged command is behind run()/the dry-run guard — so a loop path
    that doesn't exist yet is fine. This is how CI and non-root operators
    inspect the plan.)
    """
    out = _flash("--device", "/dev/loop9", "--dry-run")
    assert out.returncode == 0, out.stderr
    assert "[dry-run] wipefs -a /dev/loop9" in out.stderr
    assert "sfdisk" in out.stderr
    assert "mkfs.ext4" in out.stderr and "mkfs.ntfs" in out.stderr
    assert "mkfs.exfat" in out.stderr
    assert "dry-run complete" in out.stderr


@_FLASH_TESTS
def test_flash_bash_syntax() -> None:
    out = subprocess.run(["bash", "-n", str(FLASH)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


# ── optional: privileged end-to-end on a loop device (root rigs only) ──────

_HAS_ROOT = hasattr(Path("/dev/loop-control"), "access") and subprocess.run(
    ["bash", "-c", "losetup -f >/dev/null 2>&1"], capture_output=True
).returncode == 0


@pytest.mark.skipif(not _HAS_ROOT, reason="loop-device validation requires root")
def test_layout_on_real_loop_device(tmp_path: Path) -> None:  # pragma: no cover
    img = tmp_path / "stick.img"
    img.truncate(34 * GB)  # >32GB to clear the size gate with partition-table slack
    loop = subprocess.run(
        ["losetup", "-f", "--show", str(img)], capture_output=True, text=True, check=True
    ).stdout.strip()
    try:
        out = _flash("--device", loop, "--force")  # loop: fake size gate via --force
        assert out.returncode == 0, out.stderr
        parts = usb_layout.partition_paths(loop)
        for part in parts:
            assert Path(part).exists(), f"{part} not created by flash_usb.sh"
        # Filesystem types as planned (blkid authoritative).
        probes = subprocess.run(
            ["blkid", "-o", "json"] + parts, capture_output=True, text=True, check=True
        ).stdout
        data = json.loads(probes)
        entries = data["blockdevices"] if isinstance(data, dict) else data
        types = sorted(e.get("TYPE") or "" for e in entries)
        assert types == ["ext4", "exfat", "ntfs"]
    finally:
        subprocess.run(["losetup", "-d", loop], check=False)
        img.unlink(missing_ok=True)
