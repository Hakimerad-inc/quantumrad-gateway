"""Unit tests for the chained-hash audit log (tamper-evidence).

Tamper simulation notes: the ``audit_events`` table is append-only at the SQL
layer (BEFORE UPDATE/DELETE triggers abort any mutation), so the tamper tests
drop the triggers first — simulating an attacker with raw DB-file control.
Chain verification propagates the *computed* hash between links, so even a
rewritten row with a recomputed stored hash is caught at the next link.
"""

from __future__ import annotations

import pytest

from mercure_gateway.audit import AuditLog
from mercure_gateway.spool.db import mem_database


@pytest.fixture()
def audit() -> AuditLog:
    return AuditLog(mem_database())


def _disable_append_only(audit: AuditLog) -> None:
    """Drop the append-only triggers, simulating raw DB-file tampering."""
    audit._conn.execute("DROP TRIGGER IF EXISTS audit_events_no_update")
    audit._conn.execute("DROP TRIGGER IF EXISTS audit_events_no_delete")


def test_empty_chain_verifies(audit: AuditLog) -> None:
    ok, errors = audit.verify()
    assert ok is True
    assert errors == []


def test_append_chain_verifies(audit: AuditLog) -> None:
    audit.append("STUDY_RECEIVED", {"study_uid": "1.2.3.4"})
    audit.append("FORWARD_START", {"study_uid": "1.2.3.4", "target": "hub"})
    audit.append("FORWARD_COMPLETE", {"study_uid": "1.2.3.4", "target": "hub"})
    ok, errors = audit.verify()
    assert ok is True
    assert errors == []


def test_append_only_triggers_block_update(audit: AuditLog) -> None:
    """The SQL layer must reject UPDATE/DELETE on audit_events."""
    audit.append("STUDY_RECEIVED", {"study_uid": "1.2.3.4"})
    with pytest.raises(Exception):  # noqa: B017 — sqlite3.IntegrityError via trigger RAISE
        audit._conn.execute(
            "UPDATE audit_events SET detail = ? WHERE id = 1", ('{"study_uid":"9.9.9"}',)
        )
    with pytest.raises(Exception):  # noqa: B017
        audit._conn.execute("DELETE FROM audit_events WHERE id = 1")


def test_tamper_with_detail_detected(audit: AuditLog) -> None:
    audit.append("STUDY_RECEIVED", {"study_uid": "1.2.3.4"})
    _disable_append_only(audit)
    # Attack: mutate the stored detail of an existing event
    conn = audit._conn
    conn.execute("UPDATE audit_events SET detail = ? WHERE id = 1", ('{"study_uid":"9.9.9"}',))
    ok, errors = audit.verify()
    assert ok is False
    assert len(errors) == 1
    assert errors[0].reason == "hash mismatch"


def test_tamper_with_event_name_detected(audit: AuditLog) -> None:
    audit.append("STUDY_RECEIVED", {"study_uid": "1.2.3.4"})
    audit.append("FORWARD_START", {"study_uid": "1.2.3.4"})
    _disable_append_only(audit)
    conn = audit._conn
    conn.execute("UPDATE audit_events SET event = 'HACKED' WHERE id = 2")
    ok, errors = audit.verify()
    assert ok is False
    assert errors[0].event_id == 2


def test_tamper_breaks_chain_detected(audit: AuditLog) -> None:
    audit.append("STUDY_RECEIVED", {"study_uid": "1.2.3.4"})
    audit.append("FORWARD_START", {"study_uid": "1.2.3.4"})
    audit.append("FORWARD_COMPLETE", {"study_uid": "1.2.3.4"})
    _disable_append_only(audit)
    # Corrupt the middle hash; both the middle event and the next event break
    conn = audit._conn
    conn.execute("UPDATE audit_events SET hash = 'deadbeef' WHERE id = 2")
    ok, errors = audit.verify()
    assert ok is False
    assert len(errors) >= 1


def test_rewritten_row_with_recomputed_hash_detected(audit: AuditLog) -> None:
    """A row rewritten *together with* its stored hash still breaks the chain.

    verify() propagates the computed hash between links, so the next event's
    stored hash no longer matches — this is the defect-fix regression test
    (the old implementation chained on the stored hash and silently passed).
    """
    audit.append("STUDY_RECEIVED", {"study_uid": "1.2.3.4"})
    audit.append("FORWARD_START", {"study_uid": "1.2.3.4"})
    audit.append("FORWARD_COMPLETE", {"study_uid": "1.2.3.4"})
    _disable_append_only(audit)

    # Attacker rewrites event 2 and recomputes its own hash consistently
    import hashlib
    import json

    row = audit._conn.execute("SELECT * FROM audit_events WHERE id = 2").fetchone()
    forged_detail = json.dumps({"study_uid": "9.9.9"}, separators=(",", ":"), sort_keys=True)
    forged_hash = hashlib.sha256(
        (row["hash"] + row["ts"] + row["event"] + forged_detail + (row["user"] or "")).encode()
    ).hexdigest()
    # NOTE: the attacker would chain from the STORED prev hash — but the
    # verifier chains from the COMPUTED prev hash, which no longer matches.
    audit._conn.execute(
        "UPDATE audit_events SET detail = ?, hash = ? WHERE id = 2",
        (forged_detail, forged_hash),
    )
    ok, errors = audit.verify()
    assert ok is False
    assert any(e.event_id == 2 for e in errors)


def test_tail_deletion_documented_limitation(audit: AuditLog) -> None:
    """Deleting the LAST event leaves a verifying chain (nothing references
    the tail hash). This is a documented limitation — anchor chain heads
    outside the DB (head_hash → hub bookkeeper) to detect truncation."""
    audit.append("A")
    audit.append("B")
    _disable_append_only(audit)
    audit._conn.execute("DELETE FROM audit_events WHERE id = 2")
    ok, _ = audit.verify()
    assert ok is True  # documented limitation, not a pass condition to celebrate


def test_head_hash_tracks_latest(audit: AuditLog) -> None:
    from mercure_gateway.audit import _GENESIS_HASH

    assert audit.head_hash() == _GENESIS_HASH
    audit.append("A")
    first = audit.list_events()[0].hash
    assert audit.head_hash() == first


def test_events_returned_newest_first(audit: AuditLog) -> None:
    audit.append("A", {"n": 1})
    audit.append("B", {"n": 2})
    events = audit.list_events()
    assert [e.event for e in events] == ["B", "A"]


def test_user_attribution_stored(audit: AuditLog) -> None:
    event_id = audit.append("UI_ACTION", {"action": "reforward"}, user="alice")
    events = audit.list_events()
    assert events[0].id == event_id
    assert events[0].user == "alice"
