"""TDD (S10-T8, RED): recovery scan on USB boot — usb-dongle-gateway-spec §7.3.

DoD behaviors (functional coverage shared with tests/test_recovery.py +
tests/chaos/test_crash_recovery.py; this file pins the §7.3 boot-time
contract end-to-end, including the DoD's "recovery logged"):
1. An interrupted (SENDING) study is recovered to RECEIVED — eligible for
   re-forward.
2. A partial study (DB row, files vanished) is marked incomplete (ERROR).
3. The shutdown marker is cleared so a *later* crash is not mistaken for
   a clean shutdown.
4. Recovery actions are logged (operator must see what was reconciled).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from mercure_gateway.config import DICOMDestination, default_config
from mercure_gateway.hotplug import has_shutdown_marker, write_shutdown_marker
from mercure_gateway.recovery import recover
from mercure_gateway.spool import Spool, StudyState
from mercure_gateway.spool.db import mem_database


@pytest.fixture()
def spool(tmp_path: Path) -> Spool:
    cfg = default_config()
    cfg.storage.spool_dir = str(tmp_path)
    return Spool(mem_database(), cfg)


def _interrupted_study(spool: Spool, target_hub: DICOMDestination) -> int:
    """A study claimed for sending (as a crash would leave it) + its files."""
    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])
    spool.claim_next(limit=1)  # SENDING
    d = spool.spool_dir / "1.2.3.4" / "1.2.3.4.1"
    d.mkdir(parents=True)
    (d / "1.2.3.4.1.1.dcm").write_bytes(b"\x00" * 128)
    return study_id


# ── DoD 1+4: interrupted study recovered, actions logged ────────────────


def test_interrupted_study_recovered_and_logged(
    spool: Spool,
    target_hub: DICOMDestination,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unclean boot (no marker) with a mid-send study: recovered to
    RECEIVED and the recovery actions are logged."""
    study_id = _interrupted_study(spool, target_hub)

    with caplog.at_level(logging.INFO, logger="mercure_gateway.recovery"):
        result = recover(spool)

    assert result.ran
    assert result.studies_recovered == 1
    assert spool.state(study_id) == StudyState.RECEIVED

    messages = " ".join(r.message for r in caplog.records)
    assert "1.2.3.4" in messages, "recovery did not log which study was recovered"
    assert "RECEIVED" in messages, "recovery did not log the state transition"


def test_recovered_study_is_forwardable(
    spool: Spool, target_hub: DICOMDestination
) -> None:
    """RECEIVED after recovery means eligible for re-forward — the route
    must be re-claimable (stranding regression)."""
    study_id = _interrupted_study(spool, target_hub)
    recover(spool)

    tasks = spool.claim_next(limit=5)
    assert [t.study_id for t in tasks] == [study_id]


# ── DoD 2: partial study (DB row, no files) marked incomplete ───────────


def test_partial_study_marked_incomplete(
    spool: Spool, target_hub: DICOMDestination
) -> None:
    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])
    spool.claim_next(limit=1)
    # Files never landed on disk (crash before fsync).

    result = recover(spool)

    assert result.studies_orphaned == 1
    assert spool.state(study_id) == StudyState.ERROR


# ── DoD 3: marker cleared on boot ────────────────────────────────────────


def test_marker_cleared_on_boot(spool: Spool, tmp_path: Path) -> None:
    """§7.3 step 7: the marker is cleared on boot regardless of whether the
    scan ran — otherwise the next crash looks like a clean shutdown."""
    write_shutdown_marker(spool.spool_dir)

    result = recover(spool)  # clean shutdown → scan skipped

    assert result.had_marker
    assert result.cleaned
    assert not has_shutdown_marker(spool.spool_dir)

    # A subsequent crash (still no marker) must trigger a full scan.
    d = spool.spool_dir / "1.2.3.4" / "1.2.3.4.1"
    d.mkdir(parents=True)
    (d / "1.2.3.4.1.1.dcm").write_bytes(b"\x00" * 128)
    result = recover(spool)
    assert result.ran
    assert result.files_without_db == 1
