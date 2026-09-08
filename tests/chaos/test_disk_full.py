"""Chaos: disk fills during operation (S09-T1, K1 / US-04).

Scripts:

1. SENT studies pile up and the filesystem crosses ``disk_full_warning_pct``.
   The disk monitor auto-purges *delivered* studies oldest-first while the
   in-flight (SENDING) study must be completely untouched — files, DB row and
   route — and still delivers once the destination accepts (K2).
2. Over the threshold but nothing eligible to purge → nothing is lost and the
   monitor loop still terminates safely; the queued study still delivers.
3. ENOSPC at receive time: the receiver returns a processing failure and no
   DB row is created — persist-before-ack means a disk-full receive is never
   acknowledged (K1).
"""

from __future__ import annotations

import errno
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydicom.uid import ExplicitVRLittleEndian
from pynetdicom import AE

from mercure_gateway.config import ReceiverConfig
from mercure_gateway.disk import DiskMonitor
from mercure_gateway.receiver import Receiver
from mercure_gateway.spool import Spool, StudyState
from mercure_gateway.spool.db import mem_database
from tests.chaos._helpers import (
    FaultyStorageSCP,
    chaos_config,
    make_dataset,
    real_forwarder,
    scp_destination,
)

OLD_DELIVERED = "1.2.826.0.1.3680043.10.200.1"
NEW_DELIVERED = "1.2.826.0.1.3680043.10.200.2"
IN_FLIGHT = "1.2.826.0.1.3680043.10.200.3"
RECEIVE_UID = "1.2.826.0.1.3680043.10.200.4"


def usage(pct: float) -> SimpleNamespace:
    """A fake ``shutil.disk_usage`` result: *pct*% used of a 100 GB volume."""
    return SimpleNamespace(total=100_000_000_000, used=pct * 1_000_000_000, free=0)


def make_spool(tmp_path: Path) -> Spool:
    cfg = chaos_config(tmp_path)
    return Spool(mem_database(), cfg)


def deliver_sent(spool: Spool, study_uid: str, *, age_hours: int = 0) -> int:
    """Persist one instance and mark the study delivered (SENT, retained copy)."""
    study_id = spool.store_instance(make_dataset(study_uid, 1))
    spool._db.set_study_state(study_id, StudyState.SENT.value)
    spool.mark_delivered(study_id)
    if age_hours:
        spool._db.connection().execute(
            "UPDATE studies SET retention_delivered_at = datetime('now', ?) WHERE id = ?",
            (f"-{age_hours} hours", study_id),
        )
        spool._db.connection().commit()
    return study_id


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def send_dataset(port: int, aet_called: str, dataset: object) -> bool:
    """C-STORE *dataset* to ``127.0.0.1:port`` over a real SCU association."""
    ae = AE(ae_title="MODALITY")
    ae.add_requested_context(dataset.SOPClassUID, ExplicitVRLittleEndian)  # type: ignore[attr-defined]
    assoc = ae.associate("127.0.0.1", port, ae_title=aet_called)
    if not assoc.is_established:
        return False
    status = assoc.send_c_store(dataset)  # type: ignore[arg-type]
    assoc.release()
    return status is not None and status.Status in (0x0000, 0xFF00)


def test_disk_full_purges_delivered_oldest_but_not_inflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SENT copies are purged oldest-first under disk pressure; the study that
    is mid-forward is untouchable and still delivers afterwards."""
    spool = make_spool(tmp_path)
    old_id = deliver_sent(spool, OLD_DELIVERED, age_hours=72)
    new_id = deliver_sent(spool, NEW_DELIVERED, age_hours=1)

    scp = FaultyStorageSCP()
    scp.start()
    try:
        # In-flight study: received, queued, claimed (SENDING) — never purgable.
        in_flight_id = spool.store_instance(make_dataset(IN_FLIGHT, 1))
        cfg = chaos_config(tmp_path)
        dest = scp_destination(scp)
        spool.enqueue(in_flight_id, [dest])
        task = spool.claim_next(limit=1)[0]
        assert task.study_id == in_flight_id
        in_files = {p.name for p in spool.study_files(IN_FLIGHT)}

        # Disk over threshold; after one purge it drops under.
        measurements = iter([usage(95.0), usage(60.0)])
        monkeypatch.setattr("mercure_gateway.disk.shutil.disk_usage", lambda _p: next(measurements))
        monitor = DiskMonitor(spool, warning_pct=90, purge_on_full=True)
        pct = monitor.check_once()

        assert pct == 60.0
        assert spool._db.get_study(old_id) is None  # oldest delivered purged
        assert spool._db.get_study(new_id) is not None  # newest delivered kept
        # In-flight study is completely untouched.
        assert spool.state(in_flight_id) == StudyState.SENDING
        assert {p.name for p in spool.study_files(IN_FLIGHT)} == in_files
        assert spool.route_attempts(in_flight_id, dest.name) == 1

        # The worker that was mid-send completes its in-flight dispatch (K2).
        fwd = real_forwarder(spool, cfg, dest, max_attempts=2)
        fwd._dispatch(task)
        assert spool.state(in_flight_id) == StudyState.SENT
        # Retained copy for the freshly delivered study stays on disk.
        assert spool.study_files(IN_FLIGHT)
    finally:
        scp.stop()


def test_disk_full_with_nothing_eligible_loses_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over the threshold with no SENT copy to reclaim: the loop terminates,
    every study survives, and the queue still drains."""
    spool = make_spool(tmp_path)
    queued_id = spool.store_instance(make_dataset(IN_FLIGHT, 1))
    spool.enqueue(queued_id, [])

    monkeypatch.setattr("mercure_gateway.disk.shutil.disk_usage", lambda _p: usage(95.0))
    monitor = DiskMonitor(spool, warning_pct=90, purge_on_full=True)
    pct = monitor.check_once()

    assert pct >= 90.0  # still over — but nothing was purged, no exception
    assert spool._db.get_study(queued_id) is not None
    assert spool.study_files(IN_FLIGHT)  # files intact


def test_disk_full_at_receive_never_acks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ENOSPC while persisting a C-STORE → receiver returns a processing
    failure and creates no study row: a disk-full receive is never acked (K1)."""
    spool = make_spool(tmp_path)
    recv = Receiver(ReceiverConfig(ae_title="GATEWAY", port=free_port()), spool)
    recv.start()
    try:

        def _raise_enospc(dataset: object) -> int:
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(spool, "store_instance", _raise_enospc)

        ok = send_dataset(recv.port, "GATEWAY", make_dataset(RECEIVE_UID, 1))

        assert ok is False  # 0xC120 processing failure — not 0x0000
        assert spool._db.list_studies() == []
    finally:
        recv.stop()
