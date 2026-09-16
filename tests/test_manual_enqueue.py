"""TDD (E1 dry-run finding): an operator must be able to rescue a study.

The auto-enqueue path is best-effort by construction: if routing fails, or if
a study was received while no destination was enabled and destinations were
added later, the study sits in RECEIVED with **no routes**. ``_auto_enqueue``
documents this as "the study stays RECEIVED and can be re-forwarded from the
console" — but that console path did not exist: ``/retry`` only iterates
*existing* routes, so a route-less RECEIVED study was unreachable from the
web panel and required direct DB access (E1 dry run, 2026-09-16).

``enqueue_study`` is that missing operator action.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian

from mercure_gateway.config import DICOMDestination, ForwardingRule, default_config
from mercure_gateway.spool import Spool, StudyState
from mercure_gateway.spool.db import mem_database


def make_dataset(study_uid: str, instance_uid: str) -> Dataset:
    ds = Dataset()
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = f"{study_uid}.1"
    ds.SOPInstanceUID = instance_uid
    ds.SOPClassUID = CTImageStorage
    ds.Modality = "CT"
    ds.PatientName = "TEST^P"
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = CTImageStorage
    ds.file_meta.MediaStorageSOPInstanceUID = instance_uid
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    return ds


@pytest.fixture()
def hub() -> DICOMDestination:
    return DICOMDestination(
        name="hub", type="dicom", host="hub.local", port=11112, aet_target="MERCURE"
    )


def spool_with(tmp_path: Path, destinations: list[DICOMDestination]) -> Spool:
    cfg = default_config()
    cfg.storage.spool_dir = str(tmp_path / "spool")
    # A delay long enough that the study is still RECEIVED (route-less)
    # when the test inspects it — this is exactly the stranded state the
    # E1 dry run reached; tests call enqueue_study before it fires.
    cfg.receiver.auto_enqueue_delay_sec = 10.0
    cfg.destinations = destinations
    return Spool(mem_database(), cfg)


# ── 1. the gap itself: /retry cannot rescue a route-less study ──────────


def test_retry_cannot_rescue_routeless_study(tmp_path: Path, hub: DICOMDestination) -> None:
    """A RECEIVED study with no routes is invisible to reforward_study."""
    spool = spool_with(tmp_path, [])  # no destinations → never auto-enqueued
    study_id = spool.store_instance(make_dataset("1.2.3.7", "1.2.3.7.1.1"))

    assert spool.state(study_id) == StudyState.RECEIVED
    assert spool.reforward_study(study_id) == 0  # zero routes to reset
    assert spool.state(study_id) == StudyState.RECEIVED  # still stuck


# ── 2. the operator action: route a RECEIVED study on demand ────────────


def test_manual_enqueue_rescues_routeless_study(tmp_path: Path, hub: DICOMDestination) -> None:
    """enqueue_study routes a stranded study to the enabled destinations."""
    spool = spool_with(tmp_path, [])
    study_id = spool.store_instance(make_dataset("1.2.3.8", "1.2.3.8.1.1"))
    assert spool.state(study_id) == StudyState.RECEIVED

    # destinations added after receipt — the classic post-install rescue
    spool._config.destinations = [hub]
    assert spool.enqueue_study(study_id) == 1

    assert spool.state(study_id) == StudyState.QUEUED
    routes = spool._db.get_routes(study_id)
    assert [r["target_name"] for r in routes] == ["hub"]
    assert routes[0]["status"] == "waiting"


def test_manual_enqueue_is_idempotent(tmp_path: Path, hub: DICOMDestination) -> None:
    """Re-enqueueing must not duplicate routes (a study can only queue once)."""
    spool = spool_with(tmp_path, [hub])
    study_id = spool.store_instance(make_dataset("1.2.3.9", "1.2.3.9.1.1"))

    assert spool.enqueue_study(study_id) == 1
    assert spool.enqueue_study(study_id) == 0  # already routed; not an error
    assert len(spool._db.get_routes(study_id)) == 1


def test_manual_enqueue_rejects_terminal_study(tmp_path: Path, hub: DICOMDestination) -> None:
    """SENT studies must not be re-routed — re-delivery is not idempotent."""
    spool = spool_with(tmp_path, [hub])
    study_id = spool.store_instance(make_dataset("1.2.3.10", "1.2.3.10.1.1"))
    spool.enqueue(study_id, [hub])
    spool._db.set_study_state(study_id, StudyState.SENT.value)
    assert spool.state(study_id) == StudyState.SENT

    with pytest.raises(ValueError, match="terminal"):
        spool.enqueue_study(study_id)


def test_manual_enqueue_with_no_destinations_is_a_config_error(
    tmp_path: Path,
) -> None:
    """Enqueueing into an empty target list would silently no-op; refuse."""
    spool = spool_with(tmp_path, [])
    study_id = spool.store_instance(make_dataset("1.2.3.11", "1.2.3.11.1.1"))

    with pytest.raises(ValueError, match="no enabled destination"):
        spool.enqueue_study(study_id)


def test_manual_enqueue_unknown_study(tmp_path: Path, hub: DICOMDestination) -> None:
    """A bogus id is a caller bug, not a silent success."""
    spool = spool_with(tmp_path, [hub])
    with pytest.raises(KeyError):
        spool.enqueue_study(999999)


# ── 3. a stale forwarding-rule target must not strand a study irrecoverably ─
#
# A rule naming a destination that was renamed/disabled/removed narrows the
# target set to nothing. enqueue() used to set QUEUED anyway — with zero
# routes. That state is strictly worse than stranded: the Enqueue action
# renders only for RECEIVED, and /retry only resets routes that exist. The
# study is unreachable from the panel and needs direct DB access.


def test_stale_rule_target_keeps_study_received(
    tmp_path: Path, hub: DICOMDestination
) -> None:
    """A rule matching no enabled destination must not produce QUEUED+0 routes."""
    cfg = default_config()
    cfg.storage.spool_dir = str(tmp_path / "spool")
    cfg.receiver.auto_enqueue_delay_sec = 10.0
    cfg.destinations = [hub]
    # 'pacs' doesn't exist — the classic copied-from-another-profile case.
    cfg.forwarding_rules = [ForwardingRule(rule="modality:CT", targets=["pacs"])]
    spool = Spool(mem_database(), cfg)
    study_id = spool.store_instance(make_dataset("1.2.3.31", "1.2.3.31.1.1"))

    with pytest.raises(ValueError, match="no enabled destination"):
        spool.enqueue_study(study_id)

    assert spool.state(study_id) == StudyState.RECEIVED  # still rescuable
    assert spool._db.get_routes(study_id) == []


def test_stale_rule_target_on_auto_enqueue_stays_received(
    tmp_path: Path, hub: DICOMDestination
) -> None:
    """The automatic path has the same guard — no QUEUED-without-routes."""
    cfg = default_config()
    cfg.storage.spool_dir = str(tmp_path / "spool")
    cfg.receiver.auto_enqueue_delay_sec = 0.05
    cfg.destinations = [hub]
    cfg.forwarding_rules = [ForwardingRule(rule="modality:CT", targets=["pacs"])]
    spool = Spool(mem_database(), cfg)
    study_id = spool.store_instance(make_dataset("1.2.3.32", "1.2.3.32.1.1"))

    import time

    time.sleep(0.3)  # past the delay: the timer fired
    assert spool.state(study_id) == StudyState.RECEIVED
    assert spool._db.get_routes(study_id) == []


def test_matching_rule_enqueues_only_the_named_target(
    tmp_path: Path, hub: DICOMDestination
) -> None:
    """The happy path still works: a rule narrows to its own targets only."""
    pacs = DICOMDestination(
        name="pacs", type="dicom", host="pacs.local", port=11112, aet_target="PACS"
    )
    cfg = default_config()
    cfg.storage.spool_dir = str(tmp_path / "spool")
    cfg.receiver.auto_enqueue_delay_sec = 10.0
    cfg.destinations = [hub, pacs]
    cfg.forwarding_rules = [ForwardingRule(rule="modality:CT", targets=["pacs"])]
    spool = Spool(mem_database(), cfg)
    study_id = spool.store_instance(make_dataset("1.2.3.33", "1.2.3.33.1.1"))

    assert spool.enqueue_study(study_id) == 1
    assert [r["target_name"] for r in spool._db.get_routes(study_id)] == ["pacs"]
    assert spool.state(study_id) == StudyState.QUEUED
