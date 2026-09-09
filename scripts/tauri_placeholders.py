#!/usr/bin/env python
"""Create placeholder sidecar binaries so Tauri's build script stays happy.

The packaging pipeline freezes the real backend via ``scripts/package_backend.py``.
For local ``cargo check``/``clippy``/``test`` runs (and CI jobs that don't
package), a stub at ``src-tauri/binaries/mercure-gateway/mercure-gateway(.exe)``
keeps tooling that expects the bundle inputs well-defined. The stub is a
zero-byte marker — never shipped, never executed.

Usage: uv run python scripts/tauri_placeholders.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SIDECAR_DIR = REPO / "src-tauri" / "binaries" / "mercure-gateway"


def main() -> int:
    exe = "mercure-gateway.exe" if sys.platform == "win32" else "mercure-gateway"
    marker = SIDECAR_DIR / exe
    if not marker.exists():
        SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
        marker.touch()
        print(f"placeholder written: {marker}")
    else:
        print(f"placeholder present: {marker}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
