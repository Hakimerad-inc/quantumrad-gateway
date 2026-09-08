#!/usr/bin/env python
"""K6 installer size gate — fail the build if the installer exceeds 250 MB (PRD §5.1).

Usage:
    python scripts/check_installer_size.py <path-to-installer>

Returns exit code 0 (pass) or 1 (fail) and prints a message.
"""

from __future__ import annotations

import sys
from pathlib import Path

K6_LIMIT_MB = 250

def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: check_installer_size.py <path-to-installer>")
        return 1
    path = Path(sys.argv[1])
    if not path.exists():
        print(f"FAIL: {path} not found")
        return 1
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > K6_LIMIT_MB:
        print(f"FAIL: {path.name} is {size_mb:.1f} MB (limit {K6_LIMIT_MB} MB)")
        return 1
    print(f"PASS: {path.name} is {size_mb:.1f} MB (limit {K6_LIMIT_MB} MB)")
    return 0

if __name__ == "__main__":
    sys.exit(main())