#!/usr/bin/env python
"""K6 installer size gate — fail the build if the artifact exceeds the budget.

Accepts either a single file (deb, AppImage, installer) or a directory, whose
regular files are summed (the PyInstaller onedir sidecar tree). The default
limit is the PRD §5.1 K6 figure of 250 MB; ``--limit-mb`` overrides it (the
Linux deb gate uses 500 MB).

Usage:
    python scripts/check_installer_size.py <path> [--limit-mb N]

Returns exit code 0 (pass) or 1 (fail) and prints a message.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

K6_LIMIT_MB = 250


def _size_bytes(path: Path) -> int:
    if path.is_dir():
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return path.stat().st_size


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="K6 installer size gate (PRD §5.1)")
    parser.add_argument("path", nargs="+", help="installer file(s)/dir(s) to sum")
    parser.add_argument("--limit-mb", type=float, default=K6_LIMIT_MB)
    args = parser.parse_args(argv)

    paths = [Path(p) for p in args.path]
    for path in paths:
        if not path.exists():
            print(f"FAIL: {path} not found")
            return 1
    size_mb = sum(_size_bytes(p) for p in paths) / (1024 * 1024)
    name = " + ".join(p.name for p in paths)
    if size_mb > args.limit_mb:
        print(f"FAIL: {name} is {size_mb:.1f} MB (limit {args.limit_mb:g} MB)")
        return 1
    print(f"PASS: {name} is {size_mb:.1f} MB (limit {args.limit_mb:g} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
