#!/usr/bin/env python
"""Freeze the Python backend into the Tauri sidecar bundle (review C2/WS1).

Builds a PyInstaller **onedir** bundle of ``mercure_gateway.main`` into
``dist/mercure-gateway/`` and copies it to
``src-tauri/binaries/mercure-gateway/`` (gitignored; the Tauri build bundles
it via ``bundle.resources``).

Why onedir and not onefile: onefile self-extracts to a temp dir on every
launch (slow start for an auto-start appliance, antivirus-hostile). Tauri's
``externalBin`` only supports single files, so the directory rides in
``bundle.resources`` and lib.rs spawns it by resolved path
(see docs/dev/packaging.md).

Provenance (review P0-2): the frozen bundle is built from whatever tree ran
this script, and a stale snapshot silently ships an older backend — the
committed snapshot was 1.1.0-rc1 and predated the bind-security enforcement,
so a local ``cargo tauri build`` would have booted an unauthenticated admin
panel on the LAN instead of refusing. The build now (a) refuses when the
frozen ``__version__`` does not match the source, and (b) writes a
PROVENANCE.json beside the bundle recording what was frozen and from where.

Usage:
    uv run python scripts/package_backend.py [--keep-dist]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DIST_DIR = REPO / "dist" / "mercure-gateway"
TAURI_BIN = REPO / "src-tauri" / "binaries" / "mercure-gateway"

# The frozen package's own __init__.py — where a stale snapshot's version
# betrays it (review P0-2).
FROZEN_INIT = DIST_DIR / "_internal" / "mercure_gateway" / "__init__.py"

# uvicorn selects loop/protocol implementations via importlib strings — the
# classic PyInstaller miss — so every dynamic import is pinned explicitly.
HIDDEN_IMPORTS = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    # pydicom discovers pixel-data codecs via entry points.
    "pylibjpeg_rle",
]

# Nothing the frozen gateway needs; trims the bundle meaningfully.
EXCLUDES = [
    "tkinter",
    "matplotlib",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "IPython",
    "jupyter",
    "pytest",
    "mypy",
    "ruff",
    "pip",
    "setuptools",
    "test",
    "tests",
]


def _pyinstaller_available() -> bool:
    return importlib.util.find_spec("PyInstaller") is not None


def _source_version() -> str:
    """The canonical version, read by regex so this works from any checkout."""
    init = REPO / "src" / "mercure_gateway" / "__init__.py"
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', init.read_text(encoding="utf-8"), re.M)
    if match is None:
        raise SystemExit(f"cannot parse __version__ from {init}")
    return match.group(1)


def _frozen_version() -> str | None:
    """The version the built bundle carries (None if unparseable)."""
    if not FROZEN_INIT.exists():
        return None
    match = re.search(
        r'^__version__\s*=\s*"([^"]+)"', FROZEN_INIT.read_text(encoding="utf-8"), re.M
    )
    return match.group(1) if match is not None else None


def _git(command: list[str]) -> str:
    """Run git in REPO, returning stdout or '' when git has nothing to say."""
    try:
        out = subprocess.run(
            ["git", *command], cwd=REPO, capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        return ""  # git absent — provenance records the absence, not a crash
    return out.stdout.strip()


def _pyinstaller_version() -> str:
    """The freezing tool's version, or a marker when it is not importable.

    Read through importlib so this module type-checks without the packaging
    extra installed — the provenance record is written by the packaging
    script itself, where PyInstaller is present by construction.
    """
    try:
        spec = importlib.util.find_spec("PyInstaller")
    except (ImportError, ValueError):
        spec = None
    if spec is None or spec.origin is None:
        return "not-installed"
    # ``__version__`` lives on the package; fall back to the dist's metadata.
    try:
        from importlib.metadata import version as _dist_version

        return _dist_version("pyinstaller")
    except Exception:  # noqa: BLE001 — provenance must never fail the build
        return "unknown"


def write_provenance(dest: Path) -> Path:
    """Record what was frozen and from where, beside the bundle (P0-2).

    A stale sidecar is invisible from the outside: it boots, it answers
    health checks, and it silently runs an older backend. This file is the
    artifact an operator (or a support call) can read to see exactly which
    commit and version the frozen backend was built from.
    """
    source_version = _source_version()
    frozen_version = _frozen_version()
    record = {
        "frozen_version": frozen_version,
        "source_version": source_version,
        "version_matches_source": frozen_version == source_version,
        "pyinstaller": _pyinstaller_version(),
        "commit": _git(["rev-parse", "HEAD"]) or None,
        "branch": _git(["rev-parse", "--abbrev-ref", "HEAD"]) or None,
        "dirty": bool(_git(["status", "--porcelain"])),
        "built_on": platform.node(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        # wall-clock, not a monotonic source — this is a build label, not a
        # timing measurement.
        "built_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = dest / "PROVENANCE.json"
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}")
    return path


def assert_frozen_version_matches_source() -> None:
    """Refuse to ship a sidecar whose version disagrees with the source.

    The frozen bundle is a snapshot of a past tree, and nothing else in the
    pipeline compares it to the current one — the committed snapshot was
    1.1.0-rc1 with ``_warn_insecure`` while the source had already moved to
    ``_enforce_bind_security`` (a SystemExit, not a warning). A version check
    is a cheap proxy for "this bundle is from this tree"; the Windows smoke
    test asserts the same thing from the running binary.
    """
    source = _source_version()
    frozen = _frozen_version()
    if frozen is None:
        raise SystemExit(
            f"cannot read __version__ from the built bundle ({FROZEN_INIT}) — "
            "refusing to ship a bundle whose version is unverifiable"
        )
    if frozen != source:
        raise SystemExit(
            f"the built bundle reports version {frozen!r} but the source is "
            f"{source!r}. The frozen sidecar is stale: rebuild it with "
            "`just refresh-sidecar` (or scripts/package_backend.py) from this "
            "tree. Shipping it would silently run an older backend."
        )


def build() -> Path:
    if not _pyinstaller_available():
        raise SystemExit(
            "PyInstaller is not installed. Run: uv sync --extra package "
            "(or: uv pip install pyinstaller)"
        )
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name",
        "mercure-gateway",
        "--distpath",
        str(REPO / "dist"),
        "--workpath",
        str(REPO / "build" / "pyinstaller"),
        "--paths",
        str(REPO / "src"),
        "--collect-submodules",
        "pynetdicom",
    ]
    for mod in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", mod]
    for mod in EXCLUDES:
        cmd += ["--exclude-module", mod]
    cmd += ["--collect-all", "mercure_gateway"]  # static SPA assets + py.typed
    cmd.append(str(REPO / "src" / "mercure_gateway" / "main.py"))
    print("running:", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=REPO)

    print(f"backend bundle: {DIST_DIR}")
    return DIST_DIR


def copy_to_tauri() -> Path:
    if not DIST_DIR.exists():
        raise SystemExit(f"missing built bundle {DIST_DIR} — run build() first")
    assert_frozen_version_matches_source()
    if TAURI_BIN.exists():
        shutil.rmtree(TAURI_BIN)
    TAURI_BIN.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(DIST_DIR, TAURI_BIN)
    total = sum(f.stat().st_size for f in TAURI_BIN.rglob("*") if f.is_file())
    print(f"copied to {TAURI_BIN} ({total / (1024 * 1024):.1f} MB)")
    return TAURI_BIN


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep-dist", action="store_true", help="keep dist/mercure-gateway after copying"
    )
    args = parser.parse_args(argv)
    build()
    copy_to_tauri()
    # Provenance goes into the copy that the Tauri build actually bundles.
    write_provenance(TAURI_BIN)
    if not args.keep_dist and DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
