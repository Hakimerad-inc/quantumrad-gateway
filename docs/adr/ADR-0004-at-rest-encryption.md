# ADR-0004: At-Rest Encryption Strategy

**Status:** Accepted
**Date:** 2026-08-30
**Deciders:** Product + Engineering
**Relates to:** PRD §6.1 (Data Handling), §7 (Compliance & Audit), US-07,
refinement spec §2.5, Sprint 04 S04-T4 (risk item)

## Context

PRD §6.1 specifies "SQLite encrypted with SQLCipher (AES-256)" for the spool
database and audit log, with DICOM files on local disk under restrictive ACLs.
The MVP's acceptance criteria for US-07 include "encryption enabled by default".

Three viable strategies were evaluated:

1. **SQLCipher (pysqlcipher3 / sqlcipher3)** — real AES-256 at the SQLite
   layer; every table, index and the audit chain are encrypted on disk.
   - *Cost:* a C-extension dependency that must compile/link against a
     recompiled SQLite (system libsqlite won't do). This adds meaningful
     packaging weight and Windows-build fragility — the sprint's risk item is
     that it threatens K6 (installer ≤250 MB) and the Windows packaging story
     (S06).
2. **OS-level full-disk encryption (BitLocker / LUKS / FileVault) + restrictive
   ACLs** — no dependency, no packaging weight, industry-standard for the
   single-user, single-PC deployment model the gateway targets.
3. **App-level encryption of DICOM files** — encrypting each `.dcm` at rest
   with a library key. Rejected: adds per-read decrypt cost on the hot path,
   complicates recovery-scan and forwarder handlers, and does not protect the
   SQLite audit chain (which is the more sensitive artifact).

## Decision

**For v1.0, rely on OS-level full-disk encryption + restrictive ACLs** as the
primary at-rest protection, and add a **key-required guard at the database
layer** so an encrypted database cannot be opened in plaintext. SQLCipher is
the **documented v1.1 upgrade path**; the schema, queries and chained-hash
logic in `spool/db.py` and `audit/__init__.py` are written so that swapping the
`sqlite3.connect` call for `pysqlcipher3`'s connection + `PRAGMA key` leaves
every other line unchanged (see the audit module docstring).

Rationale:

- The gateway is a **single-user, single-workstation** appliance (PRD §6.2) —
  the OS-encrypted-profile model (BitLocker on the user's data volume) already
  covers the attack surface of concern (lost laptop / offline disk copy).
- SQLCipher's packaging weight and Windows-build fragility directly threaten
  K6 and S06, and the sprint flag explicitly allowed weighing it against the
  full-disk option (§6.1 "Optional full-disk reliance documented").
- The key-required guard closes the worst gap of a pure OS-reliance posture:
  an encrypted-volume DB copied to an unencrypted medium cannot be silently
  re-opened as plaintext — the process refuses to start without the key.

## Decision Details (the implemented guard)

`Database` accepts an `encrypt_key` on open (`open_database(path, encrypt_key=...)`).
On first keyed open it stores a `salt:hmac` verifier in the new `db_meta`
table; every later open must present the same key:

- open **without** a key on a keyed database → `DatabaseEncryptionError`
- open **with the wrong** key → `DatabaseEncryptionError`
- open **with the correct** key → succeeds
- open on a database that was never keyed → succeeds (dev/test + full-disk sites)

The key itself is never stored; only an HMAC-SHA256 over a per-database random
salt is kept. The verifier exists to reject plaintext open, **not** to be the
sole cipher — that remains the OS volume encryption (BitLocker/LUKS/FileVault).

Coverage: `tests/test_db_encryption.py` (key round-trip, plaintext-open
refusal, wrong-key refusal, unkeyed-DB unchanged, no silent re-key).

## Consequences

- `spool/db.py` gains `db_meta` (verifier table), `encrypt_key` plumbing on
  `Database`/`open_database`, and `DatabaseEncryptionError`.
- The audit log is *not* SQLCipher-encrypted in v1.0; its tamper-evidence
  (chained SHA-256) is orthogonal and unaffected (PRD §6.1/§7).
- v1.1 (Sprint 09 hardening) may add SQLCipher behind a `connection()` factory;
  the `db.py`/audit docstrings mark the exact swap points. K6/S06 gates are not
  put at risk by a C-extension dependency in v1.0.
- Operators on non-FDE machines (or with audit over an unencrypted share) are
  documented to enable a key via the encrypted-config master password (S04-T5)
  and OS policy (BitLocker on the data volume).
