"""S07-T1 (RED): Handler contract consolidation — registration semantics.

Pins the ``DestinationHandler`` protocol boundary with tests for handler
registration edge cases: duplicate registration, override, unknown-type
dispatch, and the base-class helper for retries/paths.
"""

from __future__ import annotations

from test_forwarder import FakeHandler

from mercure_gateway.config import DICOMDestination, default_config
from mercure_gateway.forwarder import DeliveryResult, Forwarder, RetryPolicy
from mercure_gateway.spool import Spool, StudyState
from mercure_gateway.spool.db import mem_database

# ══════════════════════════════════════════════════════════════════════
# Handler registration semantics
# ══════════════════════════════════════════════════════════════════════

def test_register_duplicate_type_overwrites() -> None:
    """Registering a second handler for the same type replaces the first."""
    spool = Spool(mem_database())
    fwd = Forwarder(default_config(), spool, retry=RetryPolicy(max_attempts=1))
    first = FakeHandler(succeed=False)
    second = FakeHandler(succeed=True)
    fwd.register_handler("dicom", first)
    fwd.register_handler("dicom", second)

    study_id = spool.receive("1.2.3.4")
    target = DICOMDestination(name="h", host="h", port=104, aet_target="M")
    spool.enqueue(study_id, [target])
    fwd.process_once()

    assert spool.state(study_id) == StudyState.SENT
    assert len(first.calls) == 0
    assert len(second.calls) == 1


def test_register_duplicate_destination_overwrites() -> None:
    """Registering two per-destination handlers with the same name uses the
    last one registered (last-registration-wins)."""
    spool = Spool(mem_database())
    fwd = Forwarder(default_config(), spool, retry=RetryPolicy(max_attempts=1))
    first = FakeHandler(succeed=False)
    second = FakeHandler(succeed=True)
    fwd.register_handler("dicom", first, target_name="hub")
    fwd.register_handler("dicom", second, target_name="hub")

    study_id = spool.receive("1.2.3.4")
    target = DICOMDestination(name="hub", host="h", port=104, aet_target="M")
    spool.enqueue(study_id, [target])
    fwd.process_once()

    assert spool.state(study_id) == StudyState.SENT
    assert len(first.calls) == 0
    assert len(second.calls) == 1


def test_unknown_type_without_any_handler() -> None:
    """A task with an unknown type and no registered handlers at all should
    fail gracefully (ERROR when the retry budget is not yet spent)."""
    spool = Spool(mem_database())
    fwd = Forwarder(default_config(), spool, retry=RetryPolicy())  # default budget 5

    study_id = spool.receive("1.2.3.4")
    target = DICOMDestination(name="hub", host="h", port=104, aet_target="M")
    spool.enqueue(study_id, [target])
    fwd.process_once()

    assert spool.state(study_id) == StudyState.ERROR


def test_dispatcher_returns_error_for_unregistered_type_with_other_handlers() -> None:
    """``dicom`` handler is registered, but ``sftp`` target gets ERROR."""
    spool = Spool(mem_database())
    fwd = Forwarder(default_config(), spool, retry=RetryPolicy())  # default budget 5
    fwd.register_handler("dicom", FakeHandler(succeed=True))

    study_id = spool.receive("1.2.3.4")
    from mercure_gateway.config import SFTPDestination
    target = SFTPDestination(name="nas", type="sftp", host="n", port=22, username="u")
    spool.enqueue(study_id, [target])
    fwd.process_once()

    assert spool.state(study_id) == StudyState.ERROR


def test_unregistered_type_exhausts_to_failed() -> None:
    """With max_attempts=1, an unregistered handler fails immediately to FAILED
    (no retry possible within budget)."""
    spool = Spool(mem_database())
    fwd = Forwarder(default_config(), spool, retry=RetryPolicy(max_attempts=1))
    fwd.register_handler("dicom", FakeHandler(succeed=True))

    study_id = spool.receive("1.2.3.4")
    from mercure_gateway.config import SFTPDestination
    target = SFTPDestination(name="nas", type="sftp", host="n", port=22, username="u")
    spool.enqueue(study_id, [target])
    fwd.process_once()

    assert spool.state(study_id) == StudyState.FAILED


# ══════════════════════════════════════════════════════════════════════
# Handler base-class helper (retries + paths)
# ══════════════════════════════════════════════════════════════════════

def test_handler_base_deliver_interface() -> None:
    """The ``DestinationHandler`` protocol requires a single ``deliver``
    method; any object with that method satisfies the contract."""
    class AdHocHandler:
        def deliver(self, task: object, spool_dir: object) -> DeliveryResult:
            return DeliveryResult(ok=True)

    handler = AdHocHandler()
    fwd = Forwarder(default_config(), Spool(mem_database()), retry=RetryPolicy(max_attempts=1))
    fwd.register_handler("dicom", handler)

    study_id = fwd.spool.receive("1.2.3.4")
    target = DICOMDestination(name="h", host="h", port=104, aet_target="M")
    fwd.spool.enqueue(study_id, [target])
    count = fwd.process_once()

    assert count == 1
    assert fwd.spool.state(study_id) == StudyState.SENT
