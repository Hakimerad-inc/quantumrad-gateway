#!/usr/bin/env python
"""Keep every version field in sync with the canonical source (review M13).

``src/mercure_gateway/__init__.py`` declares ``__version__`` as the single
source of truth; five other places duplicate it. This script rewrites them
from the source (default) or verifies they all agree (``--check``, exit 1 on
drift — used by the guard test and locally before tagging a release).

A sixth consumer is deliberately *not* a file: the FastAPI app's reported
version used to be a hardcoded ``"0.1.0"``, which is the one mirror this
script could never see — and it was the one an API consumer and the
OpenAPI codegen actually read (review P1-8). ``create_app`` now derives it
from ``__version__``, so it cannot drift; ``test_version_sync`` asserts that
at runtime instead.

Usage:
    uv run python scripts/sync_version.py            # rewrite drifters
    uv run python scripts/sync_version.py --check    # verify only
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

INIT_PY = REPO / "src" / "mercure_gateway" / "__init__.py"
PYPROJECT = REPO / "pyproject.toml"
PACKAGE_JSON = REPO / "web" / "package.json"
TAURI_CONF = REPO / "src-tauri" / "tauri.conf.json"
CARGO_TOML = REPO / "src-tauri" / "Cargo.toml"
CARGO_LOCK = REPO / "src-tauri" / "Cargo.lock"

__all__ = ["read_canonical_version", "check", "sync"]


def read_canonical_version() -> str:
    """Read ``__version__`` from the canonical ``__init__.py`` (regex, no import)."""
    text = INIT_PY.read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if match is None:
        raise SystemExit(f"cannot parse __version__ from {INIT_PY}")
    return match.group(1)


def _expected() -> dict[Path, str]:
    """Full expected file content for every synced file, given the canonical version."""
    version = read_canonical_version()
    expected: dict[Path, str] = {}

    pp = PYPROJECT.read_text(encoding="utf-8")
    expected[PYPROJECT] = re.sub(
        r'(?m)^version = "[^"]*"$', f'version = "{version}"', pp, count=1
    )

    pj = PACKAGE_JSON.read_text(encoding="utf-8")
    expected[PACKAGE_JSON] = re.sub(
        r'(?m)^(\s*"version":\s*)"[^"]*"', rf'\g<1>"{version}"', pj, count=1
    )

    tc = TAURI_CONF.read_text(encoding="utf-8")
    expected[TAURI_CONF] = re.sub(
        r'(?m)^(\s*"version":\s*)"[^"]*"', rf'\g<1>"{version}"', tc, count=1
    )

    ct = CARGO_TOML.read_text(encoding="utf-8")
    expected[CARGO_TOML] = re.sub(
        r'(?m)^version = "[^"]*"$', f'version = "{version}"', ct, count=1
    )

    # Cargo.lock: rewrite only the mercure-gateway package's own version line.
    lock = CARGO_LOCK.read_text(encoding="utf-8")
    lock_pattern = re.compile(
        r'(name = "mercure-gateway"\nversion = ")[^"]*(")'
    )
    new_lock, n = lock_pattern.subn(rf"\g<1>{version}\g<2>", lock, count=1)
    if n != 1:
        raise SystemExit("cannot locate mercure-gateway entry in Cargo.lock")
    expected[CARGO_LOCK] = new_lock

    return expected


def _parse_version(text: str, kind: str) -> str | None:
    """Extract the version recorded in *text* (None = unparseable).

    *kind* selects the format: ``"toml"`` (pyproject/Cargo), ``"json"``
    (package.json/tauri.conf.json) or ``"cargo-lock"``.
    """
    if kind == "toml":
        match = re.search(r'(?m)^version = "([^"]*)"$', text)
        return match.group(1) if match else None
    if kind == "json":
        try:
            return str(json.loads(text)["version"])
        except (json.JSONDecodeError, KeyError, TypeError):
            return None
    if kind == "cargo-lock":
        match = re.search(r'name = "mercure-gateway"\nversion = "([^"]*)"', text)
        return match.group(1) if match else None
    return None


_KINDS: dict[Path, str] = {
    PYPROJECT: "toml",
    CARGO_TOML: "toml",
    PACKAGE_JSON: "json",
    TAURI_CONF: "json",
    CARGO_LOCK: "cargo-lock",
}


def _actual_version(path: Path) -> str | None:
    """Extract the version currently recorded in the file at *path*."""
    return _parse_version(path.read_text(encoding="utf-8"), _KINDS[path])


def check(paths: dict[Path, str] | None = None) -> list[str]:
    """Return a list of drift descriptions; empty means every source agrees.

    *paths* optionally maps known version files to their content; when given,
    the content is parsed instead of reading from disk (lets tests exercise
    drift without touching the real files). When omitted, all five real files
    are read from disk.
    """
    if paths is None:
        version = read_canonical_version()
        drift: list[str] = []
        for path in _KINDS:
            actual = _actual_version(path)
            if actual != version:
                drift.append(
                    f"{path.relative_to(REPO)}: expected {version!r}, found {actual!r}"
                )
        return drift
    drift = []
    for path, content in paths.items():
        kind = _KINDS[path]
        # In-sync expectation: the content must carry the *canonical* version.
        version = read_canonical_version()
        actual = _parse_version(content, kind)
        if actual != version:
            drift.append(
                f"{path.relative_to(REPO)}: expected {version!r}, found {actual!r}"
            )
    return drift


def sync() -> list[str]:
    """Rewrite any drifted file; returns the same drift list that was fixed."""
    expected = _expected()
    drifted = check()
    for path, content in expected.items():
        if path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
    return drifted


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in ("--check", "-c"):
        drift = check()
        if drift:
            print("version drift detected:")
            for line in drift:
                print(f"  {line}")
            return 1
        print(f"all version sources in sync at {read_canonical_version()}")
        return 0
    drift = sync()
    if drift:
        print(f"rewrote {len(drift)} file(s) to version {read_canonical_version()}:")
        for line in drift:
            print(f"  {line}")
    else:
        print(f"already in sync at {read_canonical_version()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
