"""Frozen-sidecar provenance guards (review P0-2).

``scripts/package_backend.py`` freezes the Python backend into a Tauri
sidecar. Nothing else in the pipeline compares the frozen bundle to the
current tree, so a stale snapshot shipped silently — the committed one was
1.1.0-rc1 and predated ``_enforce_bind_security``, meaning a locally built
installer would have booted an unauthenticated admin panel on the LAN.

These tests cover the two things that catch that: the packaging build
refuses a version mismatch, and the repo carries no stale snapshot.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import package_backend  # noqa: E402


def test_source_version_matches_canonical_init() -> None:
    """The packaging script reads the same canonical version everything else does."""
    init = (REPO / "src" / "mercure_gateway" / "__init__.py").read_text(encoding="utf-8")
    canonical = re.search(r'^__version__\s*=\s*"([^"]+)"', init, re.M)
    assert canonical is not None
    assert package_backend._source_version() == canonical.group(1)


def test_assert_rejects_a_stale_frozen_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle frozen from an older tree fails the build, loudly.

    This is the rc1-snapshot failure mode: the bundle boots and answers
    health checks, so only a version comparison at packaging time notices.
    """
    frozen_init = tmp_path / "_internal" / "mercure_gateway" / "__init__.py"
    frozen_init.parent.mkdir(parents=True)
    frozen_init.write_text(
        f'__version__ = "{package_backend._source_version()}-stale"\n', encoding="utf-8"
    )
    monkeypatch.setattr(package_backend, "FROZEN_INIT", frozen_init)

    with pytest.raises(SystemExit) as exc_info:
        package_backend.assert_frozen_version_matches_source()
    assert "stale" in str(exc_info.value)
    assert "refresh-sidecar" in str(exc_info.value)


def test_assert_rejects_an_unreadable_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle whose version cannot be read is not shipped either."""
    frozen_init = tmp_path / "_internal" / "mercure_gateway" / "__init__.py"
    frozen_init.parent.mkdir(parents=True)
    frozen_init.write_text("# no version here\n", encoding="utf-8")
    monkeypatch.setattr(package_backend, "FROZEN_INIT", frozen_init)

    with pytest.raises(SystemExit):
        package_backend.assert_frozen_version_matches_source()


def test_assert_accepts_a_matching_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle built from this tree passes — the guard is not a blanket failure."""
    frozen_init = tmp_path / "_internal" / "mercure_gateway" / "__init__.py"
    frozen_init.parent.mkdir(parents=True)
    frozen_init.write_text(
        f'__version__ = "{package_backend._source_version()}"\n', encoding="utf-8"
    )
    monkeypatch.setattr(package_backend, "FROZEN_INIT", frozen_init)

    package_backend.assert_frozen_version_matches_source()


def test_provenance_records_the_source_commit_and_version(tmp_path: Path) -> None:
    """PROVENANCE.json is the artifact that makes a frozen bundle accountable."""
    record = package_backend.write_provenance(tmp_path).read_text(encoding="utf-8")
    assert package_backend._source_version() in record
    assert "frozen_version" in record
    assert "pyinstaller" in record


def test_no_stale_sidecar_snapshot_is_committed() -> None:
    """The repo must not carry a frozen bundle older than the source.

    ``src-tauri/binaries/`` is gitignored and often absent (a clean checkout
    has no sidecar at all — CI freezes its own). When a developer has frozen
    one locally, it must be from the current tree; the rc1 snapshot that
    predated bind-security enforcement is exactly what this catches.
    """
    snapshot = REPO / "src-tauri" / "binaries" / "mercure-gateway"
    if not snapshot.exists():
        return  # clean tree — nothing to be stale
    frozen = package_backend._frozen_version()
    assert frozen is not None, (
        f"a frozen sidecar exists at {snapshot} but reports no version — "
        "delete it or rebuild with `just refresh-sidecar`"
    )
    assert frozen == package_backend._source_version(), (
        f"the local sidecar snapshot is {frozen!r} but the source is "
        f"{package_backend._source_version()!r} — rebuild it with "
        "`just refresh-sidecar` before building an installer"
    )
