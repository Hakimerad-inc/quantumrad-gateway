# Phase 3: Testing & Documentation Review

**Source reports:** `phase3-3A-testing.md` (17 findings: 2 Critical / 5 High / 5 Medium / 5 Low — full suite
executed with `pytest --cov`, vitest executed, two traversal exploits built and demonstrated live, the
128-context ceiling reproduced against real pynetdicom, and the preserved US-01 benchmark re-run)
and `phase3-3B-documentation.md` (3 Critical / 7 High / 16 Medium / 12 Low / 4 structural gaps, plus
8 areas verified accurate).

**Orchestrator note:** the documentation reviewer's most consequential *new* finding (the wizard's discarded
Receiver step) and the CSP `frame-src` gap were independently re-verified against the source by the
orchestrator — both confirmed exactly as reported.

---

## Test Coverage Findings

### The suite is genuinely good — the gaps are specific and severe

**85.84% line coverage** (gate ≥80%), 754 tests pass / 5 skip, and the reviewer's own assessment is that
the suite tests *behaviour, not implementation*, with high assertion quality and healthy isolation
(`conftest.py` provides only fakes, no shared mutable state, no `chdir`/`os.environ` mutation).

But every prior-phase gap was **confirmed untested**, and four were independently reproduced by writing
new probes — meaning the tests are missing for behaviour that is **demonstrably broken right now**:

| Concurrency measured | Throughput | Per-assoc | Median latency |
|---|---|---|---|
| 1 association | 14.0 inst/s | 13.99 inst/s | 65.1 ms |
| 25 associations | 17.3 inst/s | **0.69 inst/s** | **1 338 ms** |

**25× the offered load yields 1.24× throughput** and a 20× per-association collapse — and no CI gate sees it.

### Critical

1. **T-1 — Path traversal in both report transports is untested AND exploitable.** The reviewer built live
   PoCs against both: a malicious PACS returning `study_uid = "1.2.3/../../../../../../tmp/pwned"` wrote
   files outside the reports sandbox for both the MOVE and DICOMweb transports. Existing report tests only
   ever pass well-formed UIDs; the receive path's `validate_uid` guard is tested, the reports tree has nothing.
2. **T-2 — Report transports are never injected and no test catches it.** `main.py:523-526` constructs
   `ReportRetriever` with no `finder`/`mover` (grep confirms no injection anywhere), so every poll hits
   `RuntimeError("report transports not configured")` and logs-and-skips. `test_reports_service.py` passes
   because it hand-injects fakes — the unit test is *correct about the object's contract and blind to
   whether the object is ever constructed correctly*. **US-06 is a no-op in every shipped build.**

### High

3. **T-3 — No test that the bind invariant survives a config update** (H-1). The boot guard is well tested;
   the runtime downgrade is not asserted at all.
4. **T-4 — `_restore_redacted_secrets` rename+reorder is untested** (H-3). The existing rename test renames
   destination 0 only; the cross-wiring case requires rename *and* reorder simultaneously.
5. **T-5 — bcrypt undeclared and the fail-closed branch uncovered** (H-4). Coverage confirms `auth.py:72-74`
   — the bcrypt `checkpw` path **and** its `ImportError → return False` — are never executed.
6. **T-6 — The 128-presentation-context SCU ceiling is untested and raises at runtime.** The SCP side has
   `test_sop_class_budget`; the SCU side, where the 444-context failure lives, has none. Reproduced live
   against real pynetdicom. `ValueError` is not among the caught exceptions, so affected studies crash the
   route rather than retrying.
7. **T-7 — The audit chain is never verified in production; `prune()` rewrites it without an integrity
   precondition.** `verify_anchor_signatures` has no production caller (grep-confirmed: tests only). `prune()`
   drops the append-only triggers and recomputes every hash from genesis, so **a tampered chain is helpfully
   repaired into a self-consistent state, destroying the evidence** — the exact opposite of the control's
   purpose.

### Medium

**T-8 — US-01 receive throughput unmeasured and ungated** (the table above; the reviewer recommends gating
the *scaling ratio*, which is the assertion that catches lock regressions). **T-9 — the disk-full purge loop
spins forever on a failed delete** (verified: every `OSError` in the suite was grepped and the purge path is
never exercised with a failing delete — a live infinite loop in the disk-full *recovery* path). **T-10 —
`Spool.complete`/`fail` non-atomic**: the "STUDY_SENT emitted once" test is sequential and the parallel test
gives each study one target, so the race window is never opened. **T-11 — no vitest job in CI**: 65 frontend
tests including the new a11y scan never run; vitest transpiles via esbuild and skips type-checking, so a green
local run says nothing about type correctness either. **T-12 — the a11y scan under-exercises the riskiest
markup**: only one row state is scanned (correcting an earlier-phase concern — the FAILED stub *does* render
action buttons, but RECEIVED/SENDING/SENT/ERROR rows and the full ConfigView credentials forms are unscanned).

### Low

The `integration`/`slow` marker taxonomy is declared but essentially unused (2 uses of `slow`, **zero** of
`integration`, while 338 tests are integration-flavoured by content — so no fast unit-only CI path exists);
E2E is thin for the surface area and **the one spec that would have caught T-2 does not exist**; poll-loop
tests are timing-bounded and the 120 s global timeout would let T-9's infinite loop hang rather than fail.

**Two findings emerged from this review's own probes rather than prior phases:**
- **T-16** — `db.close()` with pending auto-enqueue timers raises `sqlite3.ProgrammingError: Cannot operate
  on a closed database` (`spool/__init__.py:476`); `Spool.stop()` cancels timers but nothing calls it on this
  path.
- **T-17** — subprocess coverage is not captured (`main()` tests use `subprocess.Popen` with no
  `COVERAGE_PROCESS_START`), so `main.py` is undercounted at 58.8% while being executed every time a
  subprocess test boots — **the coverage signal is dead exactly where the integration risk lives**.

### Coverage pattern

The weakest modules map precisely onto the Critical/High findings: `reports/move.py` 57.3% (C-2 lives here,
`retrieve()`→`_save()` unexecuted), `main.py` 58.8% (composition root), `forwarder/handlers/sftp.py` 65.0%,
`reports/render.py` 71.7%, `reports/__init__.py` 76.1%, `forwarder/handlers/dicom.py` 77.5% (the SCU bug),
`web/auth.py` 81.6% (the bcrypt branch). **The receive path is well tested; the report-retrieval tree and the
SCU send path are not — exactly where the Critical/High findings sit.**

---

## Documentation Findings

### Critical

1. **C1 — Report retrieval is documented as working and cannot work.** `reports/__init__.py:9-10` claims
   "the production wiring in `main.py` supplies the real `ReportFinder` and `ReportRetrieve` implementations."
   It does not. The claim propagates through ADR-0005, `user-guide.md:64-68`, sprint-05/sprint-08 (both
   marked ✅), and `docs/qa/security-review-package.md:17` (which lists it as an in-place capability for an
   external security review). One level down, `transport.py:10-14`'s documented composition one-liner
   (`transport_for_query_source(...)`) appears only in its own docstring and tests — and would raise
   `TypeError` for `type: "dicom"` because `_register_defaults()` never registers a factory. `hl7_fhir.py:33`
   repeats the claim for a transport whose `find`/`retrieve` raise `NotImplementedError`.
2. **C2 — The documented way to enable web-UI auth does not exist.** The claim appears in **six** places:
   the config field label, `main.py`'s boot refusal message, admin-guide, site-deployment, the secrets guide,
   and both product specs. But the wizard has no auth step, there is no CLI, no endpoint, no UI field, and
   **nothing anywhere creates a password hash** (grep for writers returns readers only). The only real path
   is the out-of-band `htpasswd` workaround, which the secrets guide presents as an *alternative* to a
   wizard step that does not exist. Also: `auth_enabled=true` with an empty hash locks the panel with no
   in-product remedy, and the fail-closed bcrypt path has **no documented recovery procedure**.
3. **C3 — rc1 code inside an rc3-labelled installer, with no provenance.** Verified by the orchestrator:
   the bundled snapshot declares `__version__ = "1.1.0-rc1"` and its `main.py` has only `_warn_insecure`,
   while the source's `_enforce_bind_security` — the control `web/auth.py:6-7`, ADR-0007 and the
   site-deployment verify step all rest on — does not exist in what ships. **Important scope correction the
   reviewer recorded:** `src-tauri/binaries/` is gitignored (0 tracked files) and published CI releases
   re-freeze the sidecar from source, so published rc2/rc3 artifacts are *not* rc1. The exposure is (i) any
   local `cargo tauri build` bundling the stale snapshot into an installer that *reports* 1.1.0-rc3, and
   (ii) the absence of any guard that would catch a stale snapshot at tag time.

### High

4. **H1 — The wizard's Receiver step is silently discarded on save, then the UI claims success**
   *(new finding, orchestrator-verified)*. `SetupWizard.tsx:78` writes `data.receiver` (`ae_title`, `port`)
   into `config.general`, but `GeneralConfig` has only `appliance_name`/`locale`/`log_level` and no
   `extra=` override, so Pydantic's default `extra="ignore"` **drops both keys silently**. They belong under
   `config.receiver`. Then `:131-141` tells the operator "You can now receive and forward DICOM studies" —
   false for the receiver settings, which were validated server-side and then thrown away. `aet_source` is
   also hardcoded to `"GATEWAY"` (`:87`) instead of the AE title just typed.
5. **H2 — CSP omits `frame-src`, so PDF reports likely render blank** *(new finding, orchestrator-verified)*.
   The CSP falls back to `default-src 'self'` while `ReportsView.tsx:111` renders the only PDF path as a
   `data:application/pdf;base64` iframe — `data:` is not `'self'`, so the load is refused; `object-src 'none'`
   blocks any fallback. `admin-guide.md:83-86` presents the header set as complete and
   `tests/test_web_security.py:42` only checks the header exists, never its directives.
6. **H3 — OpenAPI advertises version 0.1.0 while the product ships 1.1.0-rc3.** `web/__init__.py:163`
   hardcodes it; the version-sync guard does not read the FastAPI string, so the generated client carried
   into the SPA is wrong too.
7. **H4 — The middleware-ordering comment is factually inverted**, and the inversion defeats a documented
   CSRF fallback: CORS is actually outermost, so a cross-origin browser request from a *different loopback
   port* is answered by CORS's fixed allow-list before the permissive loopback fallback the code comments
   rely on is reached. (Note: the security reviewer verified the CSRF origin check **itself** works for
   disallowed remote origins — this is a comment-correctness and loopback-fallback issue, not a bypass of
   the primary control.)
8. **H5 — ADR-0007's refusal boundary is boot-only and defeatable at runtime; the ADR does not say so.**
9. **H6 — Two documented developer workflows cannot run as written.** `just gen-api` reads a file its first
   line never produces (`export_openapi.py` writes to stdout, not `mercure-gateway.openapi.json`), and
   `just e2e` runs Playwright from `web/` where the root-relative config does not resolve. **The checked-in
   `api-schema.ts` therefore has no working regeneration procedure**, so schema drift is undetectable.
10. **H7 — The PRD and product brief claim SQLCipher AES-256 at rest; ADR-0004 formally declined it**, and
    both `db.py:20` and `main.py:462-465` state plainly that SQLCipher is a documented stub. The canonical
    product documents still assert a database encryption control that was evaluated and rejected.

### Medium

ADR-0005 stale against its own sprint board (M1); **ADR-0002 describes Method 1 while the shipped shell is
Method 2** — bundled sidecar, webview loads the bundled SPA from the Tauri app origin — and credits Tauri
with notifications/auto-start it does not provide, while the shell performs none of Method 2's promised
lifecycle management (no retry, no health check, no re-spawn) (M2); the documented Windows build command
fails for the exact reason CI documents and works around (`targets: "all"` emits MSI, which rejects semver
pre-release identifiers) (M2b); `PRODUCT_BRIEF.md` is stale on nearly every axis — version 0.1.0-dev, branch
`docs/sprint-plan`, a repo-structure map listing nonexistent packages, ADR-0007 absent (M3); the release
runbook is an rc1 document with a known-broken anonymous `curl` (M4); **the tray state machine is documented
as four states and live as three, `tray.py`'s only caller is tests, and the shipping Rust `derive_state` has
no test at all — the tested code is not the shipped code** (M5); Rust comments attribute the updater relaunch
to a plugin that is never invoked from Rust (it is the SPA) (M5b); the Tauri dev path collides with the
developer's own backend on port 8080 (M5c); **`usb-quickstart.md` documents an LED mapping with blink
semantics and an "amber" state that `led.py` does not have — every row of the table is wrong** (six solid
colours only) (M6); the "how many version sources" number disagrees across five documents (M7); "CSRF
tokens" is documented but the implementation is an origin check with no token (M8); `wizard.py`'s docstring
describes a state machine and `/api/wizard` route that do not exist (it is a stateless per-step validator)
(M9); the desktop auto-update banner is documented as a deferrable config-gated slice but ships and runs
unconditionally (M10); no `web/README.md` despite four load-bearing traps, plus a stray unreferenced
`web/mercure-gateway.json` (M11); the forwarder docstring names a `retry_max` knob that does not exist and
misdescribes the requeue timing (M12); the spool schema docstring lists four of seven tables, omitting the
load-bearing `hub_outbox` (M13).

### Low

An operator-facing error message pointing at a nonexistent path (`web/static/`); `main.py`'s docstring
construction/shutdown-order list is inaccurate; an eslint ignore for a path that cannot exist from `web/`;
`api.ts` claiming the hand-written interfaces are schema-derived "where a response model exists" while
`ServiceStatus` is hand-written despite a generated equivalent existing; two documents disagreeing on the
K6 size gate; a CSS comment naming a component that does not exist; **test fixtures using
`general: { ae_title: 'GATEWAY' }` — a document `GET /api/config` can never return**; the README being a
developer-only front door with no operator entry point; tray PNGs listed as bundle icons but consumed via
`include_bytes!`; the "Minimal" capability description overstating a permission set; the ADR-0006 pubkey
placeholder; a dead `dot="accent"` token.

### Structural gaps (missing, not wrong)

- **G1 — No API contract for most endpoints.** The reviewer's parse of `routes.py` gives **34 of 39
  operations with no `response_model`** (correcting the prior phase's "28 of 38" — the stricter count is
  the accurate one). No request/response examples anywhere, error contracts undocumented, and no API
  versioning or ADR on HTTP versioning.
- **G2 — No changelog, no migration guide, no breaking-change record.** Breaking changes are discoverable
  only via commit history or QA records; the `config_version`-must-be-a-string boot-crashing trap for fleet
  templates is documented only in a deployment footnote.
- **G3 — No release provenance for the Python dependency set.** Signed-installer provenance is excellent
  (Ed25519 `.sig` sidecars, custody record, `verify_release_sig.py`); the frozen Python sidecar has none —
  no SBOM, no pip-audit output attached to release assets, no record of which commit produced it.
- **G4 — No operator-facing documentation for enabling web authentication at all** (following from C2).

### Verified accurate (do not re-litigate)

`admin-guide.md` config defaults; ADR-0001, ADR-0003, ADR-0004 (the amendment declining SQLCipher is the
model for the amendments ADR-0002/0005 now need); ADR-0007's test claim; `user-guide.md`'s wizard steps
(the one document that does *not* over-claim an auth step); the `docs/qa/` evidence records, which are
honest and self-critical in the way mature QA records should be; `docs/dev/setup.md`'s two load-bearing
traps; `backup-restore.md` and `systemd/README.md`; and `security-review-package.md` §3, which states known
gaps with unusual directness.
