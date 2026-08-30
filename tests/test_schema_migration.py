"""TDD (S02-T7, RED): schema migration pattern.

Behaviors:
1. A v1 database (no ``instance_meta`` table) migrates to the current version
   on ``open_database()`` — data preserved, version stamped
2. A v2 database migrates the same way
3. Current-version databases open without re-migrating (idempotent)
4. A database stamped with a FUTURE version is refused (downgrade guard)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mercure_gateway.config import DICOMDestination
from mercure_gateway.spool import Spool, StudyState
from mercure_gateway.spool.db import SCHEMA_VERSION, open_database


def make_v1_database(path: Path) -> None:
    """Create a database with the Sprint-01 v1 schema (no instance_meta)."""
    conn = sqlite3.connect(path)
    conn.executescript(
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
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            study_id     INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
            accession    TEXT,
            study_uid    TEXT NOT NULL,
            report_type  TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',
            file_path    TEXT,
            retrieved_at DATETIME
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
        CREATE INDEX IF NOT EXISTS idx_task_routing_status ON task_routing(status);
        CREATE INDEX IF NOT EXISTS idx_task_routing_study  ON task_routing(study_id);
        CREATE INDEX IF NOT EXISTS idx_reports_study       ON reports(study_id);
        CREATE INDEX IF NOT EXISTS idx_reports_status      ON reports(status);
        CREATE INDEX IF NOT EXISTS idx_audit_ts            ON audit_events(ts);
        """
    )
    conn.execute("PRAGMA user_version = 1")
    # Seed data that must survive the migration
    conn.execute(
        "INSERT INTO studies (study_uid, accession, modality, num_instances, state) "
        "VALUES ('1.2.3.4', 'A1', 'CT', 3, 'QUEUED')"
    )
    study_id = conn.execute("SELECT id FROM studies").fetchone()[0]
    conn.execute(
        "INSERT INTO task_routing (study_id, target_name, target_type) "
        f"VALUES ({study_id}, 'hub', 'dicom')"
    )
    conn.commit()
    conn.close()


def make_v2_database(path: Path) -> None:
    """Create a v2-shaped database (current tables, no instance_meta rows)."""
    make_v1_database(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()


class TestSchemaMigration:
    def test_v1_database_migrates_data_preserved(self, tmp_path: Path) -> None:
        db_path = tmp_path / "spool.db"
        make_v1_database(db_path)

        db = open_database(db_path)

        assert db.user_version == SCHEMA_VERSION
        row = db.get_study_by_uid("1.2.3.4")
        assert row is not None, "existing study lost in migration"
        assert row["accession"] == "A1"
        assert row["state"] == "QUEUED"
        routes = db.get_routes(row["id"])
        assert len(routes) == 1 and routes[0]["target_name"] == "hub"
        db.close()

    def test_v2_database_migrates(self, tmp_path: Path) -> None:
        db_path = tmp_path / "spool.db"
        make_v2_database(db_path)

        db = open_database(db_path)

        assert db.user_version == SCHEMA_VERSION
        assert db.get_study_by_uid("1.2.3.4") is not None
        db.close()

    def test_current_version_opens_without_rehash(self, tmp_path: Path) -> None:
        db_path = tmp_path / "spool.db"
        db = open_database(db_path)
        assert db.user_version == SCHEMA_VERSION
        db.close()

        # Reopen — must not fail or re-migrate
        db = open_database(db_path)
        assert db.user_version == SCHEMA_VERSION
        db.close()

    def test_future_version_refused(self, tmp_path: Path) -> None:
        db_path = tmp_path / "spool.db"
        db = open_database(db_path)
        db.close()

        conn = sqlite3.connect(db_path)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.commit()
        conn.close()

        with pytest.raises(RuntimeError, match="newer than supported"):
            open_database(db_path)

    def test_migrated_database_still_works(self, tmp_path: Path) -> None:
        """After migration the spool state machine works on the old data."""
        db_path = tmp_path / "spool.db"
        make_v1_database(db_path)

        spool = Spool(open_database(db_path))
        study = spool._db.get_study_by_uid("1.2.3.4")
        study_id = int(study["id"])

        tasks = spool.claim_next(limit=1)
        assert len(tasks) == 1
        assert spool.state(study_id) == StudyState.SENDING

        spool.complete(study_id, "hub")
        assert spool.state(study_id) == StudyState.SENT
        spool._db.close()

    def test_migration_creates_instance_meta_table(self, tmp_path: Path) -> None:
        """The v3 migration adds ``instance_meta`` (per-instance storage
        tracking: original transfer syntax provenance, sidecar presence)."""
        db_path = tmp_path / "spool.db"
        make_v1_database(db_path)

        db = open_database(db_path)
        tables = {
            r["name"]
            for r in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "instance_meta" in tables

        # Empty and usable
        rows = db.list_instance_meta("1.2.3.4")
        assert rows == []
        db.close()

    def test_disabled_target_enqueue_still_uses_v1_rows(self, tmp_path: Path) -> None:
        """Enqueue semantics unchanged post-migration (UNIQUE still enforced)."""
        db_path = tmp_path / "spool.db"
        make_v1_database(db_path)
        spool = Spool(open_database(db_path))
        study = spool._db.get_study_by_uid("1.2.3.4")

        target = DICOMDestination(
            name="hub2", host="h", port=104, aet_target="T"
        )
        spool.enqueue(int(study["id"]), [target])
        routes = spool._db.get_routes(int(study["id"]))
        assert {r["target_name"] for r in routes} == {"hub", "hub2"}
        spool._db.close()
