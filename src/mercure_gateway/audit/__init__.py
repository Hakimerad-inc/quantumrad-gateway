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
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

_GENESIS_HASH = hashlib.sha256(b"mercure-gateway-genesis").hexdigest()

# Patient-identifying fields that are stripped from audit exports when
# ``phi_scope`` is ``"minimal"`` (the default; review M5).
_PHI_FIELDS = ("patient_name", "mrn", "patient_id")


def redact_phi(detail: dict[str, Any], phi_scope: str = "minimal") -> dict[str, Any]:
    """Return a copy of *detail* with PHI fields removed when scope is minimal.

    This is the single implementation of PHI redaction for audit exports so
    that ``/audit/export``, ``/diagnostics/export``, and ``export_bundle``
    behave identically (review M5).
    """
    if phi_scope != "minimal":
        return detail
    copy = dict(detail)
    for field in _PHI_FIELDS:
        copy.pop(field, None)
    return copy

__all__ = [
    "AuditLog",
    "AuditEvent",
    "ChainError",
    "anchor_head_to_file",
    "AnchorError",
    "AnchorVerification",
    "AnchorVerifier",
    "SignedHeadAnchorer",
    "verify_anchor_signatures",
    "ChainVerification",
    "ChainVerifier",
]


# Detail keys holding a Study Instance UID. When present, the UID is copied
# into the ``audit_events.study_uid`` column so the per-study timeline can
# query it *exactly* — the previous LIKE-on-JSON substring match attached
# other studies' events because DICOM UIDs are prefix-nested (review M7).
_STUDY_UID_KEYS = ("study_uid",)


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


def anchor_head_to_file(path: Path) -> Callable[[str], None]:
    """Return a head-anchorer that appends each head hash to *path* (review M4).

    The file is the external, append-only sink ``head_hash``'s docstring asks
    for: the DB's chain can be verified against the anchored heads, and
    rewriting the database alone cannot produce a chain that matches an
    already-anchored head. Appending (never rewriting) means historical heads
    survive even if an attacker edits the tail of the file. Best-effort by
    design — the callback is invoked on the append path, so any error is the
    caller's (``AuditLog._anchor_head``) to swallow and log.
    """
    path = Path(path)

    def _anchor(head: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(head + "\n")

    return _anchor


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
            self._db = None
        else:
            self._conn = db.connection()
            self._transaction = db.transaction
            self._db = db
        self._sink: Callable[[str, dict[str, Any], str | None], None] | None = None
        self._head_anchorer: Callable[[str], None] | None = None

    def set_sink(self, sink: Callable[[str, dict[str, Any], str | None], None] | None) -> None:
        """Attach a callback invoked after every successful :meth:`append`.

        Used by the composition root to stream audit events to the hub
        bookkeeper (the callback is the ``HubEventStreamer.feed``).  The sink
        runs *after* the append transaction commits, so events that were not
        persisted are never reported; a failing sink can never break the audit
        log (US-10 isolation invariant).
        """
        self._sink = sink

    def set_head_anchorer(self, anchorer: Callable[[str], None] | None) -> None:
        """Attach a callback that receives every new chain head hash.

        The local hash chain detects tampering by an attacker who cannot also
        rewrite the anchored copies; anchoring the head *outside* the database
        closes the rewrite-history-and-recompute attack (review M4). The
        anchorer runs after each append commits, alongside the event sink —
        a failing anchorer is logged, never raised (US-10 isolation).

        The composition root wires an append-only local file (last head wins);
        hub reporting (when enabled) additionally streams every event.
        """
        self._head_anchorer = anchorer

    def _anchor_head(self, event_id: int) -> None:
        """Run the head anchorer for the event just appended, best-effort."""
        if self._head_anchorer is None:
            return
        try:
            self._head_anchorer(self._head_hash(self._conn))
        except Exception:  # noqa: BLE001 — boundary: anchoring must not break audit
            logger.exception("head anchorer failed after event %d", event_id)

    def append(
        self, event: str, detail: dict[str, Any] | None = None, user: str | None = None
    ) -> int:
        """Append an event to the audit log and return its id.

        The chained hash is computed automatically: the previous event's hash
        is read *inside the same transaction* as the insert, so concurrent
        appends cannot fork the chain.
        """
        detail_json = json.dumps(detail or {}, separators=(",", ":"), sort_keys=True)
        study_uid = next(
            (str(detail[k]) for k in _STUDY_UID_KEYS if detail and detail.get(k)),
            None,
        )

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
                "INSERT INTO audit_events (ts, event, detail, user, hash, study_uid) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ts, event, detail_json, user, hash_, study_uid),
            )
            rowid = cur.lastrowid
            if rowid is None:  # pragma: no cover — sqlite always sets lastrowid
                raise RuntimeError("audit insert returned no rowid")
            return int(rowid)

        if self._transaction is not None:
            with self._transaction() as conn:
                rowid = _do(conn)
        else:
            # Legacy path: raw connection — serialize appends with a local lock.
            with _LEGACY_LOCK:
                rowid = _do(self._conn)
        self._notify_sink(event, detail, user)
        self._anchor_head(rowid)
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
        with self._db_lock():
            prev = self._conn.execute(
                "SELECT hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return prev["hash"] if prev is not None else _GENESIS_HASH

    def _db_lock(self) -> threading.Lock:
        """The Database read/write lock, or a local lock for raw connections.

        Read paths (``head_hash``/``list_events``/``export_bundle``) iterate
        the shared write connection, so they must hold the same lock as the
        writers — otherwise a concurrent append commits mid-iteration and the
        caller sees a chain state that never existed.

        :meth:`verify_iter` is the exception: it streams through
        :meth:`Database.iter_audit_events`, which steps a cursor on the
        per-thread *read-only* connection. SQLite hands that statement a WAL
        snapshot, so the walk sees one consistent state without taking the
        write lock — and without blocking every audit append for the duration
        of a full-table scan (see that method for why a tail append cannot
        corrupt the prefix already walked).
        """
        lock: threading.Lock = (
            self._transaction.__self__._lock if self._transaction is not None else _LEGACY_LOCK
        )
        return lock

    def verify(self) -> tuple[bool, list[ChainError]]:
        """Replay the chain and compare stored hashes.

        Returns ``(ok, errors)`` where ``ok`` is ``True`` when the chain is
        intact and ``errors`` is a list of :class:`ChainError` describing every
        broken link.  The *computed* hash is propagated between links, so a
        rewritten row (even with its stored hash recomputed) is detected at the
        next link.

        This is :meth:`verify_iter` materialized. It is the right call for the
        callers that want the whole list, and free for a healthy chain (which
        yields no errors); a corrupted log on an appliance that has run for
        months can yield an unbounded list, and the web endpoint deliberately
        consumes the lazy form instead (see ``verify_iter``).
        """
        errors = list(self.verify_iter())
        return len(errors) == 0, errors

    def verify_iter(self) -> Iterator[ChainError]:
        """Replay the chain lazily, yielding one :class:`ChainError` per broken link.

        Rows are streamed from :meth:`Database.iter_audit_events`, which steps a
        cursor one row at a time on the read-only connection, so the walk has
        bounded peak memory and takes no write lock. The previous
        implementation ran ``fetchall()`` under the write lock — one operator
        integrity check blocked every audit append for the duration of a
        full-table scan, and held the whole table in memory while it did.

        The *computed* hash is propagated between links, so a rewritten row
        (even with its stored hash recomputed) is detected at the next link.

        Callers that cannot tolerate an unbounded result (a JSON response body
        is one) must cap it themselves rather than calling ``list()`` on this
        generator — see ``/api/audit/verify`` in the web routes.
        """
        expected_hash = _GENESIS_HASH
        for row in self._iter_chain_rows():
            computed = _compute_hash(
                expected_hash, row["ts"], row["event"], row["detail"], row["user"]
            )
            if computed != row["hash"]:
                yield ChainError(
                    event_id=row["id"],
                    expected_hash=computed,
                    stored_hash=row["hash"],
                    reason="hash mismatch",
                )
            # Propagate the *computed* hash: a rewritten row cannot forge the
            # chain forward even when its stored hash was also rewritten.
            expected_hash = computed

    def _iter_chain_rows(self) -> Iterator[sqlite3.Row]:
        """The chain rows oldest-first, streamed when a Database backs this log."""
        if self._db is not None:
            yield from self._db.iter_audit_events()
            return
        # Legacy raw connection: there is no Database to hand us a read-only
        # cursor, so the scan is materialized under the legacy lock. Callers
        # that need streaming must construct the AuditLog from a Database
        # (every non-test caller does — the web layer uses the spool's).
        with _LEGACY_LOCK:
            rows = self._conn.execute(
                "SELECT id, ts, event, detail, user, hash FROM audit_events ORDER BY id"
            ).fetchall()
        yield from rows

    def list_events(self, limit: int = 100, offset: int = 0) -> list[AuditEvent]:
        """Return the most recent audit events, newest first."""
        with self._db_lock():
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
        events: list[dict[str, Any]] = []
        with self._db_lock():
            rows = self._conn.execute(
                "SELECT id, ts, event, detail, user, hash FROM audit_events ORDER BY id"
            ).fetchall()
        for row in rows:
            detail = redact_phi(json.loads(row["detail"]), phi_scope)
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
        ``PRUNE_AUDIT`` event whose detail carries the deleted id/ts range and
        the chain head before and after, so the rewrite is visible and
        verifiable against an externally anchored head (review M4).
        """
        from mercure_gateway.audit.events import PRUNE_AUDIT
        from mercure_gateway.spool.db import _AUDIT_NO_DELETE, _AUDIT_NO_UPDATE

        def _do(conn: sqlite3.Connection) -> dict[str, Any]:
            head_before = self._head_hash(conn)
            # Range of what is about to be deleted: oldest/newest id + ts.
            range_row = conn.execute(
                """
                SELECT MIN(id) AS min_id, MAX(id) AS max_id, MIN(ts) AS min_ts, MAX(ts) AS max_ts
                FROM audit_events
                WHERE ts < datetime('now', ?)
                """,
                (f"-{older_than_days} days",),
            ).fetchone()
            pruned_range = (
                {
                    "min_id": int(range_row["min_id"]),
                    "max_id": int(range_row["max_id"]),
                    "min_ts": range_row["min_ts"],
                    "max_ts": range_row["max_ts"],
                }
                if range_row is not None and range_row["min_id"] is not None
                else {"min_id": None, "max_id": None, "min_ts": None, "max_ts": None}
            )
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
            head_after = self._head_hash(conn)
            return {
                "pruned": max(0, pruned),
                "older_than_days": older_than_days,
                "pruned_range": pruned_range,
                "head_before": head_before,
                "head_after": head_after,
            }

        if self._transaction is not None:
            with self._transaction() as conn:
                detail = _do(conn)
        else:
            with _LEGACY_LOCK:
                detail = _do(self._conn)
        self.append(PRUNE_AUDIT, detail)
        return int(detail["pruned"])

    def _head_hash(self, conn: sqlite3.Connection) -> str:
        """Chain head read on an explicit connection (prune's transaction)."""
        prev = conn.execute(
            "SELECT hash FROM audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return prev["hash"] if prev is not None else _GENESIS_HASH


# Serializes appends when AuditLog was built around a raw connection.
_LEGACY_LOCK = threading.Lock()


# Verification cadence shared by the two integrity timers (review P0-10):
# often enough that a broken audit chain surfaces between scrape intervals,
# rarely enough that a large log is not replayed constantly. The first pass
# is deferred so a boot with a cold cache is not charged for it.
_DEFAULT_VERIFY_INTERVAL_SEC = 300.0
_DEFAULT_VERIFY_INITIAL_DELAY_SEC = 5.0

R = TypeVar("R")


class _PeriodicVerifier[R]:
    """Runs a verification pass on a daemon timer and records the result.

    Shared by the audit's two integrity checks: the hub-signature verifier
    (authenticity — configured deployments) and the chain replay (integrity —
    always on). Both share one contract: a pass runs on a fixed cadence, never
    raises, holds the last result for the metrics route to read as a cheap
    field access, and fans a failure out to a callback that carries the finding
    into the audit stream an unmanned box is being watched by.

    Verification belongs on a timer, never in the scrape path — a full-table
    scan per scrape is a DoS vector — which is why the metrics route reads
    ``last_result`` instead of triggering a pass.
    """

    def __init__(
        self,
        *,
        interval_sec: float,
        initial_delay_sec: float,
        on_failure: Callable[[R], None] | None,
        thread_name: str,
    ) -> None:
        self._interval = interval_sec
        self._initial_delay = initial_delay_sec
        self._thread_name = thread_name
        self._on_failure = on_failure
        self._last: R | None = None
        self._failures_total = 0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def last_result(self) -> R | None:
        """The most recent pass; None until the first one completes."""
        return self._last

    @property
    def failures_total(self) -> int:
        """Verification failures recorded since process start."""
        return self._failures_total

    @property
    def interval_sec(self) -> float:
        """The cadence the timer runs at (logged once at boot)."""
        return self._interval

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background timer (idempotent)."""
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._work, name=self._thread_name, daemon=True
        )
        self._thread.start()

    def stop(self, *, join_timeout: float = 5.0) -> None:
        """Signal the timer to stop and join it, so shutdown leaves no thread."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)
            self._thread = None

    # ── machinery ────────────────────────────────────────────────────────

    def _record(self, result: R) -> None:
        """Bookkeep one pass and fan a failure out to the callback."""
        self._last = result
        if not getattr(result, "ok", False):
            self._failures_total += len(getattr(result, "errors", ())) or 1
            if self._on_failure is not None:
                try:
                    self._on_failure(result)
                except Exception:  # noqa: BLE001 — reporting must not kill the timer
                    logger.exception("verification failure callback raised")

    def _work(self) -> None:
        # Let the boot settle (and the first rows land) before the first pass;
        # afterwards the interval is the cadence an operator alerts on.
        self._stop_event.wait(self._initial_delay)
        while not self._stop_event.is_set():
            self.verify_now()
            self._stop_event.wait(self._interval)

    def verify_now(self) -> R:
        """Run one verification pass synchronously; records and reports it.

        Never raises: a verifier that took down its caller would be worse than
        a late finding, and every failure mode here is itself a finding.
        """
        raise NotImplementedError


@dataclass(frozen=True)
class ChainVerification:
    """One scheduled replay of the audit chain (review P0-10 remainder)."""

    ok: bool
    errors: tuple[ChainError, ...] = ()


class ChainVerifier(_PeriodicVerifier[ChainVerification]):
    """Replay the audit chain on a timer (review P0-10 remainder).

    ``AnchorVerifier`` guards the *authenticity* half of tamper evidence —
    hub-held Ed25519 signatures over each anchored chain head — but unsigned
    deployments (the default: no ``anchor_public_key`` configured) have no
    signature to check, and until this timer existed the chain's own hash
    replay had no scheduled caller at all. ``/api/audit/verify`` runs only
    when an operator asks for it, and deliberately not on every scrape (a
    full-table scan per scrape is a DoS vector). So on a stock box a broken
    chain — a partial write, a dropped trigger, a botched migration, a
    hand-edited row — sat undetected until a human thought to look.

    This replays the chain at the same cadence the anchor verifier runs, so
    the failure shows up in the metrics feed an operator is already alerting
    on. It needs no external key, so it runs on every deployment, signed or
    not.

    Honest threat-model limit: this is *accident* detection, not *attacker*
    detection. The chain is an unkeyed SHA-256 over public columns — whoever
    can write the database can recompute it end to end and the replay would
    pass. Only an anchor stored outside the database (the signed anchor file,
    or the bookkeeper's signature over each head) can detect a deliberate
    rewrite. What this *does* catch is every failure mode that is not a
    deliberate rewrite, on a box nobody is watching.
    """

    def __init__(
        self,
        audit: AuditLog,
        *,
        interval_sec: float = _DEFAULT_VERIFY_INTERVAL_SEC,
        initial_delay_sec: float = _DEFAULT_VERIFY_INITIAL_DELAY_SEC,
        on_failure: Callable[[ChainVerification], None] | None = None,
    ) -> None:
        super().__init__(
            interval_sec=interval_sec,
            initial_delay_sec=initial_delay_sec,
            on_failure=on_failure,
            thread_name="audit-chain-verifier",
        )
        self._audit = audit

    def verify_now(self) -> ChainVerification:
        try:
            # Drain the generator *before* the failure callback can append:
            # verify_iter() steps a cursor on the read-only connection while
            # the callback's append takes the write lock — overlapping them
            # would deadlock the timer against itself.
            errors = tuple(self._audit.verify_iter())
        except Exception:  # noqa: BLE001 — a crash here must not kill the timer
            logger.exception("audit chain verification failed unexpectedly")
            result = ChainVerification(ok=False)
        else:
            result = ChainVerification(ok=not errors, errors=errors)
        self._record(result)
        return result


# Re-exports: the signed anchorer composes anchor_head_to_file, so importing
# it from the package root must happen after this module defines it. Kept at
# the bottom to avoid a circular import (anchoring imports from here).
from mercure_gateway.audit.anchoring import (  # noqa: E402
    AnchorError,
    AnchorVerification,
    AnchorVerifier,
    SignedHeadAnchorer,
    verify_anchor_signatures,
)
