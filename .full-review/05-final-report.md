# Comprehensive Code Review — Final Report

**Target:** `dicom-gateway` — whole repository (Python/FastAPI backend, React/TypeScript admin panel, Tauri/Rust shell)
**Base commit:** `aab94b5` (tag `v1.1.0-rc3`) plus uncommitted WIP in `web/`
**Reviewed:** ~11,700 LoC Python · ~6,900 LoC TS/TSX · Rust shell · CI/CD · documentation · test suite
**Phases executed:** Quality & Architecture → Security & Performance → Testing & Documentation → Best Practices & Standards
**Date:** 2026-09-18

---

## 1. Executive Summary

This is a **well-engineered codebase with unusually mature operational practice and a small number of severe,
specific defects** — several of which are currently exploitable or silently broken in the shipped product.

The honest summary is a two-part story, and both parts are true:

**What is genuinely strong.** The dependency tree is remarkably current (zero yanked distributions, all
packages ahead of declared floors). Python 3.12 and Pydantic v2 are used idiomatically throughout —
discriminated unions, `StrEnum`, `model_validator`, no legacy `.dict()`/`.json()`. Handlers are correctly
plain `def` for blocking work; the security middleware is a hand-written pure-ASGI implementation, the
currently-recommended form. TypeScript is genuinely strict with three documented escape hatches in the whole
SPA. The *deploy* half of the pipeline is better than most projects of this size: Ed25519-signed installers
with a documented key-custody record and correct minisign prehash handling, a supervised systemd unit with a
boot health probe, a PHI-free-by-construction metrics endpoint, and a backup/restore drill with four explicit
pass criteria. The 754-test suite tests *behaviour, not implementation*, with high assertion quality and clean
isolation. Where the code claims something, the reviewer's instinct should be to check — and in eight areas
the documentation was verified accurate, including a security-review package that states known gaps with
unusual directness.

**What is wrong.** Every non-functional requirement that matters most on a medical appliance is either
unmeasured, ungated, or silently broken:

- **Two CVSS 9.8 path-traversal sinks** in the report-retrieval tree, both demonstrated live with working PoCs.
- **The shipped installer is not the audited source** — the frozen sidecar on disk is `1.1.0-rc1` and contains
  only `_warn_insecure`, not the `_enforce_bind_security` `SystemExit` that `web/auth.py` says its security
  model rests on. Published CI releases re-freeze and are unaffected; the exposure is local builds and the
  total absence of any gate that would notice.
- **Authentication can be disabled at runtime with one authenticated `PUT` and cannot be enabled by any
  supported path.** No wizard step, no CLI, no endpoint, and no code anywhere creates a password hash.
- **US-06 (report retrieval) is a no-op in every shipped build** — `main.py` constructs `ReportRetriever` with
  no `finder`/`mover`, so the poll loop raises and skips. The unit tests pass because they hand-inject fakes;
  they are correct about the object's contract and blind to whether the object is ever constructed.
- **US-01 (receive throughput) is asserted as a config default and never verified under load.** Measured on
  real ext4: 25 associations yields **0.69 instances/s each at 1338 ms median** — 25× the offered load buys
  1.24× throughput. The perf gate measures a handler that does no I/O.
- **The 128-presentation-context fallback makes some studies permanently undeliverable** — it requests 444
  contexts, pynetdicom hard-caps at 128 and raises, and the blanket `except` converts that into a retried-5×
  permanent failure. It fires for exactly the degraded studies it was written to rescue.
- **The audit chain — the device's regulatory reason for existing — can silently stop reaching the hub with
  no metric and no alert**, and `prune()` helpfully repairs a tampered chain into a self-consistent state,
  destroying the evidence.

**The one-line diagnosis:** the codebase invests heavily in correctness *of individual components* and in
*deploy* machinery, while the *verification* layer that would catch composition errors — the reports wiring,
the SCU context budget, real throughput, the frontend tests, the composition-root coverage — is either absent
or measuring a fake. Eight of the twelve top findings are "the unit is right and the wiring is wrong."

---

## 2. Findings by Priority

Deduplicated across all four phases. Raw per-reviewer totals were 238 findings; overlaps consolidated to the
set below. Severity reflects *impact on the shipped product*, not reviewer count.

### P0 — Fix before GA (ship-blockers)

| # | Finding | Location | Effort |
|---|---|---|---|
| P0-1 | **Path traversal in both report transports (CVSS 9.8).** `_save` composes output paths from remote-controlled UIDs. Two live PoCs wrote files outside the reports sandbox. | `reports/dicomweb.py:177-184`, `reports/move.py:170-177` | 1 day |
| P0-2 | **Shipped installer is not the audited source (CVSS 8.1).** Frozen sidecar is rc1 with `_warn_insecure` only; an operator binding `0.0.0.0` gets an unauthenticated PHI API. Add a provenance manifest + frozen-version assertion. | `src-tauri/binaries/`, `scripts/package_backend.py:111-120` | 1 day |
| P0-3 | **Auth disableable at runtime (CVSS 8.7) and not enableable at all (CVSS 7.5).** One `PUT` flips `auth_enabled:false`, then an unauthenticated client gets `/api/studies`, `/api/config`, and `POST /api/system/stop`. No path exists to create a hash. | `web/routes.py:790`, `web/auth.py:94` | 3 days |
| P0-4 | **Pydantic `extra="ignore"` silently discards unknown config keys.** Root cause of the wizard's discarded Receiver step — validated, saved, 200, thrown away. Fix removes the whole class. | `config/__init__.py:69`, `SetupWizard.tsx:79` | 1 day |
| P0-5 | **128-presentation-context ceiling makes studies permanently undeliverable.** `ValueError` is not among the caught exceptions, so affected studies crash the route rather than retrying. | `forwarder/handlers/dicom.py:105-112` | 2 days |
| P0-6 | **US-06 is dead: report transports never injected.** Every poll hits `RuntimeError("report transports not configured")` and skips. | `main.py:523-526` | 0.5 day |
| P0-7 | **Report retrieval documented as working and cannot work.** The claim propagates through ADR-0005, the user guide, two sprints (both ✅), and a package prepared for an external security review. | `reports/__init__.py:9-10` et al. | 1 day |
| P0-8 | **bcrypt undeclared; the default admin password is single-round SHA-256.** A plain `uv pip install .` silently falls through to a hash brute-forceable at billions/sec. The documented bcrypt "fallback" fails closed into a permanent lockout. | `pyproject.toml`, `web/auth.py:72-74` | 0.5 day |
| P0-9 | **Two divergent routing-rule engines.** Two implementations of the same decision with different semantics; rule evaluation is not unified. | Phase 1 C2 | 3 days |
| P0-10 | **Hub audit delivery unobservable; `prune()` destroys tamper evidence.** A hub rejecting everything forever looks identical to healthy; the append-only triggers are dropped and hashes recomputed from genesis. | `hub_events.py:108-111,159-162`, `audit/` `prune()` | 2 days |
| P0-11 | **Disk-full purge loop spins at 100% CPU forever when a delete fails.** The capacity watcher becomes the load, silently. | `disk.py:115-123`, `spool/__init__.py:905-917` | 0.5 day |
| P0-12 | **1.3 MB test-only `axe.js` shipped and served publicly.** 6.1× the entire app payload, referenced by nothing, present twice. | `web/public/axe.js` | 10 min |

### P1 — Fix before/with GA (high impact, moderate urgency)

| # | Finding | Location | Effort |
|---|---|---|---|
| P1-1 | **Config secrets written cleartext to disk by default** — SFTP passwords, SSH private keys, S3 keys, hub API key, admin hash, on a USB appliance. `credentials.encrypted` only fires when nothing ever sets the master password. | `config/__init__.py:599-618` | 2 days |
| P1-2 | **Redaction sentinel cross-wires or destroys credentials** — live PoC persisted the lab server's password into the PACS entry; `"***"` saved literally destroys a credential. | `web/routes.py:705-766` | 1 day |
| P1-3 | **SSRF via server-controlled pagination** — follows whatever `Link: rel="next"` a remote PACS supplies; live PoC fetched `127.0.0.1:9`. | `reports/dicomweb.py:157-161` | 0.5 day |
| P1-4 | **Measured receive-path ceiling: 0.69 inst/s per association at 25 assoc.** Fully serialized critical section; one fsync + two FULL WAL commits per instance inside one process-wide `RLock`. | `spool/__init__.py` | 5 days |
| P1-5 | **No vitest and no eslint in CI** — 65 tests including the a11y scan have no signal; lint drift between local and CI is unbounded. | `.github/workflows/ci.yml` | 0.5 day |
| P1-6 | **Coverage gate miscounts the composition root** — `main.py` at 58.8% while the ≥80% gate passes on the aggregate; subprocess coverage never captured. | `pyproject.toml:90-101` | 1 day |
| P1-7 | **Throughput gate measures a handler that does no I/O** against a 5 items/s floor. | `scripts/check_perf_gates.py:27-41` | 2 days |
| P1-8 | **OpenAPI advertises 0.1.0 while the product ships 1.1.0-rc3**; the sync script's sixth and most visible mirror is uncovered. | `web/__init__.py:172` | 0.5 day |
| P1-9 | **Codegen covers 5 of 21 exported types; 34 of 39 operations lack `response_model`.** A generated `ReceiverConfig` type would have caught P0-4 at compile time. | `web/routes.py`, `types/api.ts` | 2 days |
| P1-10 | **The wizard's Receiver step is silently discarded, then the UI claims success** (subsumed by P0-4; the SPA wrong-section merge is a separate 10-minute fix). | `SetupWizard.tsx:78, 131-141` | 10 min |
| P1-11 | **CSP omits `frame-src`** — the only PDF rendering path is a `data:` iframe against `default-src 'self'`, so reports likely render blank; `object-src 'none'` blocks fallback. | `web/__init__.py:68-77` | 0.5 day |
| P1-12 | **No transport timeouts** — `ae.associate()` and SFTP `connect()` pass none; three hung destinations halt all delivery. | `forwarder/handlers/` | 1 day |
| P1-13 | **Release assets mutable after publish** — `--clobber` rewrites artifact and `.sig` together; nothing immutable pins what was signed. | `release.yml:187-198` | 1 day |
| P1-14 | **No application rollback path** and `Updater.rollback()` is unreachable — a bad signed update takes store-forward down with no rehearsed revert. | `update.py:302` | 1 day |
| P1-15 | **No changelog, no migration guide, no SBOM for the Python sidecar.** | `release.yml` | 1 day |
| P1-16 | **Session cookie lacks `Secure`** — replayable on the first hop under the TLS-terminating proxy ADR-0007 explicitly permits. | `web/auth.py:121-127` | 10 min |
| P1-17 | **No rate limiting on `/api/login`** (30 consecutive wrong passwords, no throttle, no audit event); **M-4/M-5** (`auth_password_hash` settable as arbitrary pre-computed string; `""` + `auth_enabled` = permanent lockout). | `web/auth.py` | 1 day |
| P1-18 | **Two FULL-fsync commits per instance** — merging measures 10.1 ms vs 16.9 ms, ~26% of the receive critical section, **with zero durability loss**. | `spool/__init__.py` | 2 days |
| P1-19 | **One shared connection + one `RLock`** serializes receiver, forwarder, web, and report poller — the mechanism behind the 1.46× scaling factor; violates US-10's isolation invariant. | `db.py` | 5 days |
| P1-20 | **Frontend pollers stack requests** — bare `setInterval`, no `AbortController`, no `document.hidden` gating; hand-rolled fetching has a request-ordering race in 6 files. | `QueueView.tsx:32` et al. | 2 days |
| P1-21 | **SFTP uses ambient credentials** — no `look_for_keys=False`/`allow_agent=False`, so the operator's personal key is tried *before* the configured password, with a successful-delivery audit event. | `forwarder/handlers/sftp.py` | 10 min |
| P1-22 | **Docs/labels claim bcrypt; the default install has none.** Six places document an auth-enabling path that does not exist. | `web/wizard.py:20`, `admin-guide.md:58` | 1 day |

### P2 — Should fix (Medium)

Config-schema enforcement end-to-end (the five-step contract fix); `verify_anchor_signatures` scheduled in
production rather than only in drills; `/api/pipeline` materializing the whole studies table per 2 s poll;
unbounded uncached `GROUP BY` per poll; duplicate study-level C-MOVEs (a typical study transfers ~1,202
instances instead of ~2); reports retrieve buffering whole studies in RAM; `_restore_redacted_secrets`
rename+reorder untested; the boot-time bind invariant not asserted to survive a config update; the
`Spool.complete`/`fail` non-atomic 3-transaction race; middleware-order comment inverted with no security
headers on preflight; DI bypassed (`request.app.state` untyped under mypy --strict); health monitor lifecycle
outside a `lifespan`; `async` handler doing blocking file write on the event loop (USB target); monitoring
config shipped only as prose; no staged rollout/update channel; the `slow`/`integration` markers declared and
unused so no fast CI path exists; test rig on `:latest` and never run in CI; duplicate `png`/`reqwest` majors
in the Rust tree; ADR-0005 stale against its own sprint board; ADR-0002 describing Method 1 while Method 2
ships; the LED quickstart table where **every row is wrong** (six solid colours, no blink semantics, no
"amber"); the tray state machine documented as four states and live as three, with the shipped Rust
`derive_state` untested and the tested `tray.py` uncalled; `usb-quickstart.md`, `wizard.py` docstring, the
forwarder's nonexistent `retry_max` knob, and the spool schema docstring listing four of seven tables; no
API contract for most endpoints; the PRD claiming SQLCipher AES-256 that ADR-0004 formally declined; two
documented `just` recipes that cannot run, one verified by execution; no `web/README.md` despite four
load-bearing traps; ~150–200 lines of duplicated fetch boilerplate.

### P3 — Nice to have (Low)

Loopback-port CSRF allow-list; sessions never rotated/revocable; absolute `file_path` echoed in PHI
responses; OpenAPI schema exposed unauthenticated; `AuthContext` unmemoized; `numpy` declared but never
imported with an EOL-looking floor; Rust `log` crate unused with no logger; Rust edition 2021/MSRV 1.77.2;
`eslint` deprecated in lockfile; queue-state string literals where a `StrEnum` exists; CI runs full packaging
on doc-only pushes; stale `docs/sprint-plan` branch trigger; fixture configs using a document `GET /config`
can never return; the README having no operator entry point; a dead `dot="accent"` token; several
operator-facing error messages pointing at nonexistent paths.

---

## 3. Findings by Category

| Category | P0 | P1 | P2 | P3 | Notes |
|---|---|---|---|---|---|
| **Security** | 7 | 12 | 6 | 3 | 2 CVSS 9.8 traversal PoCs, 8.7 auth-downgrade, 8.1 stale bundle, 7.5 no-enable-path + weak hash |
| **Correctness / data integrity** | 3 | 1 | 9 | 3 | Rule-engine divergence, silent config drops, credential cross-wiring, audit `prune()` |
| **Performance / capacity** | 1 | 4 | 8 | 0 | Measured ceiling 0.69 inst/s @ 25 assoc; two-fsync merge is free 26% |
| **Test coverage / CI** | 1 | 3 | 7 | 2 | Composition-root blind spot; vitest never gated; fake perf handler |
| **Documentation** | 1 | 2 | 13 | 5 | US-06 documented as working; auth enablement documented as existing |
| **Framework / idioms** | 0 | 3 | 5 | 4 | Pydantic, DI, lifespan, codegen, toolchain versions |
| **DevOps / release** | 1 | 2 | 6 | 3 | Provenance, rollback, SBOM, channel management |
| **Frontend** | 0 | 1 | 4 | 3 | 1.3 MB axe.js, fetch race, no splitting, poller stacking |

**Totals:** 12 P0 · 22 P1 · 58 P2 · 23 P3 = **115 deduplicated findings** (from 238 raw across eight sub-reviews).

---

## 4. Recommended Action Plan

Ordered by *risk removed per unit of effort*, not by severity alone. Items 1–8 are the GA gate.

### Sprint 1 — Close the bleeding (1.5 weeks)

1. **Delete `axe.js` (both copies)** — 10 minutes, removes a 6.1× payload bloat and an unauthenticated 1.3 MB
   file. Highest payoff-per-minute in the entire review.
2. **Fix `main.py:523-526`** — inject the real `finder`/`mover`. Half a day, revives US-06, and is the
   precondition for meaningfully fixing P0-1 and P0-7.
3. **Add `validate_uid` to both report `_save` paths** — one day, closes both CVSS 9.8 sinks. The guard
   already exists in `spool/__init__.py:62`; reuse it.
4. **Add `bcrypt` to `pyproject.toml`** and document the fail-closed recovery path. Half a day.
5. **Bound the disk-full purge loop** — iteration cap, sleep, and a `purge_stalled` metric. Half a day.
6. **Add `extra="forbid"` to the config models + fix the wizard's wrong-section merge** — one day, removes a
   whole bug class, and makes the wizard's Receiver step actually persist.
7. **Emit the sidecar provenance manifest and the frozen-version assertion in `package_backend.py`** — one
   day, and add the version check to the existing Windows smoke test. That single assertion would have caught
   the rc1 drift. Delete or refresh the stale on-disk snapshot.
8. **Add the missing test for the reports wiring** — the one E2E spec whose absence let P0-6 ship.

### Sprint 2 — Make the controls real (2 weeks)

9. **Re-check auth on the config-write path** (`routes.py:790` × `auth.py:94`) so a runtime downgrade
   re-triggers `_enforce_bind_security`, plus an audit event for the auth-mode change.
10. **Build the auth-enablement path** — a wizard step or CLI that actually creates a hash. Without this, the
    practical operator flow on a PHI device is "leave auth off."
11. **Fix the SCU presentation-context budget** — cap the requested contexts and add `ValueError` to the
    caught exceptions so a degraded study retries instead of permanently failing.
12. **Merge the two fsync commits** — measured 10.1 ms vs 16.9 ms, no durability loss.
13. **Add the CI gates that are missing:** a `web` job (`tsc -b`, `eslint`, `vitest`), subprocess coverage
    config so `main.py` is counted, and a real-handler perf mode behind `slow`/`integration` markers.
14. **Add `frame-src` to the CSP** and extend the header test to assert directives, not just presence.
15. **Fix the two broken `just` recipes** and add a schema-drift CI guard — this is the entry point for the
    codegen contract.

### Sprint 3 — Observability and provenance (1.5 weeks)

16. **Ship the hub metrics** (`hub_outbox_depth`, `hub_delivery_failures_total`, `hub_events_evicted_total`,
    a distinct `hub_delivering` gauge) and **schedule the anchor verification** so tamper evidence is not
    latent. The reviewer ranks this first on payoff-per-effort.
17. **sha256 + build provenance on release assets**, non-destructive re-dispatch, and an SBOM attached to the
    release.
18. **Ship the alert rules as files** instead of prose every site hand-transcribes.
19. **Document the rollback procedure** and either expose or delete `Updater.rollback()`.
20. **Add `response_model` to the config endpoints**, regenerate `api-schema.ts`, and delete the hand-written
    TS interfaces as the generator replaces them. This is the five-step contract fix that turns four findings
    into one enforced schema.

### Sprint 4 — Capacity and debt (GA + 1)

21. **The read-only second connection** to break the process-wide `RLock` — the mechanism behind the 1.46×
    scaling factor. Largest single performance lever; also restores US-10's isolation invariant.
22. **Unify the two routing-rule engines.**
23. **Transport timeouts** for DIMSE and SFTP.
24. **Frontend fetching:** thread `signal` through `apiFetch` (or adopt TanStack Query and delete ~120 lines of
    boilerplate), then fix poller stacking and `document.hidden` gating.

### What to *not* do

- **Do not** chase the vite 5→8 / vitest 2→5 major bump before GA. It is dev-only, has no production blast
  radius, and the advisory data was inconsistent in this environment — re-verify it first.
- **Do not** adopt TanStack Query purely for the fetch race; `AbortController` solves that. Query is a
  boilerplate decision, not a correctness one.
- **Do not** invest in route-level code splitting for the Tauri origin — there is no network to wait on. It
  only matters for the headless HTTP deployment.
- **Do not** re-litigate the CSRF origin check. Phase 2 refuted the Phase 1 suspicion with a live test
  (`Origin: http://evil.example` → `403`); the defect is the comment, not the control.
- **Do not** treat the 85.84% coverage number as evidence the composition root is tested. It isn't.

---

## 5. Review Metadata

| | |
|---|---|
| **Scope** | Whole repository: backend, SPA, Tauri shell, CI/CD, docs, tests |
| **Phases** | 5 (4 executed with 2 parallel sub-reviews each, 1 consolidation) |
| **Sub-reviews** | 8 (`1A` quality, `1B` architecture, `2A` security, `2B` performance, `3A` testing, `3B` documentation, `4A` framework, `4B` CI/CD) |
| **Raw findings** | 238 across all sub-reviews |
| **Deduplicated** | 115 (12 P0 / 22 P1 / 58 P2 / 23 P3) |
| **Live PoCs executed** | 6 (both traversal sinks, the auth-downgrade chain, credential cross-wiring, SSRF pagination, 444-context failure) |
| **Benchmarks** | Real ext4 (`/dev/sda2`), not tmpfs; preserved in `.full-review/.perfbench/` |
| **Quality gates at review time** | ruff clean · mypy --strict clean (70 files) · `tsc -b` clean · eslint clean · 754 pytest pass / 5 skip · 65 vitest pass · 85.84% coverage |
| **Method** | Parallel local subagents per phase; each phase's output written to `.full-review/` before the next began; subagents seeded with prior-phase findings (file:line) and told which claims were already verified-clean |

**Verification caveats, stated plainly:**

1. The security subagent's safety classifier was unavailable during its run. The orchestrator independently
   re-verified every load-bearing security claim against the source: the rc1/`_warn_insecure` bundle diff,
   the config-swap/live-read pair, both unvalidated `_save` paths, the missing bcrypt, the wizard step list,
   and the unauthenticated static mount. All confirmed.
2. Two Phase 3 findings — the wizard's discarded Receiver step and the CSP `frame-src` gap — were also
   orchestrator-verified against source, both confirmed exactly as reported.
3. One Phase 1 claim was **refuted** by Phase 2 with a live test (the CSRF reachability suspicion). One
   Phase 1 assumption was **corrected** by Phase 3A (the a11y stub does render action buttons; the gap is
   the other row states). One Phase 3 claim was **corrected** by Phase 4B (CI does type-check the SPA).
4. The C-3 scope was **narrowed** by Phase 3B: `src-tauri/binaries/` is gitignored and published releases
   re-freeze from source, so published rc2/rc3 artifacts are not rc1. The exposure is local builds plus the
   missing staleness guard. This report carries the corrected version.
5. The `npm audit` advisory data was inconsistent across calls in this environment (one full report, then
   `total: 0`). The version-gap conclusion is robust; the specific severities are not.

**Refuted or verified-clean (do not re-litigate):** SQL parameterized throughout; the C-STORE traversal is
closed; the audit chain is append-only at the trigger layer; `phi_scope` applied at read time; the auto-updater
fail-closed in all three cases; no XSS vectors in the SPA; no secrets in the client or repo; keyring fails
closed; `redact_config` covers all secret fields; fsync ordering sound; no N+1 in the study endpoints; the hub
outbox is bounded with a durable table; the disk monitor is on its own thread; the retry sleep is interruptible;
the CSRF origin check works; signed-installer provenance is strong; the systemd unit and metrics endpoint are
well designed; the backup/restore drill is genuinely good; the dependency audit jobs run as blocking gates;
the version-sync guard drift-tests its own logic; and `docs/qa/` is honest and self-critical in the way mature
QA records should be.

---

## 6. Output Files

| File | Contents |
|---|---|
| `00-scope.md` | Scope, flags, five-phase plan |
| `phase1-1A-code-quality.md` | 38 findings with measured complexity table |
| `phase1-1B-architecture.md` | 43 findings with ADR conformance table |
| `01-quality-architecture.md` | Phase 1 consolidation |
| `phase2-2A-security.md` | 25 findings with CVSS scores and live PoCs |
| `phase2-2B-performance.md` | 23 findings with real ext4 benchmarks |
| `02-security-performance.md` | Phase 2 consolidation |
| `phase3-3A-testing.md` | 17 findings with coverage analysis |
| `phase3-3B-documentation.md` | 42 findings + 4 structural gaps |
| `03-testing-documentation.md` | Phase 3 consolidation |
| `phase4-4A-framework.md` | 26 findings, runtime-verified |
| `phase4-4B-cicd.md` | 24 findings, recipes executed |
| `04-best-practices.md` | Phase 4 consolidation |
| **`05-final-report.md`** | **This document** |
