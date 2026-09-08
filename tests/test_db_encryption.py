"""S04-T4 (RED): at-rest encryption for the spool database (US-07, §6.1).

Per ADR-0004 the gateway keeps a key-required guard at the database layer:
when a database has been opened with an encryption key, a later open without
the correct key is rejected (it refuses plaintext open).  The real at-rest
protection in v1.0 is OS-level full-disk encryption + restrictive ACLs
(BitLocker/LUKS/FileVault); SQLCipher is the documented v1.1 upgrade path.

Behaviors:
1. A database opened with a key persists data across re-opens with the key.
2. Re-opening an encrypted database WITHOUT a key is rejected (plaintext open refused).
3. Re-opening with the WRONG key is rejected.
4. A fresh (never-encrypted) database still opens without a key — the guard is
   opt-in so dev/test and full-disk-reliant sites are unaffected.
"""

from __future__ import annotations

import pytest

from mercure_gateway.spool.db import DatabaseEncryptionError, open_database


def test_correct_key_roundtrip(tmp_path) -> None:
    path = tmp_path / "spool.db"
    db = open_database(path, encrypt_key="correct-horse-battery")
    db.initialize()
    db.insert_study("1.2.3.4", state="RECEIVED")
    db.close()

    reopened = open_database(path, encrypt_key="correct-horse-battery")
    assert reopened.get_study_by_uid("1.2.3.4") is not None
    reopened.close()


def test_encrypted_db_rejects_plaintext_open(tmp_path) -> None:
    path = tmp_path / "spool.db"
    db = open_database(path, encrypt_key="correct-horse-battery")
    db.initialize()
    db.close()

    with pytest.raises(DatabaseEncryptionError):
        open_database(path)  # no key → refuse plaintext open


def test_wrong_key_rejected(tmp_path) -> None:
    path = tmp_path / "spool.db"
    db = open_database(path, encrypt_key="correct-horse-battery")
    db.initialize()
    db.close()

    with pytest.raises(DatabaseEncryptionError):
        open_database(path, encrypt_key="wrong-key")


def test_fresh_db_opens_without_key(tmp_path) -> None:
    path = tmp_path / "spool.db"
    db = open_database(path)
    db.insert_study("1.2.3.4", state="RECEIVED")
    db.close()

    reopened = open_database(path)
    assert reopened.get_study_by_uid("1.2.3.4") is not None
    reopened.close()


def test_rekeying_detects_key_change(tmp_path) -> None:
    """Switching to a different key is an explicit, separate operation — opening
    with a new key when one is already set is rejected (no silent re-key)."""
    path = tmp_path / "spool.db"
    db = open_database(path, encrypt_key="first-key")
    db.initialize()
    db.close()

    with pytest.raises(DatabaseEncryptionError):
        open_database(path, encrypt_key="second-key")
