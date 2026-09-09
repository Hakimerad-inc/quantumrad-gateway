"""Version-source-of-truth guard (review M13).

``src/mercure_gateway/__init__.py`` is canonical; ``scripts/sync_version.py``
rewrites the four mirrors. These tests fail when any mirror drifts, so a
hand-edit that forgets the sync step cannot land silently.
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import sync_version  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def _canonical() -> str:
    return sync_version.read_canonical_version()


def test_all_five_version_sources_are_in_sync() -> None:
    """pyproject, package.json, tauri.conf.json, Cargo.toml match __init__.py."""
    version = _canonical()
    assert version, "canonical __version__ must be non-empty"

    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == version

    package = json.loads((REPO / "web" / "package.json").read_text(encoding="utf-8"))
    assert package["version"] == version

    tauri = json.loads(
        (REPO / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
    )
    assert tauri["version"] == version

    cargo = tomllib.loads((REPO / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8"))
    assert cargo["package"]["version"] == version


def test_sync_version_check_mode_detects_drift() -> None:
    """check() reports a drifted file instead of passing silently."""
    version = _canonical()

    # package.json carries the canonical version (in sync);
    # pyproject.toml is drifted to 9.9.9.
    package_in_sync = json.dumps({"name": "x", "version": version}, indent=2)
    pyproject_drifted = (
        (REPO / "pyproject.toml")
        .read_text(encoding="utf-8")
        .replace(f'version = "{version}"', 'version = "9.9.9"', 1)
    )
    assert 'version = "9.9.9"' in pyproject_drifted, "fixture must actually drift"

    paths = {
        REPO / "web" / "package.json": package_in_sync,
        REPO / "pyproject.toml": pyproject_drifted,
    }
    drift = sync_version.check(paths)
    assert len(drift) == 1
    assert "pyproject.toml" in drift[0]


def test_check_passes_on_clean_tree() -> None:
    """The real repo is currently in sync (guards the guard)."""
    assert sync_version.check() == []
