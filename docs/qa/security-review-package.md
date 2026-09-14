# External Security Review — Booking Package (PRD §11)

**Prepared:** 2026-09-14 · **Target:** independent security review before GA
**Product:** mercure/QuantumRAD DICOM gateway (`1.1.0-rc1` → `v1.1.0`)
**Owner for booking:** product (this doc is the scope + evidence package the
reviewer receives; findings triage feeds the GA gate, rc-checklist E2).

## 1. What is being reviewed

A store-and-forward DICOM gateway handling PHI in clinical environments:

- **Python backend** (~52 modules, `src/mercure_gateway/`): pynetdicom SCP
  receiver, SQLite+filesystem spool with fsync-before-ack, concurrent
  forwarder (8 destination types incl. SFTP/S3/DICOMweb/XNAT), chained-SHA256
  audit log with external + hub-signed head anchors, encrypted config
  (master-password vault), FastAPI admin API + report retrieval
  (DICOM SR/PDF, DICOMweb, experimental HL7/FHIR), Tauri-updater client
  (Ed25519), USB-dongle variant, Windows service mode.
- **Tauri v2 desktop shell** (Rust) + **React SPA** admin panel on loopback
  by default; optional operator-supplied TLS.
- **Distribution:** signed Windows installer + Linux deb/AppImage, auto-update
  via GitHub releases (ADR-0006).

**Code base for the review:** tag at review start (see §6 logistics).
Repo: `mercure-imaging/mercure-gateway` (private).

## 2. Declared security posture (read this first)

Trust boundaries: (a) modalities → receiver (AE-title allow-list, store-before-
ack), (b) gateway → destinations (per-destination credentials, TLS options),
(c) operator → web panel (loopback default; refusal of unauthenticated
non-loopback bind — ADR-0007; optional auth + TLS), (d) gateway → hub
(Token-scheme API key; audit events PHI-scoped), (e) updater → GitHub
(Ed25519 signature enforcement, fail-closed).

Controls already in place, with their tests (all runnable: `uv run pytest`):

| Control | Evidence |
|---------|----------|
| At-rest encryption: master-password config vault (ADR-0004), DB verifier HMAC, keyring-backed credentials with fallback | `tests/test_config_encryption.py`, `tests/test_db_encryption.py`, `tests/test_credentials.py`, `tests/test_keyring_credentials.py` |
| Tamper-evident audit: chained hashes, append-only triggers, external head anchors, hub-signed anchors (Ed25519) | `tests/test_audit.py` (11), `tests/test_audit_anchor_signing.py`, `tests/test_audit_coverage.py`, `scripts/verify_audit_anchors.py` |
| Web panel: CSP/XFO/nosniff headers, HSTS only over TLS, CSRF origin check, session cookies (HttpOnly/Lax), auth gate, refusal of non-loopback unauthenticated bind | `tests/test_web_security.py` (14) — ADR-0007 |
| DICOM access control: AE-title allow-list with loud startup warning | receiver tests in `tests/test_receiver*.py` |
| Transport security: DICOM-TLS w/ peer verification default, SFTP known_hosts (unset ⇒ reject, review H4), S3/DICOMweb HTTPS | `tests/test_security_gates.py` (9), handler tests |
| PHI minimization (§6.4): default-minimal audit scope, redacted config export, redacted diagnostics, PHI-stripped text log | `tests/test_audit_export.py`, `tests/test_diagnostics.py`, export/PHI tests in `tests/test_web_api.py`, `tests/test_config_import_export.py` |
| No-secrets-in-config sweep, dependency audits | `tests/test_security_gates.py` (9), CI `dependency-audit` job (pip-audit + npm audit + rustsec) |
| Crash/power-loss safety (no silent PHI loss) | `tests/chaos/` (8): kill-mid-transfer, disk-full, crash recovery |
| Updater trust: signature enforcement, tamper rejection | `tests/test_updater.py`, runbook §3 rehearsal |

Design docs: `mercure-gateway-PRD.md` §6 (esp. §6.3–6.4), `docs/adr/`
(0001–0007), `docs/guides/secrets-and-env-overrides.md`,
`docs/dev/hub-anchor-api.md`, `docs/dev/release-runbook.md` §0 (key custody).

## 3. Known gaps / explicit non-goals (get these findings out of the way)

Stated honestly so reviewer time goes to discovery, not rediscovery:

1. **SQLite encryption:** config vault is encrypted; the spool **database** is
   protected by an HMAC verifier + OS file permissions, not SQLCipher
   (documented follow-up, `main.py` review-H3 comment). DICOM files on disk are
   plaintext per deployment norm — disk-level encryption is a site requirement
   (admin guide; BitLocker/LUKS).
2. **Web panel auth:** single shared password (no per-operator accounts, no
   MFA) — appropriate for the loopback single-operator desktop model; network
   serving expects site-side controls (reverse proxy/VPN) — want reviewer
   validation of that assumption.
3. **No built-in log forwarder** (deliberate v1 egress decision; host plumbing
   documented, admin guide §Support).
4. **TLS certs are operator-supplied** — no ACME/self-signed minting
   (rationale in ADR-0007 §Rejected).
5. **SFTP known_hosts seeding is manual** (ssh-keyscan recipe in guides).
6. **Report transports:** HL7/FHIR path is marked experimental (§2.3 v1.1) —
   flag anything; scope says DICOM SR/PDF is the supported MVP.
7. **Firmware/LED/USB boot chain** (Sprint 10) outside review scope; the USB
   variant runs the same gateway binary.

## 4. Environment & data

- No live PHI in the repo. Reviewer gets: source, `test-rig/` (Orthanc + the
  bookkeeper stub — `docker compose up` in `test-rig/`), demo generators
  (`demo/fake_modality.py`) to drive synthetic studies end-to-end.
- Ephemeral test credentials only; hub endpoints mocked. Nothing to de-provision.

## 5. What we want from the review (scope asks)

1. PHI-handling correctness across the chain (§6.4 posture above) — including
   the K5 audit-chain trust assumptions and anchor custody.
2. Web panel exposure paths (ADR-0007 boundary: is the refusal+tunnel+TLS
   ladder defensible for shared-machine deployments?).
3. Update-channel trust (key custody, signature enforcement, downgrade).
4. Credential storage & the master-password model (custody SPOF in
   release-runbook §0.1).
5. Anything in the DICOM/SFTP/S3/XNAT surface the tests above miss.

Deliverable expected: written report, severity-rated, with a re-test pass for
high/critical fixes. Findings triage loop: accept-and-document vs rc-fix
(commit to `main`, re-verify gates) before the `v1.1.0` GA tag (rc-checklist
E2 prerequisite).

## 6. Logistics

- **Contact:** product owner (gateway) · hub team looped in only if reviewer
  targets the `/anchor` contract (`docs/dev/hub-anchor-api.md`).
- **Suggested window:** 2–3 weeks; start after the GitHub remote + first CI
  green exist (Phase A of the production plan) so reviewers can run CI.
- **Artifacts to send at booking:** this document, PRD + refinement + ADRs,
  `docs/qa/rc1-checklist.md`, `docs/guides/` set, repo access (read, pinned
  tag), test-rig instructions.
- **Budget/authority:** product owner signs the engagement; engineering
  (1) supports reviewer questions async.

## 7. Reviewer quick-start

```bash
git clone <pinned-tag> && cd dicom-gateway
uv sync --extra dev
uv run pytest -q                 # 710+ tests incl. all security suites, ~3 min
uv run mypy src && uv run ruff check .
docker compose -f test-rig/docker-compose.yml up -d   # Orthanc + hub stub
uv run mercure-gateway --web     # panel on http://127.0.0.1:8080
python demo/fake_modality.py --help                  # drive synthetic studies
```

Reading order: PRD §6 → ADR-0004/0006/0007 → `web/auth.py` +
`web/__init__.py` middleware → `audit/` → `spool/db.py` WAL/fsync paths →
`forwarder/handlers/*` per-destination trust handling.
