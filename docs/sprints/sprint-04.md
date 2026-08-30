# Sprint 04 — Audit Hardening, Encrypted Config & Operator Console v0 (Weeks 7–8)

**Goal:** Complete US-07 (tamper-evident, encrypted, exportable audit) and give operators an early
read-only web dashboard so status/logs are visible before the S06 full web admin panel.

**Exit criteria:** Every event recorded with chained SHA-256 hash; log exportable (redacted);
encrypted config file with master password (US-07 AC); web console v0 shows queue/status/logs.

**PRD refs:** §5.5 audit config, §6, §7, §14 US-07, K5.
**Refinement refs:** product-refinement-spec.md §2.5 (encrypted config file), §7 (API surface).

**Per-sprint gate:** `AuditLog` (chained SHA-256, `verify()`, tamper detection) is implemented and
tested (7 tests) — this sprint extends it.

| ID | Task | PRD ref | RED → GREEN | DoD | Status |
|----|------|---------|-------------|-----|--------|
| S04-T1 | **Audit event coverage sweep:** ensure receiver/forwarder/report/error paths all emit events; enumerate the event vocabulary in one module | §5.4, K5 | `tests/test_audit_coverage.py` — every lifecycle transition produces ≥1 audit event (walk a study through all states) | K5 mechanism complete | ✅ |
| S04-T2 | **Redacted export:** one-click export (bundle: config with secrets redacted + audit log as structured JSON); PHI scoping honored per §6.4 | §7, §6.4 | `tests/test_audit_export.py` — export file valid JSON, no `api_key`/credential fields, respects PHI scope setting | US-07 export AC green | ✅ |
| S04-T3 | **Rotating text log:** file-based rotating log alongside the SQLite audit (operations log, distinct from audit chain) | §2.3, §7 | `tests/test_textlog.py` — rotation at size threshold, format stable, no PHI beyond scope | Dual logging per PRD §2.3 | ✅ |
| S04-T4 | **At-rest encryption decision + spool encryption:** evaluate SQLCipher (per `db.py` docstring note) vs SQLCipher-alternatives vs OS-level (BitLocker/doc); implement chosen option or document full-disk reliance + restrictive ACLs; ADR-0004 | §6.1, US-07 | `tests/test_db_encryption.py` — encrypted DB rejects plaintext open / key required (per ADR option) | US-07 "encryption by default" AC green or documented ADR | ✅ |
| S04-T5 | **Encrypted config file + master password:** credentials stored in `mercure-gateway.json` as AES-256-GCM encrypted blocks; master password derives key via PBKDF2 (100k iterations, SHA-256); on startup, if encrypted fields detected, prompt for password via web UI or CLI flag; credentials decrypted in memory only | §6.2, refinement §2.5 | `tests/test_credentials.py` — encrypted/decrypt round-trip; wrong password rejected; credentials never in plaintext config; fallback for no-encryption mode | §6.2 mechanism ready for S07 handlers; air-gapped credential storage works | ✅ |
| S04-T6 | **Operator console v0 (web-based, no TDD for UI, RED for service):** read-only web dashboard (queue/status/logs/errors) served by FastAPI core over localhost:8080; service layer gets RED tests (`tests/test_console_service.py`), UI smoke-tested manually | §2.2 Flow B, refinement §7 | service tests green; manual UI smoke on `docs/qa/` checklist | Operators can see queue + errors before S06 full web admin | ✅ |
| S04-T7 | **Audit retention:** configurable log retention (default 1 year) applies to text log + audit events, delivered-data-safe | §7 | `tests/test_audit_retention.py` — old events pruned per config; chain verification still passes post-prune | §7 retention AC green | ✅ |

**Evidence:**
- T1 — `src/mercure_gateway/audit/events.py` (event vocabulary), Spool now emits `STUDY_RECEIVED`/`STUDY_QUEUED`/`STUDY_SENT`/`STUDY_FAILED`, Forwarder/Spool use the shared constants. `tests/test_audit_coverage.py` (6 tests).
- T2 — `AuditLog.export_bundle()` + shared `src/mercure_gateway/redact.py` (used by web routes too). `tests/test_audit_export.py` (9 tests).
- T3 — `src/mercure_gateway/textlog.py` (rotating ops log, PHI scoping, `--tail` support). `tests/test_textlog.py` (7 tests).
- T4 — `Database.encrypt_key` guard + `db_meta` verifier table + `DatabaseEncryptionError`; `docs/adr/ADR-0004-at-rest-encryption.md` (OS full-disk reliance + key-required guard; SQLCipher deferred to v1.1). `tests/test_db_encryption.py` (5 tests).
- T5 — `src/mercure_gateway/credentials.py` (AES-256-GCM, PBKDF2 100k SHA-256) + `credentials.salt` in config. `tests/test_credentials.py` (9 tests).
- T6 — `src/mercure_gateway/web/console.py` (`ConsoleService.dashboard()`) + `GET /api/console/dashboard` route; wired into `main.py` (`operations.log` + startup audit prune). `tests/test_console_service.py` (8 tests) + `test_web_api.py` endpoint test.
- T7 — `AuditLog.prune()` re-anchors the chain after deleting old events; startup prune wired in `main.py`. `tests/test_audit_retention.py` (7 tests).

**Final gate:** 237 passed · `mypy` strict clean (28 source files) · `ruff check` clean.

**Notes:**
- T4 is the sprint's risk item — if SQLCipher adds packaging weight that threatens K6 (≤250 MB
  installer), the ADR must weigh it against the full-disk-encryption reliance option in §6.1.
  **Resolved:** ADR-0004 chooses OS-level full-disk encryption + restrictive ACLs for v1.0 with a
  key-required guard at the DB layer; SQLCipher is the documented v1.1 upgrade path (swap point
  marked in `db.py`/audit docstrings).
- **Refinement change (S04-T5):** The original spec used OS keyring (`keyring` lib) with fallback
  encrypted file. The refinement replaces this with a **portable encrypted config file** approach
  that works on air-gapped machines where OS keyring may not be available. Credentials are stored
  as AES-256-GCM encrypted blocks in `mercure-gateway.json`, decrypted with a master password on
  startup. OS keyring integration is deferred to v1.1 (Sprint 07). **Implemented** in
  `credentials.py` — S07 handlers will call `CredentialVault`.
- Console v0 is intentionally read-only and localhost-only; it is *not* the PRD §3.2 desktop shell
  (that's S06) and not remote admin (out of scope, §6.2). It uses FastAPI to serve a minimal
  web dashboard — the same stack that S06 builds into the full web admin panel.
