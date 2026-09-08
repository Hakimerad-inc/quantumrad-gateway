"""Append-only tamper-evident audit log (chained hash).

Each event is written to the ``audit_events`` table with a SHA-256 hash
computed over the previous event's hash, the timestamp, the event name, the
detail JSON and the user field.  Tampering is detected by re-computing the
chain and comparing each link against the *previous computed* hash (not the
stored one — rewriting a row together with its stored hash cannot forge the
chain).

Integrity notes
---------------
- Appends are serialized inside a ``BEGIN IMMEDIATE`` transaction with the
  previous-hash read, so concurrent writers cannot fork the chain.
- The ``audit_events`` table is append-only at the SQL layer (``BEFORE
  UPDATE``/``BEFORE DELETE`` triggers abort; see ``spool.db``).
- The hash is *unkeyed* SHA-256: a determined local attacker with DB write
  access can still rewrite history and recompute the chain.  Real
  tamper-evidence requires anchoring chain heads outside the database (the
  hub bookkeeper, PRD §6.1) — ``head_hash``/``export`` exist for that.

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
import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

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
        audit = AuditLog(db)
        audit.append("STUDY_RECEIVED", {"study_uid": "1.2.3.4"})
        audit.append("FORWARD_START", {"study_uid": "1.2.3.4", "target": "hub"})
        ok, errors = audit.verify()
        assert ok
    """

    def __init__(self, db: sqlite3.Connection | Any) -> None:
        # Accepts a Database (preferred — shares its transaction/lock) or a
        # raw sqlite3.Connection (legacy callers/tests).
        if isinstance(db, sqlite3.Connection):
            self._conn = db
            self._transaction = None
        else:
            self._conn = db.connection()
            self._transaction = db.transaction
        self._sink: Callable[[str, dict[str, Any], str | None], None] | None = None

    def set_sink(self, sink: Callable[[str, dict[str, Any], str | None], None] | None) -> None:
        """Attach a callback invoked after every successful :meth:`append`.

        Used by the composition root to stream audit events to the hub
        bookkeeper (the callback is the ``HubEventStreamer.feed``).  The sink
        runs *after* the append transaction commits, so events that were not
        persisted are never reported; a failing sink can never break the audit
        log (US-10 isolation invariant).
        """
        self._sink = sink

    def append(
        self, event: str, detail: dict[str, Any] | None = None, user: str | None = None
    ) -> int:
        """Append an event to the audit log and return its id.

        The chained hash is computed automatically: the previous event's hash
        is read *inside the same transaction* as the insert, so concurrent
        appends cannot fork the chain.
        """
        detail_json = json.dumps(detail or {}, separators=(",", ":"), sort_keys=True)

        def _do(conn: sqlite3.Connection) -> int:
            prev = conn.execute(
                "SELECT hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            prev_hash = prev["hash"] if prev is not None else _GENESIS_HASH
            ts = conn.execute(
                "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now') AS ts_str"
            ).fetchone()["ts_str"]
            hash_ = _compute_hash(prev_hash, ts, event, detail_json, user)
            cur = conn.execute(
                "INSERT INTO audit_events (ts, event, detail, user, hash) VALUES (?, ?, ?, ?, ?)",
                (ts, event, detail_json, user, hash_),
            )
            rowid = cur.lastrowid
            assert rowid is not None
            return int(rowid)

        if self._transaction is not None:
            with self._transaction() as conn:
                rowid = _do(conn)
        else:
            # Legacy path: raw connection — serialize appends with a local lock.
            with _LEGACY_LOCK:
                rowid = _do(self._conn)
        self._notify_sink(event, detail, user)
        return rowid

    def _notify_sink(self, event: str, detail: dict[str, Any] | None, user: str | None) -> None:
        if self._sink is None:
            return
        try:
            self._sink(event, dict(detail or {}), user)
        except Exception:  # noqa: BLE001 — boundary: reporting must not break audit
            logger.exception("hub event sink failed for %s", event)

    def head_hash(self) -> str:
        """Return the hash of the newest event (or the genesis hash).

        Anchor this value outside the database (hub bookkeeper / offline
        export) for tamper evidence against a local DB-writing attacker.
        """
        prev = self._conn.execute(
            "SELECT hash FROM audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return prev["hash"] if prev is not None else _GENESIS_HASH

    def verify(self) -> tuple[bool, list[ChainError]]:
        """Replay the chain and compare stored hashes.

        Returns ``(ok, errors)`` where ``ok`` is ``True`` when the chain is
        intact and ``errors`` is a list of :class:`ChainError` describing every
        broken link.  The *computed* hash is propagated between links, so a
        rewritten row (even with its stored hash recomputed) is detected at the
        next link.
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
            # Propagate the *computed* hash: a rewritten row cannot forge the
            # chain forward even when its stored hash was also rewritten.
            expected_hash = computed
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

    def export_bundle(self, config: Any) -> str:
        """Serialize a redacted export bundle (config + audit log) as JSON.

        US-07 / §7: the bundle contains the gateway configuration with all
        secrets redacted (via :func:`mercure_gateway.redact.redact_config`) and
        every audit event as structured JSON including its chain hash and the
        current :meth:`head_hash` for offline tamper evidence.

        PHI scoping (§6.4): when ``config.audit.phi_scope`` is ``"minimal"``
        (the default), patient-identifying detail keys (``patient_name``,
        ``mrn``, ``patient_id``) are stripped from the exported events.
        """
        from mercure_gateway.redact import redact_config

        config_data = json.loads(config.model_dump_json())
        redacted = redact_config(config_data)

        phi_scope = getattr(config.audit, "phi_scope", "minimal")
        phf_fields = ("patient_name", "mrn", "patient_id")
        events: list[dict[str, Any]] = []
        for row in self._conn.execute(
            "SELECT id, ts, event, detail, user, hash FROM audit_events ORDER BY id"
        ):
            detail = json.loads(row["detail"])
            if phi_scope == "minimal":
                for field in phf_fields:
                    detail.pop(field, None)
            events.append(
                {
                    "id": row["id"],
                    "ts": row["ts"],
                    "event": row["event"],
                    "detail": detail,
                    "user": row["user"],
                    "hash": row["hash"],
                }
            )

        bundle = {
            "config": redacted,
            "audit": {
                "events": events,
                "count": len(events),
                "head_hash": self.head_hash(),
            },
            "exported_at": self._now(),
        }
        return json.dumps(bundle, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _now() -> str:
        """ISO-8601 UTC timestamp for export metadata."""
        import datetime

        return datetime.datetime.now(datetime.UTC).isoformat()

    def prune(self, older_than_days: int) -> int:
        """Delete audit events older than *older_than_days* and re-anchor the chain.

        Returns the number of events deleted.  The append-only triggers are
        dropped around the delete, the surviving events' hashes are recomputed
        from the genesis hash (so :meth:`verify` still passes — deleting the
        oldest events would otherwise break the link they anchor), and the
        triggers are recreated.  The prune action itself is recorded as a
        ``PRUNE_AUDIT`` event so operators can see retention ran (PRD §7).
        """
        from mercure_gateway.audit.events import PRUNE_AUDIT
        from mercure_gateway.spool.db import _AUDIT_NO_DELETE, _AUDIT_NO_UPDATE

        def _do(conn: sqlite3.Connection) -> int:
            conn.execute("DROP TRIGGER IF EXISTS audit_events_no_update")
            conn.execute("DROP TRIGGER IF EXISTS audit_events_no_delete")
            cur = conn.execute(
                "DELETE FROM audit_events WHERE ts < datetime('now', ?)",
                (f"-{older_than_days} days",),
            )
            pruned = cur.rowcount
            # Re-anchor the chain from genesis so verification still passes.
            rows = conn.execute(
                "SELECT id, ts, event, detail, user FROM audit_events ORDER BY id"
            ).fetchall()
            expected = _GENESIS_HASH
            for row in rows:
                computed = _compute_hash(
                    expected, row["ts"], row["event"], row["detail"], row["user"]
                )
                conn.execute(
                    "UPDATE audit_events SET hash = ? WHERE id = ?", (computed, row["id"])
                )
                expected = computed
            conn.execute(_AUDIT_NO_UPDATE)
            conn.execute(_AUDIT_NO_DELETE)
            return pruned

        if self._transaction is not None:
            with self._transaction() as conn:
                pruned = _do(conn)
        else:
            with _LEGACY_LOCK:
                pruned = _do(self._conn)
        self.append(PRUNE_AUDIT, {"pruned": pruned, "older_than_days": older_than_days})
        return pruned


# Serializes appends when AuditLog was built around a raw connection.
_LEGACY_LOCK = threading.Lock()
