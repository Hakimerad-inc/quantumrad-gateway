"""Scheduled replay of the audit chain (review P0-10 remainder).

``AnchorVerifier`` guards the *authenticity* half of tamper evidence: hub-held
Ed25519 signatures over each anchored chain head.  But unsigned deployments —
the default, with no ``anchor_public_key`` configured — have no signature to
check, and the chain's own hash replay had exactly one production caller, the
on-demand ``/api/audit/verify`` endpoint.  That endpoint is deliberately kept
out of the scrape path (a full-table scan per scrape is a DoS vector), so on a
stock box a broken chain sat undetected until an operator thought to ask.

``ChainVerifier`` is the timer that closes that gap.  Honest threat-model
limit, carried into the class docstring: this detects *accidents* — a partial
write, a dropped trigger, a botched migration, a hand-edited row — not an
attacker who can rewrite the database, because the chain is an unkeyed hash
over public columns and can be recomputed end to end.  Only an anchor stored
outside the database can see that.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from mercure_gateway.audit import AuditLog, ChainVerification, ChainVerifier
from mercure_gateway.audit.events import AUDIT_CHAIN_FAILED
from mercure_gateway.spool.db import (
    _AUDIT_NO_DELETE,
    _AUDIT_NO_UPDATE,
    mem_database,
)


def _audit_with_events(n: int = 3) -> Any:
    """An audit log holding n intact events."""
    database = mem_database()
    audit = AuditLog(database)
    for i in range(n):
        audit.append("STUDY_RECEIVED", {"study_uid": f"1.2.3.{i}"})
    return database, audit


def _damage_event(database: Any, event_id: int) -> None:
    """Rewrite one row's detail without recomputing its chain hash.

    The append-only triggers exist precisely to stop this, so a real attacker
    would have to drop them first — which is exactly the gesture simulated
    here: the row's stored hash is now stale relative to its neighbours.
    """
    conn = database.connection()
    conn.execute("DROP TRIGGER IF EXISTS audit_events_no_update")
    conn.execute("DROP TRIGGER IF EXISTS audit_events_no_delete")
    conn.execute(
        "UPDATE audit_events SET detail = ? WHERE id = ?",
        ('{"edited": "after the fact"}', event_id),
    )
    conn.execute(_AUDIT_NO_UPDATE)
    conn.execute(_AUDIT_NO_DELETE)


def _wait_until(predicate: object, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return
        time.sleep(0.02)
    raise AssertionError(f"condition not met within {timeout}s")


def _make_verifier(
    audit: AuditLog,
    *,
    interval_sec: float = 300.0,
    initial_delay_sec: float = 5.0,
    on_failure: Callable[[ChainVerification], None] | None = None,
) -> ChainVerifier:
    """The verifier under test, with the production defaults made explicit."""
    return ChainVerifier(
        audit,
        interval_sec=interval_sec,
        initial_delay_sec=initial_delay_sec,
        on_failure=on_failure,
    )


# ── the timer ──────────────────────────────────────────────────────────────


def test_a_healthy_chain_verifies_on_demand() -> None:
    _database, audit = _audit_with_events()
    verifier = _make_verifier(audit)

    result = verifier.verify_now()

    assert result.ok is True
    assert result.errors == ()


def test_an_empty_chain_is_not_a_failure() -> None:
    """A fresh boot with nothing appended yet is a healthy chain of zero links."""
    audit = AuditLog(mem_database())
    verifier = _make_verifier(audit)

    result = verifier.verify_now()

    assert result.ok is True


def test_a_broken_chain_is_detected_on_demand() -> None:
    database, audit = _audit_with_events()
    _damage_event(database, 3)  # the newest link
    verifier = _make_verifier(audit)

    result = verifier.verify_now()

    assert result.ok is False
    assert len(result.errors) == 1
    assert result.errors[0].event_id == 3
    assert result.errors[0].reason == "hash mismatch"


def test_damage_propagates_to_every_downstream_link() -> None:
    """A rewritten row breaks the chain forward, not just at the edited row.

    The replay propagates the *computed* hash, so an edit of the oldest event
    surfaces as a mismatch at every later event — this is what makes a
    partial rewrite (fix the row, forget its neighbours) detectable.
    """
    database, audit = _audit_with_events()
    _damage_event(database, 1)
    verifier = _make_verifier(audit)

    result = verifier.verify_now()

    assert result.ok is False
    assert [e.event_id for e in result.errors] == [1, 2, 3]


def test_the_timer_detects_a_chain_broken_after_boot() -> None:
    """The finding P0-10 is about: damage that happens later is still caught.

    The chain was intact when the gateway booted; the verifier has to re-replay
    it on its cadence, not trust a boot-time pass.
    """
    database, audit = _audit_with_events()
    failures: list[ChainVerification] = []
    verifier = _make_verifier(
        audit, interval_sec=0.05, initial_delay_sec=0.0, on_failure=failures.append
    )

    verifier.start()
    try:
        _wait_until(lambda: verifier.last_result is not None)
        assert verifier.last_result is not None and verifier.last_result.ok

        _damage_event(database, 1)

        _wait_until(lambda: not verifier.last_result.ok)  # type: ignore[union-attr]
        _wait_until(lambda: bool(failures))
    finally:
        verifier.stop()

    assert verifier.failures_total >= 1
    assert failures[0].errors[0].event_id == 1


def test_stop_joins_the_timer_thread() -> None:
    verifier = _make_verifier(AuditLog(mem_database()), interval_sec=0.05)
    verifier.start()
    verifier.stop()

    assert verifier._thread is None  # noqa: SLF001 — the lifecycle contract


def test_a_failure_callback_that_raises_does_not_kill_the_timer() -> None:
    """The reporting path must not become a second way to lose the finding."""
    database, audit = _audit_with_events()
    _damage_event(database, 3)

    def explode(_result: ChainVerification) -> None:
        raise RuntimeError("downstream sink is broken")

    verifier = _make_verifier(
        audit, interval_sec=0.05, initial_delay_sec=0.0, on_failure=explode
    )
    verifier.start()
    try:
        _wait_until(lambda: verifier.failures_total >= 2)
    finally:
        verifier.stop()

    # The timer kept replaying past the first broken callback.
    assert verifier.failures_total >= 2


# ── composition root wiring ────────────────────────────────────────────────


def test_chain_verification_is_scheduled_on_unsigned_deployments() -> None:
    """The default deployment has no hub key, so this timer is the only check.

    ``_start_anchor_verification`` correctly returns None here — there is no
    signature to verify.  That used to leave *no* scheduled integrity check at
    all; this is the one that runs instead.
    """
    import mercure_gateway.main as main_mod

    database, audit = _audit_with_events()

    verifier = main_mod._start_chain_verification(audit)
    try:
        assert verifier is not None
        verifier.verify_now()
        assert verifier.last_result is not None
        assert verifier.last_result.ok is True
    finally:
        assert verifier is not None
        verifier.stop()


def test_a_broken_chain_becomes_an_audit_event() -> None:
    """The finding reaches the hub feed, not just a local log line.

    A tampered audit log cannot suppress the copy of the finding that already
    shipped, so recording it in the chain (which the streamer then ships) is
    what makes the check worth running on an unmanned box.
    """
    import mercure_gateway.main as main_mod

    database, audit = _audit_with_events()
    _damage_event(database, 3)

    verifier = main_mod._start_chain_verification(audit)
    try:
        assert verifier is not None
        verifier.verify_now()
    finally:
        verifier.stop()  # type: ignore[union-attr]

    rows = database.list_audit_events()
    events = [r["event"] for r in rows]
    assert AUDIT_CHAIN_FAILED in events
    detail = json.loads(
        next(r["detail"] for r in rows if r["event"] == AUDIT_CHAIN_FAILED)
    )
    assert detail["errors"]


def test_the_same_chain_failure_is_recorded_once() -> None:
    """A persistent break is re-found every pass but recorded once.

    The chain verifier measures the chain it also writes to.  Recording a new
    event per pass would grow the log it is checking at ~288 events/day for a
    300s cadence — the reporting would itself become the damage.  Every pass
    still fails (the finding stays live in the metrics); only the audit event
    is deduped on a stable failure signature.

    Note the consequence this test pins: the recorded finding is appended onto
    a chain whose head is already broken, so its own link can never verify —
    later replays report it alongside the original damage.  That is inherent
    (the chain cannot self-heal) and bounded (one event per distinct finding).
    """
    import mercure_gateway.main as main_mod

    database, audit = _audit_with_events()
    _damage_event(database, 3)

    verifier = main_mod._start_chain_verification(audit)
    try:
        assert verifier is not None
        verifier.verify_now()
        verifier.verify_now()
        verifier.verify_now()
    finally:
        verifier.stop()  # type: ignore[union-attr]

    events = [r["event"] for r in database.list_audit_events()]
    assert events.count(AUDIT_CHAIN_FAILED) == 1
    # ...but every pass still counts as a live failure — the gauge must not
    # go green just because the first finding was already written down. The
    # counter tallies broken links, not passes: 1 found by the first pass,
    # then 2 per pass after the recorded finding became a broken link too.
    assert verifier.failures_total == 5  # type: ignore[union-attr]
