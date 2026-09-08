"""Chaos: destination killed mid-transfer (S09-T1, K1/K2).

Script: the forwarder is C-STOREing a two-instance study when the remote
destination dies after the first instance (association aborted). Assertions:

- K1 / retention: the local spool copy survives the failed transfer — every
  instance file is still on disk, nothing was auto-deleted (US-04).
- Retry with backoff ran: the retry loop re-claimed the SAME route by id
  (two recorded attempts against the crashed destination).
- K2: once the destination comes back, the study is eventually delivered in
  full (every instance reaches the recovered SCP) and transitions to SENT.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mercure_gateway.spool import Spool, StudyState
from mercure_gateway.spool.db import mem_database
from tests.chaos._helpers import (
    FaultyStorageSCP,
    chaos_config,
    make_dataset,
    real_forwarder,
    scp_destination,
)

STUDY_UID = "1.2.826.0.1.3680043.10.150.9"
FIRST_SOP = f"{STUDY_UID}.1.1"  # first instance file (sorted lexicographically first)
SECOND_SOP = f"{STUDY_UID}.1.2"


@pytest.fixture()
def study_on_wire(tmp_path: Path) -> SimpleNamespace:
    """A received-and-queued two-instance study in front of a live SCP."""
    scp = FaultyStorageSCP()
    scp.start()
    try:
        cfg = chaos_config(tmp_path)
        spool = Spool(mem_database(), cfg)
        study_id = spool.store_instance(make_dataset(STUDY_UID, 1))
        spool.store_instance(make_dataset(STUDY_UID, 2))
        dest = scp_destination(scp)
        spool.enqueue(study_id, [dest])
        yield SimpleNamespace(
            scp=scp,
            spool=spool,
            cfg=cfg,
            dest=dest,
            study_id=study_id,
            files=spool.study_files(STUDY_UID),
        )
    finally:
        scp.stop()


def test_destination_killed_mid_transfer_retains_copy(
    study_on_wire: SimpleNamespace,
) -> None:
    """SCP dies after the first instance: local copy intact, retry budget
    consumed, study FAILED (never silently marked delivered)."""
    ctx = study_on_wire
    assert len(ctx.files) == 2

    # Destination dies after accepting one instance of the transfer.
    ctx.scp.crash_after = 1
    fwd = real_forwarder(ctx.spool, ctx.cfg, ctx.dest, max_attempts=2)
    fwd.process_once()

    # One instance reached the destination before it died; the retry loop
    # re-claimed the same route (attempts=2) then exhausted its budget.
    assert ctx.scp.received == [FIRST_SOP]
    assert ctx.spool.route_attempts(ctx.study_id, ctx.dest.name) == 2
    assert ctx.spool.state(ctx.study_id) == StudyState.FAILED

    # K1: the failed send never touched the local copy (exact file names).
    surviving = {p.name for p in ctx.spool.study_files(STUDY_UID)}
    assert surviving == {p.name for p in ctx.files}


def test_eventual_delivery_after_destination_recovers(
    study_on_wire: SimpleNamespace,
) -> None:
    """K2: once the destination is restored, re-forward delivers the full study."""
    ctx = study_on_wire

    ctx.scp.crash_after = 1
    doomed = real_forwarder(ctx.spool, ctx.cfg, ctx.dest, max_attempts=2)
    doomed.process_once()
    assert ctx.spool.state(ctx.study_id) == StudyState.FAILED

    # Destination recovers; manual re-forward (US-04) resumes delivery.
    ctx.scp.crash_after = None
    ctx.spool.reforward_study(ctx.study_id)
    revived = real_forwarder(ctx.spool, ctx.cfg, ctx.dest, max_attempts=2)
    revived.process_once()

    assert ctx.spool.state(ctx.study_id) == StudyState.SENT
    # At-least-once: instance 1 went twice (pre-crash + full re-send), plus
    # instance 2 once on the re-send.
    assert sorted(ctx.scp.received) == sorted([FIRST_SOP, FIRST_SOP, SECOND_SOP])
    # Retained copy still on disk for verification / retention (never deleted).
    assert len(ctx.spool.study_files(STUDY_UID)) == 2
