"""Append-only tamper-evident audit log (chained hash).

Each event is written to the ``audit_events`` table with a SHA-256 hash
computed over the previous event's hash, the timestamp, the event name, the
detail JSON and the user field.  Tampering is detected by re-computing the
chain and comparing.

Encryption-at-rest (SQLCipher) note
------------------------------------
The PRD (§5.1, §6.1) specifies SQLCipher AES-256 for the database.  The
current implementation uses plain SQLite.  When the SQLCipher dependency is
added (``pysqlcipher3`` or ``sqlcipher3``), replace:

    >>> import sqlite3
    >>> conn = sqlite3.connect(path)

with:

    >>> from pysqlcipher3 import dbapi2 as sqlite3
    >>> conn = sqlite3.connect(path)
    >>> conn.execute("PRAGMA key = '...'")  # key from OS keyring

and keep every other line unchanged — the schema, queries, and chained hashing
logic are identical.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

_GENESIS_HASH = hashlib.sha256(b"mercure-gateway-genesis").hexdigest()

__all__ = ["AuditLog", "AuditEvent", "ChainError"]


@dataclass(frozen=True)
class AuditEvent:
    """A single deserialised audit event."""

    id: int
    ts: str
    event: str
    detail: dict[str, Any]
    user: str | None
    hash: str  # noqa: A003


@dataclass(frozen=True)
class ChainError:
    """Describes a single broken link in the audit chain."""

    event_id: int
    expected_hash: str
    stored_hash: str
    reason: str


def _compute_hash(prev_hash: str, ts: str, event: str, detail_json: str, user: str | None) -> str:
    payload = prev_hash + ts + event + detail_json + (user or "")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AuditLog:
    """Append-only chained-hash audit log backed by the SQLite database.

    Typical usage::

        db = Database("audit.db")
        db.initialize()
        audit = AuditLog(db.connection())
        audit.append("STUDY_RECEIVED", {"study_uid": "1.2.3.4"})
        audit.append("FORWARD_START", {"study_uid": "1.2.3.4", "target": "hub"})
        ok, errors = audit.verify()
        assert ok
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    def append(
        self, event: str, detail: dict[str, Any] | None = None, user: str | None = None
    ) -> int:
        """Append an event to the audit log and return its id.

        The chained hash is computed automatically: the previous event's hash
        is read from the database (or :data:`_GENESIS_HASH` for the first event).
        """
        detail_json = json.dumps(detail or {}, separators=(",", ":"), sort_keys=True)
        prev = self._conn.execute(
            "SELECT hash FROM audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        prev_hash = prev["hash"] if prev is not None else _GENESIS_HASH
        ts = self._conn.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now') AS ts_str"
        ).fetchone()["ts_str"]
        hash_ = _compute_hash(prev_hash, ts, event, detail_json, user)
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO audit_events (ts, event, detail, user, hash) VALUES (?, ?, ?, ?, ?)",
                (ts, event, detail_json, user, hash_),
            )
            rowid = cur.lastrowid
            assert rowid is not None
        return int(rowid)

    def verify(self) -> tuple[bool, list[ChainError]]:
        """Replay the chain and compare stored hashes.

        Returns ``(ok, errors)`` where ``ok`` is ``True`` when the chain is
        intact and ``errors`` is a list of :class:`ChainError` describing every
        broken link.
        """
        errors: list[ChainError] = []
        expected_hash = _GENESIS_HASH
        for row in self._conn.execute(
            "SELECT id, ts, event, detail, user, hash FROM audit_events ORDER BY id"
        ):
            computed = _compute_hash(
                expected_hash, row["ts"], row["event"], row["detail"], row["user"]
            )
            if computed != row["hash"]:
                errors.append(
                    ChainError(
                        event_id=row["id"],
                        expected_hash=computed,
                        stored_hash=row["hash"],
                        reason="hash mismatch",
                    )
                )
            expected_hash = row["hash"]
        return len(errors) == 0, errors

    def list_events(self, limit: int = 100, offset: int = 0) -> list[AuditEvent]:
        """Return the most recent audit events, newest first."""
        rows = self._conn.execute(
            "SELECT id, ts, event, detail, user, hash "
            "FROM audit_events ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [
            AuditEvent(
                id=r["id"],
                ts=r["ts"],
                event=r["event"],
                detail=json.loads(r["detail"]),
                user=r["user"],
                hash=r["hash"],
            )
            for r in rows
        ]