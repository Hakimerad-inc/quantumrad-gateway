"""TDD: unit tests for the recovery scan system.

Behaviours covered (semantics per review F3 — marker = clean shutdown):
1.  Empty spool, no marker → unclean shutdown → scan runs (finds nothing)
2.  Clean shutdown (marker present) → scan skipped (fast startup)
3.  Stale marker → cleared even when the scan is skipped
4.  Files on disk without DB rows → study rows created (RECEIVED)
5.  SENDING study with files → recovered to RECEIVED
6.  ERROR study with files → recovered to RECEIVED, errored routes re-claimed
7.  Sending/errored route reset to waiting after recovery
8.  DB row without files → marked ERROR (orphaned)
9.  Non-UID directories in the spool are ignored
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mercure_gateway.config import DICOMDestination, default_config
from mercure_gateway.hotplug import write_shutdown_marker
from mercure_gateway.recovery import recover
from mercure_gateway.spool import Spool, StudyState
from mercure_gateway.spool.db import mem_database


@pytest.fixture()
def spool(tmp_path: Path) -> Spool:
    cfg = default_config()
    cfg.storage.spool_dir = str(tmp_path)
    db = mem_database()
    return Spool(db, cfg)


@pytest.fixture()
def target_hub() -> DICOMDestination:
    return DICOMDestination(
        name="hub", type="dicom", host="hub.local", port=11112, aet_target="MERCURE"
    )


# ── Empty spool ─────────────────────────────────────────────────────

def test_empty_spool_no_recovery(spool: Spool) -> None:
    """Empty spool, no marker → unclean shutdown → scan runs, finds nothing."""
    result = recover(spool)
    assert result.ran
    assert not result.had_marker
    assert result.studies_recovered == 0
    assert result.files_without_db == 0


# ── Shutdown marker ─────────────────────────────────────────────────

def test_stale_marker_cleared_even_when_scan_skipped(spool: Spool) -> None:
    """A marker means clean shutdown: the scan is skipped, but the stale
    marker must still be cleared so a *later* crash is not mistaken for
    another clean shutdown."""
    write_shutdown_marker(spool.spool_dir)
    assert has_shutdown_marker(spool.spool_dir)
    result = recover(spool)
    assert result.had_marker
    assert not result.ran  # clean shutdown → scan skipped
    assert result.cleaned
    assert not has_shutdown_marker(spool.spool_dir)


def has_shutdown_marker(d: Path) -> bool:
    return (d / ".shutdown").exists()


# ── Unclean shutdown (no marker) runs the scan ──────────────────────

def test_unclean_shutdown_recovers_sending_study(
    spool: Spool, target_hub: DICOMDestination
) -> None:
    """Crash / power-loss leaves NO marker → the scan MUST run and recover
    the interrupted SENDING study (review F3: the old gate did the opposite)."""
    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])
    spool.claim_next(limit=1)  # SENDING
    d = spool.spool_dir / "1.2.3.4" / "1.2.3.4.1"
    d.mkdir(parents=True)
    (d / "1.2.3.4.1.1.dcm").write_bytes(b"\x00" * 128)

    result = recover(spool)  # no marker, not forced → unclean shutdown
    assert result.ran
    assert result.studies_recovered == 1
    assert spool.state(study_id) == StudyState.RECEIVED


def test_clean_shutdown_marker_skips_scan(
    spool: Spool, target_hub: DICOMDestination
) -> None:
    """Marker present (graceful shutdown) → DB is authoritative → scan
    skipped so the DICOM port binds immediately."""
    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])
    spool.claim_next(limit=1)  # SENDING
    d = spool.spool_dir / "1.2.3.4" / "1.2.3.4.1"
    d.mkdir(parents=True)
    (d / "1.2.3.4.1.1.dcm").write_bytes(b"\x00" * 128)
    write_shutdown_marker(spool.spool_dir)  # graceful shutdown → marker present

    result = recover(spool)
    assert result.had_marker
    assert not result.ran
    assert spool.state(study_id) == StudyState.SENDING  # untouched

    # force=True scans regardless of the marker
    result = recover(spool, force=True)
    assert result.ran
    assert result.studies_recovered == 1


# ── Files without DB rows ───────────────────────────────────────────

def test_orphaned_files_get_study_rows(spool: Spool, tmp_path: Path) -> None:
    """DICOM files on disk with no DB row → study created as RECEIVED."""
    study_dir = spool.spool_dir / "1.2.3.4" / "1.2.3.4.1"
    study_dir.mkdir(parents=True)
    (study_dir / "1.2.3.4.1.1.dcm").write_bytes(b"\x00" * 128)
    # no marker → unclean shutdown → scan runs

    result = recover(spool)
    assert result.files_without_db == 1

    # Verify study row exists
    row = spool._db.get_study_by_uid("1.2.3.4")
    assert row is not None
    assert row["state"] == StudyState.RECEIVED.value


def test_multiple_orphaned_files(spool: Spool) -> None:
    for uid in ("1.1.1", "2.2.2", "3.3.3"):
        d = spool.spool_dir / uid / f"{uid}.1"
        d.mkdir(parents=True)
        (d / f"{uid}.1.1.dcm").write_bytes(b"\x00" * 128)
    result = recover(spool)  # no marker → unclean shutdown → scan runs
    assert result.files_without_db == 3
    assert spool._db.get_study_by_uid("1.1.1") is not None
    assert spool._db.get_study_by_uid("2.2.2") is not None
    assert spool._db.get_study_by_uid("3.3.3") is not None


def test_non_uid_directory_ignored(spool: Spool) -> None:
    """A planted non-UID directory must not be registered as a study."""
    evil = spool.spool_dir / "..%2F..%2Fetc" / "1.1"
    evil.mkdir(parents=True)
    (evil / "x.dcm").write_bytes(b"\x00" * 128)
    result = recover(spool)  # no marker → unclean shutdown → scan runs
    assert result.files_without_db == 0
    assert result.errors == []


# ── Non-terminal states recovered ───────────────────────────────────

def test_sending_study_recovered_to_received(
    spool: Spool, target_hub: DICOMDestination
) -> None:
    """A study in SENDING state with files on disk → RECEIVED."""
    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])
    spool.claim_next(limit=1)  # transitions to SENDING
    assert spool.state(study_id) == StudyState.SENDING

    # Create the files on disk
    d = spool.spool_dir / "1.2.3.4" / "1.2.3.4.1"
    d.mkdir(parents=True)
    (d / "1.2.3.4.1.1.dcm").write_bytes(b"\x00" * 128)
    result = recover(spool)  # no marker → unclean shutdown → scan runs
    assert result.studies_recovered == 1
    assert spool.state(study_id) == StudyState.RECEIVED


def test_error_study_recovered_and_forwardable(
    spool: Spool, target_hub: DICOMDestination
) -> None:
    """A study in ERROR state recovers to RECEIVED *and its errored route
    must be re-claimable by the forwarder* (stranding regression test)."""
    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])
    spool.claim_next(limit=1)
    spool.fail(study_id, "hub", "connection refused", max_attempts=5)
    assert spool.state(study_id) == StudyState.ERROR

    d = spool.spool_dir / "1.2.3.4" / "1.2.3.4.1"
    d.mkdir(parents=True)
    (d / "1.2.3.4.1.1.dcm").write_bytes(b"\x00" * 128)
    result = recover(spool)  # no marker → unclean shutdown → scan runs
    assert result.studies_recovered == 1
    assert spool.state(study_id) == StudyState.RECEIVED

    # The recovered study must actually be forwardable — claim its route.
    tasks = spool.claim_next(limit=5)
    assert len(tasks) == 1, "errored route was stranded: not claimable after recovery"
    assert tasks[0].study_id == study_id


def test_sending_route_reset_to_waiting(
    spool: Spool, target_hub: DICOMDestination
) -> None:
    """Sending routes should be reset to waiting after recovery."""
    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])
    spool.claim_next(limit=1)

    d = spool.spool_dir / "1.2.3.4" / "1.2.3.4.1"
    d.mkdir(parents=True)
    (d / "1.2.3.4.1.1.dcm").write_bytes(b"\x00" * 128)

    recover(spool)  # no marker → unclean shutdown

    routes = spool._db.get_routes(study_id)
    assert len(routes) == 1
    assert routes[0]["status"] == "waiting"


# ── DB rows without files ───────────────────────────────────────────

def test_db_row_without_files_marked_error(
    spool: Spool, target_hub: DICOMDestination
) -> None:
    """A study in the DB with no files on disk → marked ERROR."""
    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])
    spool.claim_next(limit=1)

    # No files created on disk; no marker → unclean shutdown → scan runs
    result = recover(spool)
    assert result.studies_orphaned == 1
    assert spool.state(study_id) == StudyState.ERROR


# ── Combined scenarios ──────────────────────────────────────────────

def test_mixed_recovery(spool: Spool, target_hub: DICOMDestination) -> None:
    """Multiple studies in different states — verify correct handling."""
    # Study 1: SENDING with files → should recover
    s1 = spool.receive("1.1.1")
    spool.enqueue(s1, [target_hub])
    spool.claim_next(limit=1)
    d1 = spool.spool_dir / "1.1.1" / "1.1.1.1"
    d1.mkdir(parents=True)
    (d1 / "1.1.1.1.1.dcm").write_bytes(b"\x00" * 128)

    # Study 2: SENT (terminal — should not be touched)
    s2 = spool.receive("2.2.2")
    spool.enqueue(s2, [target_hub])
    spool.claim_next(limit=1)
    spool.complete(s2, "hub")
    assert spool.state(s2) == StudyState.SENT

    # Study 3: orphaned files only
    d3 = spool.spool_dir / "3.3.3" / "3.3.3.1"
    d3.mkdir(parents=True)
    (d3 / "3.3.3.1.1.dcm").write_bytes(b"\x00" * 128)

    result = recover(spool)  # no marker → unclean shutdown → scan runs

    assert not result.had_marker
    assert result.studies_recovered == 1  # study 1
    assert result.files_without_db == 1   # study 3
    assert spool.state(s1) == StudyState.RECEIVED
    assert spool.state(s2) == StudyState.SENT  # untouched


def test_idempotent_recovery(spool: Spool, tmp_path: Path) -> None:
    """Running recovery twice should not duplicate studies."""
    d = spool.spool_dir / "1.2.3.4" / "1.2.3.4.1"
    d.mkdir(parents=True)
    (d / "1.2.3.4.1.1.dcm").write_bytes(b"\x00" * 128)

    recover(spool)  # no marker → unclean shutdown
    recover(spool, force=True)  # marker is gone; force the second scan

    row = spool._db.get_study_by_uid("1.2.3.4")
    assert row is not None
    # Only one study row should exist
    all_rows = spool._db.list_studies()
    uid_counts = [r["study_uid"] for r in all_rows]
    assert uid_counts.count("1.2.3.4") == 1
