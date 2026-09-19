# Changelog

All notable changes to mercure Gateway. Versions follow the tags published to
the release feed; each entry links the review finding it resolves (findings
live in `.full-review/05-final-report.md`).

## Unreleased — GA candidate

Everything below lands with `v1.1.0`. It is the output of the comprehensive
review pass: 12 P0 (ship-blocker) and 22 P1 findings, resolved in dependency
order. Operators on rc1–rc3 should read the migration notes for the two
behaviour changes marked **breaking-ish** (config strictness, secret
encryption) — neither bricks a deployed appliance, and both have a documented
recovery.

### Security

- **Config schema is now enforced (P0-4).** Unknown keys are rejected. An
  on-disk file carrying them is *self-healed* on boot: the offending keys are
  pruned, logged, and the original is preserved as
  `<config>.unknown-keys.bak` — so an upgrade cannot brick an appliance, and
  no upgrade is required. `PUT /api/config` and `POST /api/config/import`
  remain strict and name the offending path in the 400.
- **Admin authentication (P0-3, P0-8, P1-17).** Password hashes are PBKDF2
  (stdlib, 200k iterations) — the bcrypt branch is gone, so a hash can never
  become unverifiable because a transitive dependency went missing. A
  `mercure-gateway --set-web-password` CLI rescue path exists for a locked-out
  operator. Setting a non-loopback bind with authentication disabled is now a
  hard startup failure (was a warning); `MERCURE_GATEWAY_ALLOW_INSECURE_BIND=1`
  downgrades it for dev rigs and TLS-terminating proxies.
- **Secrets are encrypted at rest by default (P1-1).** At first boot with no
  master password resolvable, one is generated and persisted via the OS
  keyring (fallback: a 0600 file beside the config). Cleartext secrets are now
  an explicit `MERCURE_GATEWAY_ALLOW_PLAINTEXT_SECRETS=1` opt-in for dev.
- **Redacted secrets can no longer be cross-wired (P1-2).** Renaming a
  destination while a `***` sentinel is in the payload used to risk restoring
  *another* destination's stored secret; the positional fallback is gone, and
  duplicate destination names are now rejected. The session secret is
  per-process, so a config save no longer invalidates live sessions.
- **SFTP no longer tries ambient credentials (P1-21).** `look_for_keys=False,
  allow_agent=False` — the operator's personal SSH key was tried *before* the
  configured password, and a success was audit-recorded as if the configured
  credential had worked.
- **Session cookie `Secure` (P1-16)** — set only under TLS, so plain-HTTP
  localhost and the loopback default keep working.

### Correctness

- **Report retrieval (US-06) actually works (P0-6).** The C-FIND/C-MOVE
  transports were complete but never wired into the retriever; every poll hit
  a `RuntimeError` that was swallowed to a warning. They are wired now, and
  the E2E spec that would have caught it exists. (P0-7: the documentation that
  claimed this worked is corrected.)
- **SCU presentation-context budget (P0-5).** Degraded studies (unreadable
  files, unknown SOP classes) used to request all 111 storage classes × 4
  syntaxes = 444 contexts, and pynetdicom raised `ValueError` at the 129th —
  crashing the route for exactly the studies the fallback existed to rescue.
  Contexts are now bounded, and a failure returns `DeliveryResult(ok=False)`
  so the study retries instead of permanently failing.
- **One routing-rule engine (P0-9).** The preview path and the production path
  used two implementations with incompatible semantics. Production now
  delegates to the same engine the preview uses; a syntactically broken rule
  fails *open* (route to all targets, log loudly) rather than stranding
  studies in RECEIVED. `tests/test_routing_rules.py` passes unchanged as the
  backward-compatibility proof.
- **Transport timeouts (P1-12).** DIMSE associations and SFTP connects now
  carry timeouts like every other transport; three hung destinations can no
  longer halt all delivery.
- **Purge loops are bounded (P0-11).** Iteration cap + inter-iteration sleep,
  with a `mercure_gateway_purge_budget_hits_total` metric for when the cap is
  hit.

### Observability

- **Hub delivery is observable (P0-10).** `mercure_gateway_hub_streaming` was a
  thread-liveness flag set once at boot — a bookkeeper down since startup
  reported healthy. Added: `hub_outbox_depth`, `hub_delivering`,
  `hub_delivered_total`, `hub_delivery_failures_total`,
  `hub_events_evicted_total`. **Alert on depth, not on streaming** — the
  worker thread is alive while retrying, so `streaming` stays 1 by design.
- **Audit anchor verification runs on a timer (P0-10).** `verify_anchor_signatures`
  had zero production callers; `/api/audit/verify` replays internal chain
  hashes, which an attacker who rewrites the database recomputes. Only the
  hub-held Ed25519 signatures detect a whole-chain rewrite, so they are now
  verified every 5 minutes when `audit.hub_reporting.anchor_public_key` is
  set, surfacing as `mercure_gateway_audit_anchor_ok` and an
  `AUDIT_ANCHOR_FAILED` audit event. Deliberately **not** on the scrape path.

### Provenance & release

- **Sidecar provenance (P0-2).** The frozen backend snapshot is gone from the
  tree; `scripts/package_backend.py` emits a `PROVENANCE.json` and fails the
  packaging build on version drift. A locally-built installer can no longer
  boot an unauthenticated admin panel from a stale snapshot.
- **Release artifacts (P1-13, P1-15).** Every release now carries a CycloneDX
  1.5 SBOM for the frozen Python sidecar, `SHA256SUMS` of every artifact
  (printed into the release body — the out-of-band integrity channel), and a
  non-destructive re-dispatch: `--clobber` is gone, so a re-run can never
  overwrite an artifact *and* its `.sig` together.
- **Rollback (P1-14).** The dead `Updater.rollback()` is deleted (zero
  production callers; the updater is forward-only). The real procedure —
  reinstall the previous signed installer, spool data and config survive — is
  documented in `docs/dev/release-runbook.md` §6.
- **The API reports the product version (P1-8)** — it said `0.1.0` (the
  scaffold's), and the version-mirror test didn't cover it.

### Performance

- **One commit per received instance (P1-18)** instead of two — the provenance
  row and the study upsert used to be separate `BEGIN IMMEDIATE` +
  `synchronous=FULL` transactions with the instance's fsync barrier between
  them; ~26% of the receive critical section, removed with no durability loss
  (the study row still cannot say RECEIVED before the file is fsynced). New-
  series detection moves *inside* the transaction, which closes a real race
  the old out-of-transaction read had: two associations storing a series's
  first instances could each see "no series yet" and double-count
  `num_series`. Provenance becomes transactional rather than best-effort — a
  metadata failure now rolls both writes back and rejects the instance
  (`0xC120`) so the modality retries, instead of leaving a study row with no
  provenance; the orphaned file is reconciled as before.
- **Reads off the write connection (P1-19).** Pure-SELECT list and aggregate
  queries (`list_studies`, `count_states`, `list_audit_events`, `list_reports`,
  `spool_num_bytes`, the hub pending-event readers) now run on a per-thread
  read-only connection, so the web UI and the report poller no longer take the
  receiver's write lock. In WAL mode a reader sees the last committed snapshot
  without acquiring it. The carve-outs stay on the write connection on
  purpose: `get_routes` and the other reads that feed a write decision in the
  same transaction, and `iter_audit_events` (one shared cursor). `:memory:`
  test databases are unaffected. If a read-only WAL connection cannot be
  opened on some configuration (it needs the `-shm` sidecar), the routing
  degrades to the write connection with one warning instead of raising — the
  queries are correct on either connection, the split is an optimisation.

### CI

- Frontend gates in CI: `tsc -b`, `eslint`, `vitest run` (P1-5) — vitest's 68
  tests including the a11y scan previously had no CI signal at all.
- Subprocess coverage captured (`COVERAGE_PROCESS_START` + parallel mode), so
  the composition root is actually measured by the 80% gate (P1-6).
- `just gen-api` runs on a clean checkout, and an `api-schema-drift` CI job
  fails on an un-regenerated `api-schema.ts` (P1-9) — the drift that let the
  SPA's `fetchConfig` type itself into `Record<string, unknown>`.
- The throughput gate keeps the fast synthetic check as the CI default (P1-7)
  and gains a real mode (`just perf-gates-real`, `--real`) that delivers the
  same batch through the **real `DICOMHandler`** to a live C-STORE SCP,
  reading real `.dcm` files off a temp spool and counting instances that
  arrived on the wire. The synthetic handler does no I/O and its number is
  ~300× the real one — the fake measured a while loop, not throughput. The
  two are labelled distinctly in the output so the synthetic figure is never
  quoted as a delivery measurement; `--real` runs on the integration path
  (the `integration` marker, previously declared but unused, is now wired
  and deselected in CI) because a socket-bound measurement flaps on a loaded
  shared runner.

### Frontend

- **CSP `frame-src 'self' data:` is now pinned by a test (P1-11).** The
  directive was present but only header *presence* was asserted anywhere, so
  a directive removed or relaxed by accident would have passed silently —
  and report PDFs render in a `data:` iframe that `object-src 'none'` gives
  no fallback to. `test_csp_content` now pins each load-bearing directive as
  a full directive, including the deliberate `style-src 'unsafe-inline'`
  relaxation React needs.
- **Cancellable fetches and non-stacking pollers (P1-20).** `apiFetch` /
  `getJson` / `postJson` accept an `AbortSignal`, and a new `usePoll` hook
  replaces the bare `setInterval` pollers in the logs and pipeline views:
  each call aborts its predecessor, the timer re-arms only after settlement
  (polls cannot overlap or land out of order), a hidden tab stops polling
  and refetches on return, and unmount aborts and clears. `isAbortError()`
  keeps a deliberately cancelled request from being reported as "Log refresh
  failed".

### Documentation

- `CHANGELOG.md` (this file) and `docs/guides/migration-guide.md` — neither
  existed; an operator could not tell what a release changed.
- `docs/dev/release-runbook.md` gained §5 (non-destructive re-dispatch) and
  §6 (the real rollback procedure).
- The remaining stale auth documents are corrected (P1-22): the config
  examples in the PRD and the refinement spec no longer say the password hash
  is bcrypt set via the setup wizard (it is `pbkdf2$...` via
  `--set-web-password`, and the wizard posts plaintext to
  `/api/web-ui/password`, never a hash), and
  `docs/guides/secrets-and-env-overrides.md` no longer recommends `htpasswd`
  for a hash the scheme does not use. The sprint logs still record
  bcrypt/sha256 as the state of that sprint — that is what happened, and the
  CHANGELOG is the live record.

### Still open for GA (not yet landed)

None. Every P0 and P1 finding from the comprehensive review has landed on this
branch.

## v1.1.0-rc3 — 2026-09-17

Published tag. Signing key deliberately **not** rotated after the rc2 review
(recorded in `docs/qa/`): no key compromise, and rotation would strand every
installed rc1/rc2 updater. Rides GA.

## v1.1.0-rc2

Audit anchoring: the forwarder shares the anchored `AuditLog` instead of
constructing a fresh one. Passes the E1 go/no-go gate (3/3 burst on the
published artifact).

## v1.1.0-rc1 — 2026-09-14

First bundled release. NSIS on Windows, deb + AppImage on Linux, signed
updater manifests, and the first pass of the QA matrix (Windows 10/11, Ubuntu
LTS).
