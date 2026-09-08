"""SQLite store-and-forward database (spool + audit).

Schema per PRD §5.4 with ``studies``, ``task_routing``, ``reports`` and
``audit_events`` tables. Connections are configured for WAL journaling, a
``busy_timeout`` and foreign-key enforcement.

Concurrency note:
- One connection is shared by receiver/forwarder/web threads (``:memory:``
  databases cannot be shared across connections), so **all** access is
  serialized through a re-entrant lock. Multi-statement operations run inside
  ``BEGIN IMMEDIATE`` transactions (see :meth:`Database.transaction`) so a
  crash cannot leave half-applied state (PRD §3.4 store-before-acknowledge).

Security note (per the sqlite-database-expert skill):
- All queries use parameterized statements; user-supplied values are never
  interpolated into SQL.
- ``audit_events`` is append-only: ``BEFORE UPDATE``/``BEFORE DELETE``
  triggers abort any tampering attempt at the SQL layer.

Encryption-at-rest (SQLCipher) is a documented stub only: SQLite connections
here are plain. See the ``audit`` module docstring for the encryption note.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

SCHEMA_VERSION = 4


class DatabaseEncryptionError(Exception):
    """Raised when an encrypted database is opened with a missing or wrong key."""


# Append-only triggers on audit_events, extracted as constants so retention
# pruning (AuditLog.prune) can drop and recreate them around the delete.
_AUDIT_NO_UPDATE = """
CREATE TRIGGER IF NOT EXISTS audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;
"""

_AUDIT_NO_DELETE = """
CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;
"""

# Order of index creation matters only for readability; SQLite is fine with this.
SCHEMA_SQL = (
    """
CREATE TABLE IF NOT EXISTS studies (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    study_uid              TEXT NOT NULL UNIQUE,
    accession              TEXT,
    mrn                    TEXT,
    patient_name           TEXT,
    modality               TEXT,
    study_description      TEXT,
    study_date             TEXT,
    num_series             INTEGER NOT NULL DEFAULT 0,
    num_instances          INTEGER NOT NULL DEFAULT 0,
    state                  TEXT NOT NULL DEFAULT 'RECEIVED',
    created_at             DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at             DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    retention_delivered_at DATETIME
);

CREATE TABLE IF NOT EXISTS task_routing (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id    INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
    target_name TEXT NOT NULL,
    target_type TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'waiting',
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (study_id, target_name)
);

CREATE TABLE IF NOT EXISTS reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id      INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
    accession     TEXT,
    study_uid     TEXT NOT NULL,
    report_type   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    file_path     TEXT,
    sop_class_uid TEXT,
    retrieved_at  DATETIME
);

CREATE TABLE IF NOT EXISTS audit_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    event   TEXT NOT NULL,
    detail  TEXT NOT NULL DEFAULT '{}',
    user    TEXT,
    hash    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_studies_accession   ON studies(accession);
CREATE INDEX IF NOT EXISTS idx_studies_state       ON studies(state);
CREATE INDEX IF NOT EXISTS idx_studies_created_at  ON studies(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_routing_status ON task_routing(status);
CREATE INDEX IF NOT EXISTS idx_task_routing_study  ON task_routing(study_id);
CREATE INDEX IF NOT EXISTS idx_reports_study       ON reports(study_id);
CREATE INDEX IF NOT EXISTS idx_reports_status      ON reports(status);
CREATE INDEX IF NOT EXISTS idx_audit_ts            ON audit_events(ts);

-- Key-verifier meta table (at-rest encryption guard, ADR-0004).
CREATE TABLE IF NOT EXISTS db_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Per-instance storage tracking (v3): original transfer-syntax provenance
-- and sidecar presence for each persisted instance file (S02-T3/S02-T4).
CREATE TABLE IF NOT EXISTS instance_meta (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    study_uid           TEXT NOT NULL,
    series_uid          TEXT NOT NULL,
    instance_uid        TEXT NOT NULL UNIQUE,
    file_path           TEXT NOT NULL,
    received_syntax     TEXT NOT NULL,
    stored_syntax       TEXT NOT NULL,
    num_bytes           INTEGER NOT NULL DEFAULT 0,
    received_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_instance_meta_study ON instance_meta(study_uid);
"""
    + _AUDIT_NO_UPDATE
    + _AUDIT_NO_DELETE
)


def _rowid(cursor: sqlite3.Cursor) -> int:
    """Return the last insert rowid, or raise RuntimeError if unavailable."""
    rid = cursor.lastrowid
    if rid is None:
        raise RuntimeError("no row id available after INSERT")
    return rid


class Database:
    """Thread-safe wrapper over a single SQLite connection for spool + audit.

    All public methods serialize on an internal re-entrant lock (receiver,
    forwarder and web threads share this instance). Multi-statement invariants
    run inside :meth:`transaction` (``BEGIN IMMEDIATE``), so they are atomic.
    """

    def __init__(self, path: str | Path | None = None, *, encrypt_key: str | None = None) -> None:
        self._path = ":memory:" if path is None else str(path)
        self._lock = threading.RLock()
        self._encrypt_key = encrypt_key
        self._conn = self._connect()

    @property
    def path(self) -> str:
        """Filesystem path of the database, or ``":memory:"`` for in-memory."""
        return self._path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        # FULL, not NORMAL: under WAL, NORMAL skips the fsync at COMMIT, so a
        # transaction that has already been acknowledged can still be lost on
        # power loss or device yank. Study state is the source of truth for
        # "we have this data" (PRD §3.4), so it must be durable at commit
        # (review H1). The instance files are fsynced by Spool.store_instance.
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block inside a locked ``BEGIN IMMEDIATE`` transaction.

        Commits on success, rolls back on error. Nested use re-enters the
        lock and joins the outer transaction.
        """
        with self._lock:
            if self._conn.in_transaction:
                # Join the outer transaction (nested context manager).
                yield self._conn
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.rollback()
                raise
            self._conn.commit()

    def initialize(self) -> None:
        """Create the schema if it does not exist and record the schema version."""
        with self.transaction():
            self._conn.executescript(SCHEMA_SQL)
            self._migrate_reports_sop_class_uid()
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        # At-rest encryption guard: verify the provided key (or create the
        # verifier on first keyed open). Runs after schema creation so the
        # db_meta table exists (ADR-0004).
        self._verify_or_create_key()

    def _migrate_reports_sop_class_uid(self) -> None:
        """Add the ``sop_class_uid`` column to an existing ``reports`` table.

        v3 → v4 migration: the report rows gained the SOP class UID so the
        console can distinguish SR from PDF without re-parsing the file.  This
        is idempotent (skipped when the column already exists).
        """
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(reports)").fetchall()}
        if "sop_class_uid" not in columns:
            self._conn.execute("ALTER TABLE reports ADD COLUMN sop_class_uid TEXT")

    def _verify_or_create_key(self) -> None:
        """Enforce the at-rest encryption guard (ADR-0004).

        A keyed database stores a ``salt:hmac`` verifier in ``db_meta``.
        Opening it again requires the same key; opening it without a key (or
        with a different key) is refused with :class:`DatabaseEncryptionError`.
        A database that has never been keyed opens normally without a key.
        """
        if self._encrypt_key is None:
            row = self._conn.execute(
                "SELECT value FROM db_meta WHERE key = 'encryption_key_verifier'"
            ).fetchone()
            if row is not None:
                raise DatabaseEncryptionError(
                    "database is encrypted; an encryption key is required to open it"
                )
            return
        salt = os.urandom(16).hex()
        row = self._conn.execute(
            "SELECT value FROM db_meta WHERE key = 'encryption_key_verifier'"
        ).fetchone()
        if row is None:
            # First keyed open — store the verifier for later opens.
            digest = hmac.new(self._encrypt_key.encode(), salt.encode(), hashlib.sha256).hexdigest()
            verifier = f"{salt}:{digest}"
            self._conn.execute(
                "INSERT INTO db_meta (key, value) VALUES ('encryption_key_verifier', ?)",
                (verifier,),
            )
            return
        stored_salt, _, expected = row["value"].partition(":")
        actual = hmac.new(
            self._encrypt_key.encode(), stored_salt.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(actual, expected):
            raise DatabaseEncryptionError("invalid encryption key")

    @property
    def user_version(self) -> int:
        """The schema version recorded in the database (0 = fresh database)."""
        row = self._conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row is not None else 0

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            self._conn.close()

    def connection(self) -> sqlite3.Connection:
        """Expose the underlying connection (audit module writes here too).

        Callers must not run multi-statement work on it directly; prefer
        :meth:`transaction`.
        """
        return self._conn

    # --- studies ---------------------------------------------------------

    def insert_study(
        self,
        study_uid: str,
        accession: str | None = None,
        mrn: str | None = None,
        patient_name: str | None = None,
        modality: str | None = None,
        study_description: str | None = None,
        study_date: str | None = None,
        num_series: int = 0,
        num_instances: int = 0,
        state: str = "RECEIVED",
    ) -> int:
        """Insert a study row and return its id."""
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO studies (
                    study_uid, accession, mrn, patient_name, modality,
                    study_description, study_date, num_series, num_instances, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    study_uid,
                    accession,
                    mrn,
                    patient_name,
                    modality,
                    study_description,
                    study_date,
                    num_series,
                    num_instances,
                    state,
                ),
            )
        return _rowid(cur)

    def upsert_study_instance(
        self,
        study_uid: str,
        accession: str | None,
        mrn: str | None,
        patient_name: str | None,
        modality: str | None,
        *,
        new_instance: bool,
        new_series: bool,
    ) -> int:
        """Insert-or-update a study row on C-STORE receive, and return its id.

        Single atomic statement on the receive hot path. Semantics (review
        F6 — modalities routinely re-send instances after a timeout):

        - New row → created in state RECEIVED with counters 1/1.
        - Known study, NEW instance → counters advance, tags refreshed, and
          the study re-opens: any state (except SENDING, which may still be
          mid-dispatch) returns to RECEIVED.
        - DUPLICATE instance (already stored) → strict no-op for state and
          counters: a re-sent copy must never demote QUEUED/SENT/ERROR/FAILED
          studies to RECEIVED (which strands them) nor double-count.
        """
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO studies (
                    study_uid, accession, mrn, patient_name, modality,
                    num_series, num_instances, state
                ) VALUES (?, ?, ?, ?, ?, 1, 1, 'RECEIVED')
                ON CONFLICT(study_uid) DO UPDATE SET
                    accession     = COALESCE(excluded.accession, studies.accession),
                    mrn           = COALESCE(excluded.mrn, studies.mrn),
                    patient_name  = COALESCE(excluded.patient_name, studies.patient_name),
                    modality      = COALESCE(excluded.modality, studies.modality),
                    num_series    = studies.num_series + ?,
                    num_instances = studies.num_instances + ?,
                    state = CASE
                        WHEN studies.state = 'SENDING' THEN studies.state
                        WHEN ? = 0 THEN studies.state  -- duplicate: no-op
                        ELSE 'RECEIVED'
                    END,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    study_uid,
                    accession,
                    mrn,
                    patient_name,
                    modality,
                    1 if (new_instance and new_series) else 0,
                    1 if new_instance else 0,
                    1 if new_instance else 0,
                ),
            )
            # ``lastrowid`` is unreliable across ON CONFLICT branches — read
            # the row id explicitly (SELECT is cheap and always correct).
            row = conn.execute(
                "SELECT id FROM studies WHERE study_uid = ?", (study_uid,)
            ).fetchone()
        if row is None:
            raise RuntimeError("study upsert vanished")
        return int(row["id"])

    def get_study(self, study_id: int) -> sqlite3.Row | None:
        """Fetch a single study row by id, or ``None`` if absent."""
        with self._lock:
            return cast(
                sqlite3.Row | None,
                self._conn.execute("SELECT * FROM studies WHERE id = ?", (study_id,)).fetchone(),
            )

    def get_study_by_uid(self, study_uid: str) -> sqlite3.Row | None:
        """Fetch a single study row by Study Instance UID, or ``None``."""
        with self._lock:
            return cast(
                sqlite3.Row | None,
                self._conn.execute(
                    "SELECT * FROM studies WHERE study_uid = ?", (study_uid,)
                ).fetchone(),
            )

    def set_study_state(self, study_id: int, state: str) -> None:
        """Update a study's state and touch ``updated_at``."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE studies SET state = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (state, study_id),
            )

    def set_retention_delivered(self, study_id: int) -> None:
        """Stamp ``retention_delivered_at`` (delivery completed for retention)."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE studies SET retention_delivered_at = CURRENT_TIMESTAMP WHERE id = ?",
                (study_id,),
            )

    def list_purgable_delivered(self, retention_hours: int) -> list[sqlite3.Row]:
        """Return SENT studies delivered more than *retention_hours* ago.

        Hour granularity covers both the day-based default
        (``storage.retention_delivered_days`` converted by the caller) and the
        USB variant's aggressive ``usb_mode.retention_delivered_hours`` window.
        Only fully-delivered (state ``SENT``) studies with a delivery stamp are
        eligible; undelivered/FAILED/ERROR studies are never returned here
        (US-04: local copy is never auto-deleted unless delivered).
        """
        with self._lock:
            return self._conn.execute(
                """
                SELECT id, study_uid FROM studies
                WHERE state = 'SENT'
                  AND retention_delivered_at IS NOT NULL
                  AND retention_delivered_at < datetime('now', ?)
                """,
                (f"-{max(0, retention_hours)} hours",),
            ).fetchall()

    def delete_study(self, study_id: int) -> None:
        """Delete a study row (cascades to task_routing/reports)."""
        with self.transaction() as conn:
            conn.execute("DELETE FROM studies WHERE id = ?", (study_id,))

    def list_oldest_delivered(self) -> sqlite3.Row | None:
        """Return the oldest delivered (``SENT``) study, or ``None``.

        Used by the disk-full auto-purger: deletes happen oldest-first so
        recently delivered studies are kept on disk longest.  Undelivered /
        FAILED studies are never returned (US-04: never auto-delete unless
        delivered).
        """
        with self._lock:
            return cast(
                sqlite3.Row | None,
                self._conn.execute(
                    """
                    SELECT id, study_uid FROM studies
                    WHERE state = 'SENT'
                      AND retention_delivered_at IS NOT NULL
                    ORDER BY retention_delivered_at ASC, id ASC
                    LIMIT 1
                    """
                ).fetchone(),
            )

    def list_studies(
        self,
        state: str | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        """List studies, optionally filtered by state, newest first."""
        sql = "SELECT * FROM studies"
        params: list[Any] = []
        if state is not None:
            sql += " WHERE state = ?"
            params.append(state)
        sql += " ORDER BY created_at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def list_studies_with_route_counts(
        self,
        state: str | None = None,
        modality: str | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        """List studies with per-study route counts, newest first.

        Filters for ``state``/``modality`` are applied in SQL so pagination
        stays consistent.
        """
        sql = """
            SELECT s.*, COUNT(r.id) AS num_destinations
            FROM studies AS s
            LEFT JOIN task_routing AS r ON r.study_id = s.id
        """
        params: list[Any] = []
        conditions: list[str] = []
        if state is not None:
            conditions.append("s.state = ?")
            params.append(state)
        if modality is not None:
            conditions.append("s.modality = ?")
            params.append(modality)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " GROUP BY s.id ORDER BY s.created_at DESC, s.id DESC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def count_states(self) -> dict[str, int]:
        """Count studies grouped by lifecycle state (SQL aggregate)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) AS n FROM studies GROUP BY state"
            ).fetchall()
        return {row["state"]: int(row["n"]) for row in rows}

    def count_studies(self, state: str | None = None, modality: str | None = None) -> int:
        """Count studies matching the given ``state``/``modality`` filters.

        Used for pagination metadata (total rows) so the web admin can render
        page controls consistently with the filtered list.
        """
        sql = "SELECT COUNT(*) AS n FROM studies"
        params: list[Any] = []
        conditions: list[str] = []
        if state is not None:
            conditions.append("state = ?")
            params.append(state)
        if modality is not None:
            conditions.append("modality = ?")
            params.append(modality)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["n"]) if row else 0

    # --- task_routing ----------------------------------------------------

    def insert_route(
        self,
        study_id: int,
        target_name: str,
        target_type: str,
        status: str = "waiting",
    ) -> int:
        """Insert a routing task for a study/target pair and return its id.

        Re-inserting an existing (study_id, target_name) pair is a no-op
        (keeps the original task and returns its id).
        """
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO task_routing (study_id, target_name, target_type, status)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(study_id, target_name) DO NOTHING
                """,
                (study_id, target_name, target_type, status),
            )
            row = conn.execute(
                "SELECT id FROM task_routing WHERE study_id = ? AND target_name = ?",
                (study_id, target_name),
            ).fetchone()
        if row is None:
            raise RuntimeError("route insert vanished")
        return int(row["id"])

    def get_routes(self, study_id: int) -> list[sqlite3.Row]:
        """Return all routing tasks for a study."""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM task_routing WHERE study_id = ? ORDER BY id", (study_id,)
            ).fetchall()

    def count_routes_by_target(self) -> list[sqlite3.Row]:
        """Per-destination rollup across all studies (pipeline view).

        One row per (target_name, target_type, status) with live counts and
        the most recent activity timestamp — feeds the destination nodes.
        """
        with self._lock:
            return self._conn.execute(
                """
                SELECT target_name, target_type, status,
                       COUNT(*) AS n, MAX(updated_at) AS last_activity
                FROM task_routing
                GROUP BY target_name, target_type, status
                """
            ).fetchall()

    def list_recent_routes(self, target_name: str, *, limit: int = 20) -> list[sqlite3.Row]:
        """Latest studies routed to one destination (pipeline drill-down).

        Joins the study row for display fields; newest route activity first.
        """
        with self._lock:
            return self._conn.execute(
                """
                SELECT r.id AS route_id, r.study_id, r.target_type, r.status,
                       r.attempts, r.last_error, r.updated_at,
                       s.study_uid, s.accession, s.patient_name, s.modality
                FROM task_routing AS r
                JOIN studies AS s ON s.id = r.study_id
                WHERE r.target_name = ?
                ORDER BY r.updated_at DESC, r.id DESC
                LIMIT ?
                """,
                (target_name, limit),
            ).fetchall()

    def list_audit_for_study(self, study_uid: str, *, limit: int = 50) -> list[sqlite3.Row]:
        """Audit events whose detail JSON references *study_uid*.

        ``detail`` is a JSON string; study UIDs contain no characters that
        JSON escapes, so a substring match on the quoted value is exact. The
        LIKE pattern is escaped for ``%``/``_`` wildcards.
        """
        escaped = study_uid.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        with self._lock:
            return self._conn.execute(
                """
                SELECT id, ts, event, detail, user, hash FROM audit_events
                WHERE detail LIKE ? ESCAPE '\\'
                ORDER BY id DESC LIMIT ?
                """,
                (pattern, limit),
            ).fetchall()

    def _has_waiting_tasks(self) -> bool:
        """Cheap read: is there any waiting task at all?"""
        row = self._conn.execute(
            "SELECT 1 FROM task_routing WHERE status = 'waiting' LIMIT 1"
        ).fetchone()
        return row is not None

    def claim_next_tasks(self, limit: int = 1) -> list[sqlite3.Row]:
        """Concurrency-safe "claim next task" for the store-and-forward queue.

        Runs inside a ``BEGIN IMMEDIATE`` transaction so a concurrent worker
        cannot grab the same waiting task. Claimed tasks transition to
        ``sending`` and their study to ``SENDING``.
        """
        with self._lock:
            # Avoid taking the write lock when the queue is empty (2 Hz poll).
            if not self._has_waiting_tasks():
                return []
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    """
                    SELECT r.id AS route_id, r.study_id, r.target_name, r.target_type
                    FROM task_routing AS r
                    JOIN studies AS s ON s.id = r.study_id
                    WHERE r.status = 'waiting' AND s.state != 'FAILED'
                    ORDER BY r.id
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                if rows:
                    route_ids = [row["route_id"] for row in rows]
                    study_ids = list({row["study_id"] for row in rows})
                    self._conn.executemany(
                        """
                        UPDATE task_routing
                        SET status = 'sending', attempts = attempts + 1,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        [(rid,) for rid in route_ids],
                    )
                    self._conn.executemany(
                        "UPDATE studies SET state = 'SENDING', "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        [(sid,) for sid in study_ids],
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            return rows

    def claim_route(self, route_id: int) -> sqlite3.Row | None:
        """Claim one specific route (retry path); ``None`` if not claimable.

        Only succeeds when the route is ``waiting`` and its study is not
        ``FAILED``. Atomically bumps the attempt counter.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = cast(
                    sqlite3.Row | None,
                    self._conn.execute(
                        """
                        SELECT r.id AS route_id, r.study_id, r.target_name, r.target_type
                        FROM task_routing AS r
                        JOIN studies AS s ON s.id = r.study_id
                        WHERE r.id = ? AND r.status = 'waiting' AND s.state != 'FAILED'
                        """,
                        (route_id,),
                    ).fetchone(),
                )
                if row is not None:
                    self._conn.execute(
                        """
                        UPDATE task_routing
                        SET status = 'sending', attempts = attempts + 1,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (route_id,),
                    )
                    self._conn.execute(
                        "UPDATE studies SET state = 'SENDING', "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (row["study_id"],),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            return row

    def mark_route_sent(self, route_id: int) -> None:
        """Mark a routing task complete (delivered)."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE task_routing SET status = 'complete', "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (route_id,),
            )

    def mark_route_error(self, route_id: int, error: str) -> None:
        """Mark a routing task as errored and record the last error."""
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE task_routing
                SET status = 'error', last_error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (error, route_id),
            )

    def reset_route_waiting(self, route_id: int) -> None:
        """Return a failed/errored route to ``waiting`` for re-forward."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE task_routing SET status = 'waiting', "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (route_id,),
            )

    def reset_route_attempts(self, route_id: int) -> None:
        """Reset the retry budget for a route (manual re-forward)."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE task_routing SET attempts = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (route_id,),
            )

    def all_routes_complete(self, study_id: int) -> bool:
        """Return ``True`` when every route for the study is complete."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status = 'complete' THEN 1 ELSE 0 END) AS done
                FROM task_routing WHERE study_id = ?
                """,
                (study_id,),
            ).fetchone()
        return bool(row is not None and row["total"] == row["done"])

    # --- reports ---------------------------------------------------------

    def insert_report(
        self,
        study_id: int,
        study_uid: str,
        report_type: str,
        accession: str | None = None,
        status: str = "pending",
        file_path: str | None = None,
        sop_class_uid: str | None = None,
    ) -> int:
        """Insert a report row and return its id."""
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO reports (
                    study_id, study_uid, report_type, accession,
                    status, file_path, sop_class_uid
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (study_id, study_uid, report_type, accession, status, file_path, sop_class_uid),
            )
        return _rowid(cur)

    def set_report_status(
        self,
        report_id: int,
        status: str,
        file_path: str | None = None,
        sop_class_uid: str | None = None,
    ) -> None:
        """Update a report's status, optionally recording where it was stored."""
        with self.transaction() as conn:
            if file_path is None and sop_class_uid is None:
                conn.execute("UPDATE reports SET status = ? WHERE id = ?", (status, report_id))
            elif file_path is not None and sop_class_uid is not None:
                conn.execute(
                    """
                    UPDATE reports
                    SET status = ?, file_path = ?, sop_class_uid = ?,
                        retrieved_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (status, file_path, sop_class_uid, report_id),
                )
            else:
                # Partial update — build the SET clause dynamically (safe: the
                # values are bound parameters, only the column names vary).
                sets: list[str] = ["status = ?"]
                params: list[Any] = [status]
                if file_path is not None:
                    sets.append("file_path = ?")
                    params.append(file_path)
                if sop_class_uid is not None:
                    sets.append("sop_class_uid = ?")
                    params.append(sop_class_uid)
                params.append(report_id)
                conn.execute(f"UPDATE reports SET {', '.join(sets)} WHERE id = ?", params)

    def get_report(self, report_id: int) -> sqlite3.Row | None:
        """Fetch a single report row by id, or ``None`` if absent."""
        with self._lock:
            return cast(
                sqlite3.Row | None,
                self._conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone(),
            )

    def list_reports(
        self,
        status: str | None = None,
        report_type: str | None = None,
        study_uid: str | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        """List reports with optional filters, newest first."""
        sql = "SELECT * FROM reports"
        params: list[Any] = []
        conditions: list[str] = []
        if status is not None:
            conditions.append("status = ?")
            params.append(status)
        if report_type is not None:
            conditions.append("report_type = ?")
            params.append(report_type)
        if study_uid is not None:
            conditions.append("study_uid = ?")
            params.append(study_uid)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def list_instance_meta(self, study_uid: str) -> list[sqlite3.Row]:
        """Return per-instance storage rows for *study_uid* (oldest first)."""
        with self._lock:
            return self._conn.execute(
                """
                SELECT id, study_uid, series_uid, instance_uid, file_path,
                       received_syntax, stored_syntax, num_bytes, received_at
                FROM instance_meta WHERE study_uid = ? ORDER BY id
                """,
                (study_uid,),
            ).fetchall()

    def insert_instance_meta(
        self,
        *,
        study_uid: str,
        series_uid: str,
        instance_uid: str,
        file_path: str,
        received_syntax: str,
        stored_syntax: str,
        num_bytes: int,
    ) -> int:
        """Record per-instance storage provenance (v3 schema).

        Re-recording the same instance updates the row (idempotent).
        """
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO instance_meta (
                    study_uid, series_uid, instance_uid, file_path,
                    received_syntax, stored_syntax, num_bytes
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instance_uid) DO UPDATE SET
                    file_path       = excluded.file_path,
                    received_syntax = excluded.received_syntax,
                    stored_syntax   = excluded.stored_syntax,
                    num_bytes       = excluded.num_bytes
                """,
                (
                    study_uid,
                    series_uid,
                    instance_uid,
                    file_path,
                    received_syntax,
                    stored_syntax,
                    num_bytes,
                ),
            )
            row = conn.execute(
                "SELECT id FROM instance_meta WHERE instance_uid = ?",
                (instance_uid,),
            ).fetchone()
        if row is None:
            raise RuntimeError("instance_meta insert vanished")
        return int(row["id"])

    # --- audit_events ----------------------------------------------------

    def append_audit_event(self, event: str, detail: str, user: str | None, hash_: str) -> int:
        """Append an audit event with its chained hash and return its id."""
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO audit_events (event, detail, user, hash) VALUES (?, ?, ?, ?)",
                (event, detail, user, hash_),
            )
        return _rowid(cur)

    def list_audit_events(
        self,
        event: str | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        """List audit events, newest first, optionally filtered by event name."""
        sql = "SELECT id, ts, event, detail, user, hash FROM audit_events"
        params: list[Any] = []
        if event is not None:
            sql += " WHERE event = ?"
            params.append(event)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def iter_audit_events(self) -> Iterator[sqlite3.Row]:
        """Yield all audit events in append order (oldest first)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, event, detail, user, hash FROM audit_events ORDER BY id"
            ).fetchall()
        yield from rows


def open_database(path: str | Path | None, *, encrypt_key: str | None = None) -> Database:
    """Open a :class:`Database` and initialize its schema.

    Refuses to open a database written by a *newer* schema version; older
    versions are migrated forward (all current steps are idempotent).

    ``encrypt_key`` (ADR-0004): pass the at-rest encryption key to open an
    encrypted database.  A database opened with a key previously requires that
    same key on every later open.
    """
    db = Database(path, encrypt_key=encrypt_key)
    current = db.user_version
    if current > SCHEMA_VERSION:
        db.close()
        raise RuntimeError(
            f"database schema version {current} is newer than supported {SCHEMA_VERSION}"
        )
    db.initialize()
    return db


def mem_database() -> Database:
    """Open an in-memory database with schema initialized (for tests)."""
    return open_database(None)
