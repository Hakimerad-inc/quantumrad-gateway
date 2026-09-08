"""Chaos: process crash mid-send, then recovery on restart (S09-T1, K1/K2).

Script 1 — complete outage while forwarding: a study is accepted (files on
disk, DB row RECEIVED) and claimed by the forwarder (SENDING, route 'sending')
when the process is killed — no shutdown marker. On restart ``recover()`` must:

- keep every accepted instance on disk (K1, unchanged file set),
- drop the interrupted study back to RECEIVED and reset the sending route to
  'waiting' so the forwarder re-claims it,
- then deliver the full study once the destination is reachable (K2).

Script 2 — files that survived but whose DB row never made it (crash between
file write and upsert) are re-registered as RECEIVED and forwarded.
"""

from __future__ import annotations

from pathlib import Path

from mercure_gateway.hotplug import write_shutdown_marker
from mercure_gateway.recovery import recover
from mercure_gateway.spool import Spool, StudyState
from mercure_gateway.spool.db import mem_database
from tests.chaos._helpers import (
    FaultyStorageSCP,
    chaos_config,
    make_dataset,
    real_forwarder,
    scp_destination,
)

STUDY_UID = "1.2.826.0.1.3680043.10.300.1"
ORPHAN_UID = "1.2.826.0.1.3680043.10.300.2"
FIRST_SOP = f"{STUDY_UID}.1.1"
SECOND_SOP = f"{STUDY_UID}.1.2"


def make_spool(tmp_path: Path) -> Spool:
    cfg = chaos_config(tmp_path)
    return Spool(mem_database(), cfg)


def test_recover_interrupted_send_then_redeliver(
    tmp_path: Path,
) -> None:
    """Kill mid-send: restart recovery re-queues the SENDING study (files kept)
    and the forwarder delivers the whole study afterwards (at-least-once)."""
    spool = make_spool(tmp_path)

    # Received and fully accepted (2 instances).
    spool.store_instance(make_dataset(STUDY_UID, 1))
    spool.store_instance(make_dataset(STUDY_UID, 2))
    files_before = {p.name for p in spool.study_files(STUDY_UID)}
    assert len(files_before) == 2

    scp = FaultyStorageSCP()
    scp.start()
    try:
        cfg = chaos_config(tmp_path)
        dest = scp_destination(scp)
        study_id = spool._db.get_study_by_uid(STUDY_UID)["id"]
        spool.enqueue(study_id, [dest])
        spool.claim_next(limit=1)  # forwarder mid-C-STORE
        assert spool.state(study_id) == StudyState.SENDING

        # Process killed mid-send → no shutdown marker; startup runs recovery.
        result = recover(spool)
        assert result.ran
        assert not result.had_marker
        assert result.studies_recovered == 1

        # K1: every accepted instance survived the crash.
        assert {p.name for p in spool.study_files(STUDY_UID)} == files_before
        # Interrupted study is re-claimable: RECEIVED + route back to waiting.
        assert spool.state(study_id) == StudyState.RECEIVED
        routes = spool._db.get_routes(study_id)
        assert len(routes) == 1 and routes[0]["status"] == "waiting"

        # K2: forwarder redelivers the full study to the live destination.
        forwarder = real_forwarder(spool, cfg, dest, max_attempts=2)
        forwarder.process_once()
        assert spool.state(study_id) == StudyState.SENT
        assert sorted(scp.received) == sorted([FIRST_SOP, SECOND_SOP])
        assert len(spool.study_files(STUDY_UID)) == 2  # retained copy
    finally:
        scp.stop()


def test_orphaned_files_on_disk_are_registered_and_forwarded(
    tmp_path: Path,
) -> None:
    """Recovery re-registers files that made it to disk before a crash (the
    DB row never did) and they are then delivered — nothing is stranded."""
    spool = make_spool(tmp_path)
    dataset = make_dataset(ORPHAN_UID, 1)
    series = spool.spool_dir / ORPHAN_UID / f"{ORPHAN_UID}.1"
    series.mkdir(parents=True)
    instance_file = series / f"{ORPHAN_UID}.1.1.dcm"
    dataset.save_as(str(instance_file), enforce_file_format=True)  # real Part-10
    assert spool._db.list_studies() == []  # crash before the DB upsert

    result = recover(spool)  # no marker → unclean shutdown → scan runs
    assert result.ran
    assert result.files_without_db == 1
    row = spool._db.get_study_by_uid(ORPHAN_UID)
    assert row is not None and row["state"] == StudyState.RECEIVED.value

    # Idempotent rerun must not duplicate the registered study.
    rerun = recover(spool, force=True)
    assert rerun.files_without_db == 0  # already known after the first scan
    assert sum(r["study_uid"] == ORPHAN_UID for r in spool._db.list_studies()) == 1

    # And the re-registered study is forwardable to a live destination (K2).
    scp = FaultyStorageSCP()
    scp.start()
    try:
        cfg = chaos_config(tmp_path)
        dest = scp_destination(scp)
        spool.enqueue(row["id"], [dest])
        forwarder = real_forwarder(spool, cfg, dest, max_attempts=2)
        forwarder.process_once()
        assert spool.state(row["id"]) == StudyState.SENT
        assert scp.received == [str(dataset.SOPInstanceUID)]
    finally:
        scp.stop()


def test_clean_shutdown_marker_skips_scan_and_is_cleared(
    tmp_path: Path,
) -> None:
    """A marker (graceful shutdown) means the DB is authoritative: recovery is
    skipped for speed and the stale marker is still cleared for next time."""
    spool = make_spool(tmp_path)
    study_id = spool.store_instance(make_dataset(STUDY_UID, 1))
    write_shutdown_marker(spool.spool_dir)

    result = recover(spool)
    assert result.had_marker
    assert not result.ran  # skipped — port binds fast

    from mercure_gateway.hotplug import has_shutdown_marker

    assert not has_shutdown_marker(spool.spool_dir)  # stale marker cleared
    # The study row was untouched by the (skipped) scan.
    assert spool.state(study_id) == StudyState.RECEIVED
