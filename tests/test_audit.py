"""Unit tests for the chained-hash audit log (tamper-evidence)."""

from __future__ import annotations

import pytest

from mercure_gateway.audit import AuditLog
from mercure_gateway.spool.db import mem_database


@pytest.fixture()
def audit() -> AuditLog:
    return AuditLog(mem_database().connection())


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


def test_tamper_with_detail_detected(audit: AuditLog) -> None:
    audit.append("STUDY_RECEIVED", {"study_uid": "1.2.3.4"})
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
    conn = audit._conn
    conn.execute("UPDATE audit_events SET event = 'HACKED' WHERE id = 2")
    ok, errors = audit.verify()
    assert ok is False
    assert errors[0].event_id == 2


def test_tamper_breaks_chain_detected(audit: AuditLog) -> None:
    audit.append("STUDY_RECEIVED", {"study_uid": "1.2.3.4"})
    audit.append("FORWARD_START", {"study_uid": "1.2.3.4"})
    audit.append("FORWARD_COMPLETE", {"study_uid": "1.2.3.4"})
    # Corrupt the middle hash; both the middle event and the next event break
    conn = audit._conn
    conn.execute("UPDATE audit_events SET hash = 'deadbeef' WHERE id = 2")
    ok, errors = audit.verify()
    assert ok is False
    assert len(errors) >= 1


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
