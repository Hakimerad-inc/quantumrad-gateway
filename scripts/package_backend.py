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

Usage:
    uv run python scripts/package_backend.py [--keep-dist]
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DIST_DIR = REPO / "dist" / "mercure-gateway"
TAURI_BIN = REPO / "src-tauri" / "binaries" / "mercure-gateway"

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


def build() -> Path:
    if not _pyinstaller_available():
        raise SystemExit(
            "PyInstaller is not installed. Run: uv sync --extra package "
            "(or: uv pip install pyinstaller)"
        )
    exe_name = "mercure-gateway.exe" if sys.platform == "win32" else "mercure-gateway"
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

    exe = DIST_DIR / "_internal"  # placeholder to keep type-checkers honest
    _ = exe
    print(f"backend bundle: {DIST_DIR}")
    return DIST_DIR


def copy_to_tauri() -> Path:
    if not DIST_DIR.exists():
        raise SystemExit(f"missing built bundle {DIST_DIR} — run build() first")
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
    if not args.keep_dist and DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
