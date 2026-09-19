"""S09-T2 (P1-19): pure-SELECT queries off the write connection.

One connection plus one process-wide RLock serializes the receiver, the
forwarder, the web threads and the report poller — a slow admin-UI query
stalls a C-STORE receive. The fix routes the list/aggregate queries to a
per-thread read-only connection: a WAL reader sees the last committed
snapshot without taking the write lock.

These tests pin the three properties the fix is worth anything for, and the
two properties that must NOT change (``:memory:`` keeps working; the fallback
still answers correctly when a read-only connection cannot be opened).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from mercure_gateway.spool.db import Database, mem_database, open_database


def _filled(path: Path, *, n: int = 5) -> Database:
    db = open_database(path)
    for i in range(n):
        db.insert_study(f"1.2.3.{i}", state="RECEIVED" if i % 2 else "SENT")
    return db


# ══════════════════════════════════════════════════════════════════════════
# Correctness: the reader sees exactly the committed state
# ══════════════════════════════════════════════════════════════════════════


def test_routed_reads_return_committed_rows(tmp_path: Path) -> None:
    db = _filled(tmp_path / "spool.db")
    try:
        assert len(db.list_studies()) == 5
        assert db.count_studies() == 5
        assert db.count_states() == {"RECEIVED": 2, "SENT": 3}
        assert db.spool_num_bytes() == 0
        assert db.count_pending_hub_events() == 0
        assert db.load_pending_hub_events() == []
        assert db.list_audit_events() == []
        assert db.list_instance_meta("1.2.3.0") == []
        assert db.count_routes_by_target() == []
        assert db.list_recent_routes("nowhere") == []
        assert db.list_audit_for_study("1.2.3.0") == []
        assert db.list_reports() == []
    finally:
        db.close()


def test_reader_sees_a_commit_made_after_it_opened(tmp_path: Path) -> None:
    """The reader must not pin a stale snapshot for the process lifetime."""
    db = _filled(tmp_path / "spool.db", n=1)
    try:
        assert db.count_studies() == 1
        db.insert_study("1.2.3.9", state="RECEIVED")
        # Autocommit readers take a fresh snapshot per statement, so the row
        # written after the reader connection was first opened is visible.
        assert db.count_studies() == 2
        assert db.list_studies()[0]["study_uid"] == "1.2.3.9"  # newest first
    finally:
        db.close()


def test_reader_does_not_see_a_rolled_back_write(tmp_path: Path) -> None:
    db = _filled(tmp_path / "spool.db", n=1)
    try:
        with pytest.raises(RuntimeError), db.transaction() as conn:
            conn.execute(
                "INSERT INTO studies (study_uid, state) VALUES (?, 'RECEIVED')",
                ("1.2.3.9",),
            )
            raise RuntimeError("boom")
        assert db.count_studies() == 1
    finally:
        db.close()


def test_routed_reads_do_not_take_the_write_lock(tmp_path: Path) -> None:
    """A list query runs on the reader, so it must not touch ``_conn``.

    The write connection is what every receive and every route claim waits on;
    if a list query held it, the optimisation would be a no-op. A query_only
    reader also cannot write at all, which is the property that makes routing
    safe rather than merely fast.
    """
    db = _filled(tmp_path / "spool.db", n=1)
    try:
        reader = db._read_connection()
        assert reader is not db._conn
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("CREATE TABLE _should_not_exist (x INTEGER)")
        assert db._conn.execute(
            "SELECT name FROM sqlite_master WHERE name = '_should_not_exist'"
        ).fetchone() is None
    finally:
        db.close()


# ══════════════════════════════════════════════════════════════════════════
# The point of the change: a read must not block a writer
# ══════════════════════════════════════════════════════════════════════════


def test_a_long_read_does_not_block_a_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow list query must not stall a C-STORE receive.

    The page-1 join over a full spool is exactly the query an operator's
    browser triggers while a modality is sending. Before P1-19 it ran under
    the process-wide RLock, so the receive path queued behind the browser for
    the whole query. The sleep stands in for that query's real cost; it goes
    inside ``_read`` so a revert to ``with self._lock: self._conn.execute(...)``
    makes this test fail instead of passing vacuously.
    """
    db = _filled(tmp_path / "spool.db", n=1)
    try:
        real_read = Database._read

        def slow_read(self: Database, sql: str, params: object = ()) -> list[sqlite3.Row]:
            if "FROM studies" in sql:
                time.sleep(0.4)
            return real_read(self, sql, params)

        monkeypatch.setattr(Database, "_read", slow_read)

        started = threading.Event()
        read_error: list[BaseException] = []

        def run_a_slow_list_query() -> None:
            try:
                started.set()
                db.list_studies()
            except BaseException as exc:  # noqa: BLE001 - reported on the main thread
                read_error.append(exc)

        browser = threading.Thread(target=run_a_slow_list_query, name="p1-19-slow-read")
        browser.start()
        assert started.wait(5.0)

        t0 = time.monotonic()
        db.insert_study("1.2.3.9", state="RECEIVED")  # the receive path
        elapsed = time.monotonic() - t0

        browser.join(10.0)
        assert not read_error, read_error
        # The 0.4 s query is still running; a blocked write takes >= that long.
        assert elapsed < 0.35, f"receive was blocked by a list query for {elapsed:.3f}s"
    finally:
        db.close()


def test_reads_from_two_threads_use_two_connections(tmp_path: Path) -> None:
    """The reader is per-thread: no thread shares another's sqlite3 handle."""
    db = _filled(tmp_path / "spool.db", n=1)
    try:
        seen: dict[int, object] = {}
        barrier = threading.Barrier(2)

        def look() -> None:
            barrier.wait()
            seen[threading.get_ident()] = id(db._read_connection())

        threads = [threading.Thread(target=look) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5.0)
        assert len(set(seen.values())) == 2, "threads shared one connection"
    finally:
        db.close()


# ══════════════════════════════════════════════════════════════════════════
# The carve-outs and the degradation path
# ══════════════════════════════════════════════════════════════════════════


def test_in_memory_databases_stay_on_the_write_connection() -> None:
    """A second connection to ``:memory:`` sees an empty database, not this one."""
    db = mem_database()
    try:
        db.insert_study("1.2.3.4", state="RECEIVED")
        assert db._read_connection() is db._conn
        assert len(db.list_studies()) == 1
    finally:
        db.close()


def test_read_falls_back_when_mode_ro_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A WAL reader needs the ``-shm`` sidecar; if it cannot be opened, the
    routed queries still answer correctly on the write connection."""
    db = open_database(tmp_path / "spool.db")
    try:
        db.insert_study("1.2.3.4", state="RECEIVED")

        def _fail(*args: object, **kwargs: object) -> sqlite3.Connection:
            raise sqlite3.OperationalError("unable to open database file")

        # Go through the real _open_read_connection so its catch-and-degrade
        # path is what's exercised — patching the method itself would skip it.
        monkeypatch.setattr("mercure_gateway.spool.db.sqlite3.connect", _fail)
        # The failure is sticky: one warning, then the write connection forever.
        assert db._read_connection() is db._conn
        monkeypatch.undo()
        assert db._read_connection() is db._conn  # still degraded, no retry
        assert len(db.list_studies()) == 1
        assert db.count_states() == {"RECEIVED": 1}
    finally:
        db.close()


def test_close_shuts_the_readers_down(tmp_path: Path) -> None:
    db = _filled(tmp_path / "spool.db", n=1)
    reader = db._read_connection()
    db.close()
    with pytest.raises(sqlite3.ProgrammingError):
        reader.execute("SELECT 1").fetchall()


@pytest.mark.slow
def test_reads_do_not_starve_a_sustained_writer(tmp_path: Path) -> None:
    """Under offered load, concurrent reads must not stall the receive path.

    The single-connection design's failure mode is exactly this: as read load
    rises, write throughput falls, because both queue on one handle. A reader
    on a separate connection does not.
    """
    db = open_database(tmp_path / "spool.db")
    try:
        stop = threading.Event()
        read_errors: list[BaseException] = []

        def hammer_reads() -> None:
            try:
                while not stop.is_set():
                    db.count_studies()
            except BaseException as exc:  # noqa: BLE001 - reported on the main thread
                read_errors.append(exc)

        readers = [threading.Thread(target=hammer_reads, name="p1-19-hammer") for _ in range(4)]
        for t in readers:
            t.start()
        try:
            t0 = time.monotonic()
            n = 100
            for i in range(n):
                db.insert_study(f"1.2.3.{i}", state="RECEIVED")
            elapsed = time.monotonic() - t0
        finally:
            stop.set()
            for t in readers:
                t.join(5.0)

        assert not read_errors, read_errors
        # ~3 ms/write is the unloaded number on this box; the gate is generous
        # because temp dirs differ, but a serialized read queue shows up as a
        # many-fold slowdown, not a fractional one.
        assert elapsed < n * 0.05, f"{n} writes took {elapsed:.2f}s under read load"
        assert db.count_studies() == n
    finally:
        db.close()
