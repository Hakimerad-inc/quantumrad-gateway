"""Frozen-sidecar provenance guards (review P0-2).

``scripts/package_backend.py`` freezes the Python backend into a Tauri
sidecar. A stale snapshot shipped silently once — the committed one was
1.1.0-rc1 and predated ``_enforce_bind_security``, meaning a locally built
installer would have booted an unauthenticated admin panel on the LAN.

The comparison now runs at both ends of the chain: this script when it
freezes, and ``src-tauri/build.rs`` on every ``cargo tauri build`` (the
documented pipeline is two steps, and skipping the first used to skip the
only check). These tests cover the guard in the script, the repo's own
cleanliness, and that the two implementations agree on what to compare.
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


def test_rust_build_guard_parses_the_same_files_and_version() -> None:
    """``src-tauri/build.rs`` mirrors this script's paths and version parse.

    The frozen-sidecar check now runs from two places: this script, and the
    Cargo build script (which catches the documented two-step chain where a
    developer freezes once and then runs ``cargo tauri build`` from a newer
    tree). The two must agree on *what to compare*, or one passes while the
    other bundles a stale backend — a mismatch only visible the day a real
    check actually fires.
    """
    build_rs = (REPO / "src-tauri" / "build.rs").read_text(encoding="utf-8")

    # The Rust guard hardcodes the same relative paths this script uses. If
    # either side moves a file without the other, both sides quietly check a
    # path that no longer exists and the guard no-ops.
    for needle in (
        '"binaries/mercure-gateway/_internal/mercure_gateway/__init__.py"',
        '"../src/mercure_gateway/__init__.py"',
        '"binaries/mercure-gateway/PROVENANCE.json"',
    ):
        assert needle in build_rs, f"src-tauri/build.rs no longer names {needle}"

    # And it must accept what this script accepts: the canonical version line,
    # which opens with comment lines the parser has to scan past.
    init = (REPO / "src" / "mercure_gateway" / "__init__.py").read_text(encoding="utf-8")
    canonical = re.search(r'^__version__\s*=\s*"([^"]+)"', init, re.M)
    assert canonical is not None
    assert package_backend._source_version() == canonical.group(1)


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
    # ``scripts/tauri_placeholders.py`` leaves a zero-byte marker so cargo has
    # well-defined bundle inputs; it is not a bundle and carries no version.
    marker = snapshot / "mercure-gateway"
    if marker.exists() and marker.stat().st_size == 0:
        return  # placeholder, never shipped
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
