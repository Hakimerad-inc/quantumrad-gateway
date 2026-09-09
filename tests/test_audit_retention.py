"""S04-T7 (RED): audit log retention (PRD §7).

Old events are pruned per a configurable retention window (default 1 year),
the chain is re-anchored so verification still passes post-prune, and the
prune is itself recorded as an audit event.  Pruning never touches the spool's
study data (delivered-data-safe).
"""

from __future__ import annotations

from mercure_gateway.audit import AuditLog
from mercure_gateway.audit.events import PRUNE_AUDIT
from mercure_gateway.config import default_config
from mercure_gateway.spool import Spool
from mercure_gateway.spool.db import mem_database


def _backdate(audit: AuditLog, event_ids: list[int], days: int) -> None:
    """Backdate events so they fall outside the retention window.

    The audit_events table is append-only at the SQL layer, so the triggers
    are dropped for the (test-only) timestamp rewrite.
    """
    audit._conn.execute("DROP TRIGGER IF EXISTS audit_events_no_update")
    audit._conn.execute("DROP TRIGGER IF EXISTS audit_events_no_delete")
    for eid in event_ids:
        audit._conn.execute(
            "UPDATE audit_events SET ts = datetime(ts, ?) WHERE id = ?",
            (f"-{days} days", eid),
        )
    audit._conn.execute(
        "CREATE TRIGGER IF NOT EXISTS audit_events_no_update "
        "BEFORE UPDATE ON audit_events BEGIN "
        "SELECT RAISE(ABORT, 'audit_events is append-only'); END"
    )
    audit._conn.execute(
        "CREATE TRIGGER IF NOT EXISTS audit_events_no_delete "
        "BEFORE DELETE ON audit_events BEGIN "
        "SELECT RAISE(ABORT, 'audit_events is append-only'); END"
    )


def test_retention_default_is_one_year() -> None:
    """PRD §7: log retention defaults to 1 year."""
    cfg = default_config()
    assert cfg.audit.retention_days == 365


def test_prune_removes_old_events() -> None:
    audit = AuditLog(mem_database())
    old = audit.append("STUDY_RECEIVED", {"study_uid": "1.2.3"})
    audit.append("FORWARD_COMPLETE", {"study_uid": "1.2.3"})
    _backdate(audit, [old], 400)  # 400 days old → outside the 1-year window

    pruned = audit.prune(older_than_days=365)

    assert pruned == 1
    remaining = [e.event for e in audit.list_events()]
    assert "STUDY_RECEIVED" not in remaining
    assert "FORWARD_COMPLETE" in remaining


def test_prune_keeps_recent_events() -> None:
    audit = AuditLog(mem_database())
    old = audit.append("STUDY_RECEIVED")
    _backdate(audit, [old], 400)
    audit.append("STUDY_QUEUED")  # fresh — must survive

    audit.prune(older_than_days=365)

    events = [e.event for e in audit.list_events()]
    assert "STUDY_QUEUED" in events


def test_chain_verifies_after_prune() -> None:
    """Deleting the oldest events must not break the hash chain (re-anchored)."""
    audit = AuditLog(mem_database())
    old1 = audit.append("STUDY_RECEIVED")
    old2 = audit.append("STUDY_QUEUED")
    _backdate(audit, [old1, old2], 400)
    audit.append("FORWARD_START")
    audit.append("FORWARD_COMPLETE")

    audit.prune(older_than_days=365)

    ok, errors = audit.verify()
    assert ok is True
    assert errors == []


def test_prune_records_audit_event() -> None:
    """The prune action itself is appended so operators can see retention ran."""
    audit = AuditLog(mem_database())
    old = audit.append("STUDY_RECEIVED")
    _backdate(audit, [old], 400)

    audit.prune(older_than_days=365)

    events = audit.list_events()
    assert events[0].event == PRUNE_AUDIT
    assert events[0].detail["pruned"] == 1
    ok, _ = audit.verify()
    assert ok is True


def test_prune_is_delivered_data_safe() -> None:
    """Pruning the audit log never touches spool study rows (delivered data)."""
    db = mem_database()
    audit = AuditLog(db)
    spool = Spool(db)
    study_id = spool.receive("1.2.3.4")
    spool._db.set_study_state(study_id, "SENT")
    old = audit.append("STUDY_RECEIVED")
    _backdate(audit, [old], 400)

    audit.prune(older_than_days=365)

    assert spool._db.get_study(study_id) is not None
    assert spool.state(study_id).value == "SENT"


def test_prune_within_window_removes_nothing() -> None:
    audit = AuditLog(mem_database())
    audit.append("STUDY_RECEIVED")
    audit.append("STUDY_QUEUED")

    pruned = audit.prune(older_than_days=365)

    assert pruned == 0
    events = [e.event for e in audit.list_events()]
    assert events.count("STUDY_RECEIVED") == 1
    assert events.count("STUDY_QUEUED") == 1
    assert events[0] == PRUNE_AUDIT  # prune is always recorded


def test_prune_event_records_range_and_heads() -> None:
    """PRUNE_AUDIT carries the deleted id/ts range and the chain head before
    and after, so a history rewrite is visible and checkable against an
    externally anchored head (review M4)."""
    audit = AuditLog(mem_database())
    old = audit.append("STUDY_RECEIVED")
    _backdate(audit, [old], 400)
    head_before = audit.head_hash()

    audit.prune(older_than_days=365)

    detail = audit.list_events()[0].detail
    assert detail["pruned_range"]["min_id"] == old
    assert detail["pruned_range"]["max_id"] == old
    assert detail["pruned_range"]["min_ts"] is not None
    assert detail["head_before"] == head_before
    # The PRUNE_AUDIT event is appended *after* the re-anchored chain, so the
    # head it recorded is the parent of the prune event itself — verify the
    # newest event's hash recomputes from head_after.
    newest = audit.list_events()[0]
    ok, _ = audit.verify()
    assert ok is True
    # head_after is the chain head at the moment the re-anchor finished; the
    # PRUNE_AUDIT event then chained from it. Verify by recomputing.
    from mercure_gateway.audit import _compute_hash
    computed = _compute_hash(
        detail["head_after"], newest.ts, newest.event,
        __import__("json").dumps(newest.detail, separators=(",", ":"), sort_keys=True),
        newest.user,
    )
    assert computed == newest.hash


def test_prune_nothing_still_records_null_range() -> None:
    """A no-op prune records a null range with matching heads (idempotent)."""
    audit = AuditLog(mem_database())
    audit.append("STUDY_RECEIVED")

    audit.prune(older_than_days=365)

    detail = audit.list_events()[0].detail
    assert detail["pruned"] == 0
    assert detail["pruned_range"] == {
        "min_id": None, "max_id": None, "min_ts": None, "max_ts": None,
    }
    assert detail["head_before"] == detail["head_after"]
