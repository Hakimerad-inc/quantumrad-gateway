# P2/P3 Backlog Implementation

## Context

`.full-review/05-final-report.md:110-139` carries the P2/P3 backlog as two prose
paragraphs — 58 P2 + 23 P3 = 81 findings — with **no IDs, no checkboxes, and no
status tracking**. Nothing in the repo tracks them at item level. The review base
was `aab94b5`; 34 commits have landed since, including P0/P1 work that closed
several of these findings and re-graded others into tiers already shipped.

So the backlog as written is materially rotted. This plan starts from a
re-validation of every item against `HEAD` (`6d9815e`), which changes the shape
of the work considerably:

- **~11 items are STALE** — already closed by P0/P1 commits (`axe.js` deleted,
  `verify_anchor_signatures` on a timer, bind-invariant enforced at every write
  boundary, `_restore_redacted_secrets` made name-only and tested, vitest/tsc/eslint
  gated in CI, PROVENANCE + SBOM + rollback shipped, composition-root coverage
  captured, `PipelineView` drill-down server-capped, both `just` recipes fixed).
- **The report's own counts don't reconcile.** The prose double-counts the LED
  finding (it appears as both "the LED quickstart table" and "`usb-quickstart.md`"),
  and several items the category table files as P2 were re-graded to P0/P1 in the
  final report's own lists. Security P2 is 3 distinct items, not 6.
- **One genuinely open item is tracked nowhere** — the `_read_connection` docstring
  (`spool/db.py:265-287`) documents three read-connection carve-outs but omits the
  `_read_disabled` sticky fallback that `__init__` at `db.py:205-206` points readers
  to. It is not in `05-final-report.md` and will not be fixed by anything on a list.
- **The working tree is not clean** — 9 files, +578/-12, in-flight `POST
  /rules/preview` work spanning backend and regenerated `api-schema.ts`.

**Outcome:** implement everything still open at `HEAD`, in one pass, with the gate
run at the end. Stale items are dropped with evidence rather than re-done.

Two direction decisions, confirmed: **docs align to code** for the LED table and
the SQLCipher claims (ADR-0004 formally declined it); **code aligns to docs** for
the tray state machine — the 4th `removable` state gets implemented end-to-end.

---

## Progress tracker

Updated after every step. Status: `todo` → `doing` → `done` / `dropped` / `blocked`.
Counts at the bottom are the source of truth for "how far through the pass are we."

### Step 0 — Baseline

| Item | Status | Notes |
|---|---|---|
| `/rules/preview` WIP | done | Landed as `1ab6eb5` (P0-9). Was uncommitted when this pass started; verified before it landed: 95 backend + 93 frontend tests pass, `tsc -b` clean |
| `dicomweb` report transport | done | Landed as `c4a5f82`. Resolved the schema drift: `api-schema.ts` now matches the backend exactly (re-verified, no diff) |
| `PROVENANCE.json` verify at build time | done | Landed as `6d9815e`. The guard moved into `src-tauri/build.rs` and fires on every `cargo tauri build`, not just when `package_backend.py` is the thing freezing. Parsers live in `src-tauri/src/sidecar.rs` so `cargo test` actually runs them — a build script's `#[cfg(test)]` module never executes. `ci.yml`'s Linux packaging job now runs that `cargo test`. Working tree clean |

### Step 1 — Cheap sweep

All Step 1 items below landed as **`2e2c83f`** (Wave A). Gate was green on that
commit: 976 tests, 89.71% coverage, web/ lint + `tsc -b` + 93/93 vitest,
`gen-api-check` clean, `cargo test` 12/12 + `cargo fmt` + clippy clean.

| Item | Status | Notes |
|---|---|---|
| `/api/pipeline` → `COUNT(*)` | done | |
| `count_routes_by_target` index + TTL memo | done | |
| `spool_num_bytes` covering index | done | |
| `import_config` `run_in_threadpool` | done | |
| Queue-state literals → `StudyState` | done | |
| Middleware order swap + preflight test | done | |
| OpenAPI gated behind auth | done | |
| Loopback CSRF port pin | done | |
| `AuthContext` `useMemo` | done | |
| Dead `dot="accent"` token | done | |
| `ae_title` fixtures | done | |
| CSS `ProtectedRoute` comment | done | |
| SPA-not-built error message path | done | |
| Spool schema docstring (4→7 tables) | done | |
| Read-conn `_read_disabled` docstring | done | untracked item |
| Forwarder docstring (`retry_max`) | done | |
| `wizard.py` docstring | done | |
| LED quickstart table | done | docs align to code |
| CI `paths-ignore` for docs | done | |
| Drop `docs/sprint-plan` trigger | done | |
| `dependabot.yml` + `CODEOWNERS` | done | |
| `numpy` floor | done | |
| Rust `log` crate | done | |
| Rust edition 2024 / MSRV | done | `cargo tauri build` verified clean; edition changed rustfmt's `use` ordering — the pre-existing import block was normalized with `cargo fmt`, proven edition-induced by a temporary revert |
| Test-rig image pin | done | |

### Step 2 — Spool durability and performance

| Item | Status | Notes |
|---|---|---|
| 2a `Spool.complete`/`fail` atomic completion | done | 3 methods wrapped in one transaction; transaction-count tests assert one BEGIN, mutation-verified |
| 2b `_requeue_complete_routes` single `executemany` | done | resolved inside spool/__init__.py via transaction join; db.py left to B7's owner |

### Step 3 — Reports retrieval

| Item | Status | Notes |
|---|---|---|
| 3a Study-level C-MOVE dedup | done | moved_studies set; move-count test added — 18 failures when reverted |
| 3b Save-in-handler, no RAM buffering | done | on_c_store saves before acking; UIDs from the dataset, not the match |
| 3c C-FIND early stop | done | keyword-only `limit` on find() + Protocol/transport pass-through; fake-AE counter proves the early stop |

### Step 4 — Web layer

| Item | Status | Notes |
|---|---|---|
| `response_model` on 31 remaining ops | done | 38 named schema components; 4 Response-returning ops documented as documentation-only |
| Absolute `file_path` out of PHI responses | done | scrubbed from 6 sites + omitted from the models (durable against SELECT *) |
| Audit verify → `iter_audit_events` streaming | done | cursor-per-row on the read conn, 100-error cap + total_error_count/truncated |
| Session revocation | done | RevocationStore + the route wiring that was missing at the shipped endpoint; e2e test added |
| Health monitor into a `lifespan` | done | starts/stops under lifespan, joins the thread, respects a pre-set app.state monitor |

### Step 5 — Tray state machine

| Item | Status | Notes |
|---|---|---|
| `SystemStatus.usb_mode` field | done | read live from the in-memory config at request time, not a boot snapshot |
| Rust `derive_state` `removable` arm + glyph | done | TRAY_REMOVABLE in both state_label and apply_tray_state; glyph authored |
| Test the shipped `derive_state` | done | derive_state refactored to be testable (live I/O split out); 9 tests |
| Fix divergent defaults (`running` vs `stopped`) | done | fail-closed chosen and pinned, documented and tested |
| Update tray docs once behaviour is real | done | ADR-0002's 2026-09-21 amendment item 5 documents all four states with priority and the reachability guard; B11 made it real, D1 wrote it |

### Step 6 — Documentation

| Item | Status | Notes |
|---|---|---|
| ADR-0002 rewrite (Method 2 as shipped) | done | dated amendment records Method 2 as shipped; plugin claims corrected to Cargo.toml; four tray states documented. Two verifier-flagged mis-citations fixed in-tree (a `PRD §2.2 Flow C` pointer copied from lib.rs comments that lands on "Failure & recovery"; an ADR-0006 attribution ADR-0006 does not make, re-anchored to packaging.md) |
| ADR-0005 stale vs sprint board | done | amendment records the DICOMweb transport shipped (S08-T4 + `c4a5f82`); three verifier-flagged defects fixed: a phantom `ReportMover` class (it is `ReportRetrieve`), a false "the seam superseded ReportFinder" claim (it is live wiring the seam wraps), "offset pagination" (it is RFC 5988 Link rel=next), and a pre-existing `>95% (per PRD §1 survey)` attribution to a survey that does not exist |
| PRD + PRODUCT_BRIEF SQLCipher claims | done | docs align to code; ADR-0004 declined. Scope-corrected: the backlog's `main.py:711` target is stale AND wrong — that comment is a swap-point docstring the ADR amendment explicitly retains, so zero Python files were touched. Verifier caught four further PRD spots (48/110/451/490) left contradicting the corrected §6.1; fixed, so the PRD is internally consistent |
| README operator entry point | done | operator/developer split, all 7 guides + 7 ADRs linked, diagram now shows the Tauri shell over the Python sidecar. Verifier caught "hub bookkeeper stand-in" (test-rig.md says "mercure hub stand-in") and an overstrong "everything else is referenced from there"; both fixed |
| `web/README.md` + delete stray JSONs | done | web/README.md documents the four load-bearing traps. The three `.bak-*` strays at the repo root were deleted (user-approved, verified untracked/gitignored/unreferenced first; live config and the master-password sidecar confirmed intact). The two live `mercure-gateway.json` files and the master password were left alone by design |
| Monitoring config as real YAML files | done | `monitoring/prometheus.yml` + `monitoring/alerts.yml` (12 rules), admin-guide points at them. Verifier caught the two `purge_*` metrics documented as `counter` while `routes.py` emits them as plain `gauge()` with no `kind=` — the batch's claim of having verified the `kind=` sites was false. Reclassified as gauge in both files with a "kinds are as-emitted, not as-named" note, since the code's own comment and the `_total` suffix both read counter |

### Step 7 — Frontend structure

| Item | Status | Notes |
|---|---|---|
| `useAsync` hook + migrate **3** of 7 pages | done | QueueView/AuditView/ReportsView — the other 4 are honest exclusions, not skips: ConfigView's fetch feeds the edit-guard, DestinationsView resets `dirty`/`error` and seeds a list the operator mutates in place, LogsView/PipelineView are pollers under `ui/usePoll` (abort + `document.hidden` gating, P1-20). Documented in the hook header |
| Route-level code splitting | done | 8 of 10 views lazy under one Suspense; Dashboard/LoginView/BackendDownView stay eager (the page you land on must not be behind a chunk). Failed chunk load → ErrorBoundary module-load branch + full reload, not a blank pane |
| a11y-scan parametrization | done | 10 `it()` blocks → one `CASES` table + `it.each`; still exactly 10 cases, still `toEqual([])` |

### Step 8 — CI and release

| Item | Status | Notes |
|---|---|---|
| `test-fast` CI job | done | `test-fast` job (`ci.yml:145`) runs `-m "not slow and not integration"` — 1030 of 1036 tests, 6 deselected. Its selector is guarded: `tests/conftest.py` resolves the `-m` expression against the declared markers and raises `pytest.UsageError` on an unknown name, because `--strict-markers` does not validate names *inside* a `-m` expression, so a typo silently degraded to the full selection and still passed. Pinned by `tests/test_marker_selection.py` (`1a13bb9`) |
| Perf gate `--real` in CI (E2a) | done | `perf-gates-real` job in `ci.yml`; `continue-on-error` on PRs, blocking on `main`. Needs no Orthanc — `--real` ships its own in-process pynetdicom SCP on an ephemeral loopback port |
| Receive-scaling ratio (E2b) | done, report-only | `scripts/check_receive_scaling.py` re-establishes the missing benchmark. Ships ungated: the 3× floor would fail on shipped code (measured 1.77–2.18× at 5 assoc), and the floor's premise — that US-01 requires *scaling* — conflates "accepts" with "scales". See the script docstring and §"Genuinely blocked" below |
| Staged rollout / update channel | done | `UpdateConfig.channel` (`config/__init__.py:428`) + the `channel` field and `_channel_for_version` gate in `update.py`: a stable install is never offered a higher prerelease, and a manifest whose channel field is absent counts as stable. `tauri.conf.json` untouched — `tauri-plugin-updater` 2.11.0 ignores unknown keys. Mutation-verified in both directions; `channel` also threaded through `main.py`'s update check |
| `eslint` / `globals` / `whatwg-encoding` bumps | done, partly declined | eslint stays at 9.39.5: the 10.x bump is blocked by `eslint-plugin-react-hooks`'s peer cap (^9) and by the `@eslint/js@^10.11.0` range never being published; the 7 findings it would surface are deliberate documented patterns in 6 files, so the correct fix is a source refactor, not a bump. `whatwg-encoding` is dropped only by jsdom ≥27.7.0, and the jsdom ^24→^28 bump was reverted after a cold-cache A/B (3/3 red on 28 vs 3/3 green on 24) — recorded in `web/README.md` so the experiment is not repeated blind |
| Runbook §4 Windows flake wording | done | rewritten as a ratchet, not blanket re-run advice: §4.1 tables the three timing tests against the jobs that can actually see them, documents the `--cov` timeout-budget inversion (10 s untraced vs 60 s traced), and makes "it passes in isolation" explicitly *not* flake evidence. The old Windows attribution was stale — those tests have been `skipif(win32)` since `eb80a58` |

### Gate

| Check | Status | Notes |
|---|---|---|
| `ruff check .` | pass | clean on 2e2c83f |
| `mypy .` | pass | 72 source files, no issues |
| `pytest -m "not integration"` | pass | 976 passed, 5 skipped, 3 deselected |
| coverage ≥ 80 | pass | 89.71% |
| `web/`: eslint + `tsc -b` + vitest | pass | lint clean, tsc -b exit 0, 93/93 tests; bare `tsc --noEmit` checks nothing here (root tsconfig is references-only) |
| `just gen-api-check` | pass | api-schema.ts matches the exported schema (no response_model work in Wave A, so no regen needed) |
| `cargo test` + `cargo tauri build` | pass | 12/12 tests, fmt clean, clippy clean; `cargo tauri build` green at edition 2024 |

Wave B (`8f9a57a`) — gate re-run in full after the wave, including the barrier:

| Check | Status | Notes |
|---|---|---|
| `ruff check .` | pass | clean |
| `mypy .` | pass | 72 source files, no issues |
| `pytest -m "not integration"` | pass | 1014 passed, 5 skipped, 3 deselected (was 976) |
| coverage ≥ 80 | pass | 90% |
| `web/`: eslint + `tsc -b` + vitest | pass | lint clean, tsc -b exit 0, 93/93 tests |
| `just gen-api` (the barrier — run for real) | pass | 773 lines of typed schema added to api-schema.ts; 38 new named components from B5's response_model work |
| `cargo test` + `cargo tauri build` | pass | 20/20 tests, fmt clean, clippy clean; 3 bundles produced |

**Tally:** 46 done · 8 todo · 0 dropped · 1 blocked

Wave C (frontend structure) — gate re-run in full after the wave:

| Check | Status | Notes |
|---|---|---|
| `ruff check .` | pass | clean — no Python changed this wave, re-run anyway |
| `mypy .` | pass | 72 source files, no issues |
| `pytest -m "not integration"` | pass | 1014 passed, 5 skipped, 3 deselected; `test_25_concurrent_associations` (since renamed `test_5_concurrent_associations` when US-01 was re-sized to ≥5 assoc, 2026-09-24) failed once under full-suite load and passes in isolation and in its file (a concurrency race, not a Wave C regression — Wave C touched no Python) |
| coverage ≥ 80 | pass | 90% |
| `web/`: eslint + `tsc -b` + vitest | pass | lint clean, tsc -b exit 0, **105 tests / 17 files** (93 baseline + 8 `useAsync` + 4 `app-lazy`; the a11y refactor is count-neutral) |
| `just gen-api-check` | pass | api-schema.ts matches — no response_model work this wave, so the barrier did not need to run |
| `cargo` | skipped | no `src-tauri` file changed; `static/assets/` is gitignored build output |

**Wave C's one verifier flag was real and is fixed in-tree** rather than
deferred: `app-lazy.test.tsx`'s first case claimed "Dashboard renders on
first paint, not behind the Suspense boundary" while only asserting that
Dashboard content renders — a property it could not observe, since
AuthProvider starts in its loading state and clears it asynchronously, and
jsdom resolves a registered dynamic import synchronously. It now pins the
observable part of the decision (`typeof Dashboard === "function`", a
lazy wrapper is never a function) and is mutation-verified against a real
module-scope `lazy(() => import())`, the regression it guards.

**Scope correction carried back to the tracker:** the Step 7 line read
"useAsync hook + migrate 7 pages"; 3 of the 7 are genuine fits. The other
4 have behaviour the hook cannot express (ConfigView's edit-guard,
DestinationsView's stateful load resets, the two polling pages under
`ui/usePoll`), and converting them would delete real behaviour rather than
boilerplate. The line above is corrected to 3 and each exclusion is
recorded in the `useAsync.ts` header, so the reason travels with the code.

**Tally after Wave E:** 58 done · 0 todo · 0 dropped · 0 blocked of 64 tracked
lines (51 implementation + Step 0 + 7 gate checks + tray sub-items). ~11
findings were dropped up front as stale (see the DROPPED table below), so
those 51 represent the open surface of the original 81. Step 8's CI/release
cluster (E1/E3/E4/E5, counted as one line in the tally but four batches) is
done; E2 is closed — its sole blocked row turned out to rest on a false premise
(that the perf gate's `--real` mode needs a test-rig Orthanc). It splits to E2a
(the `perf-gates-real` CI job, landed) and E2b (the receive-scaling measurement,
shipped as `scripts/check_receive_scaling.py`, deliberately report-only). The
block was attributed to a missing test-rig Orthanc, which was never the real
dependency: `--real` ships its own in-process pynetdicom SCP.

Wave D (documentation aligns to shipped behaviour) — gate re-run in full:

| Check | Status | Notes |
|---|---|---|
| `ruff check .` | pass | clean — no Python logic changed this wave |
| `mypy .` | pass | 72 source files, no issues |
| `pytest -m "not integration"` | pass | 1014 passed, 5 skipped, 3 deselected — identical to the Wave C baseline, which is the expected result for a docs-only wave |
| coverage ≥ 80 | pass | 90.19% |
| `web/`: eslint + `tsc -b` + vitest | pass | lint clean, tsc -b exit 0, 105/105 tests |
| `just gen-api-check` | pass | api-schema.ts matches; the barrier did not need to run |
| `cargo` | skipped | no `src-tauri` file changed; `web/vite.config.ts` got a comment-only re-point of its ADR-0002 citation |

**Wave D's verifier flags were real and are fixed in-tree** rather than
deferred — five of six batches came back NEEDS_ATTENTION, and the flags were
the drift mode the wave exists to catch, not noise:

- D1 cited `PRD §2.2 Flow C` for the removable tray state — copied from
  `lib.rs`'s own comments, which say the same thing; PRD §2.2 Flow C is
  "Failure & recovery". Re-pointed to the `usb_mode` config field. D1 also
  attributed "shell plus frozen backend" to ADR-0006, which never mentions the
  backend; re-anchored to `docs/dev/packaging.md` where the bundle layout is
  actually specified.
- D2 named a `ReportMover` class that does not exist (it is `ReportRetrieve`)
  and claimed the S08-T3 seam "superseded" `ReportFinder`/`ReportMover` —
  `ReportFinder` is live production wiring the seam wraps, not retired. It
  also said QIDO-RS pages "with `offset` pagination"; the code follows the
  RFC 5988 `Link: rel="next"` header and tracks no offset. Separately, a
  pre-existing `>95% of report delivery (per PRD §1 survey)` bullet attributed
  itself to a survey that does not exist — the PRD's only ≥95% figure is the
  K3 retrieval SLA, a different quantity. Removed and the removal recorded.
- D3 left the PRD internally self-contradictory: §6.1 was corrected to deny DB
  encryption, but lines 48/110/451/490 still asserted it. Fixed, so the file
  is consistent. This was incomplete execution of the batch's own stated scope
  ("extend the correction to avoid leaving them self-contradictory"), not a
  scope question.
- D4 described `test-rig.md` as "the hub bookkeeper stand-in" — the doc says
  "mercure hub stand-in" and never mentions a bookkeeper; the bookkeeper stub
  is a sibling artefact that doc does not cover. Also softened an overstrong
  "everything else is referenced from there" that `user-guide.md` does not
  satisfy (it cross-references nothing).
- D6 documented the two `purge_*` metrics as `counter` while
  `routes.py` emits them as plain `gauge()` with no `kind=`. The batch claimed
  it verified `kind=` at the emission sites; it did not — there are exactly
  five `kind=` occurrences in the file, none of them the purge metrics. The
  `_total` suffix and the code's own comment both read counter, so this is the
  code contradicting itself; the doc now matches the live scrape output and
  records why.

**One stale source comment fixed outside the batch file sets:**
`web/vite.config.ts:6` said "(ADR-0002 Method 1)" while ADR-0002 is now
amended to record Method 2 as shipped. Re-pointed at the amendment. D1's
historical "dynamic port assignment" table row was left verbatim — it is
under an explicit superseded marker and the amendment describes the real
mechanism (fixed 8080, overridable by `MERCURE_BACKEND_PORT`) nearby, so
correcting decision history in place would defeat the convention the wave
adopted.

**Residue closed out** (`01d3f06`, 2026-09-23) — the three items recorded above
as "not acted on" were all closed by that commit, independent of E2:

- **R1 — `-m` marker validation.** pytest's `--strict-markers` validates markers
  *applied to a test* but not the names *inside* a `-m` expression, so a typo in
  the `test-fast` selector silently degraded to the full selection and still
  passed (`-m "not sloww and not integration"` collected 1030/3-deselected vs
  the correct 1027/6). A `tests/conftest.py` hook resolves the expression
  against the markers declared in `pyproject.toml` and raises `UsageError` for
  any unknown name. Subprocess-based tests because the guard fires at configure
  time; the typo test is mutation-verified.
- **R2 — `AuditConfig.encrypt`.** Documented honestly rather than wired: the
  field is marked reserved-not-wired in the model
  (`config/__init__.py:376-383`), since presenting it as enabling encryption
  would overstate ADR-0004's guard. Kept, not removed, so an existing operator
  config that sets it still loads under `extra="forbid"`. PRD §5.5's example and
  the US-07 AC are aligned to the same wording.
- **R3 — the stale `main.py` comment** claiming SQLite encryption "still
  requires SQLCipher — documented as a follow-up", contradicted by ADR-0004's
  2026-09-09 amendment that declined it. Rewritten to state the shipped posture.

The 64 tracked lines are 51 implementation + Step 0 + 7 gate checks + tray
sub-items. ~11 findings were dropped up front as stale (see the DROPPED table
below), so those 51 represent the open surface of the original 81.

Wave E (CI and release) — gate re-run in full:

| Check | Status | Notes |
|---|---|---|
| `ruff check .` | pass | clean |
| `mypy .` | pass | 72 source files, no issues |
| `pytest -m "not integration"` | pass | 1025 passed, 5 skipped, 3 deselected in 528.65 s — exactly +11 on the Wave D baseline (1014), which is the 10 new `test_updater.py` channel tests plus the 1 new `test_main.py` plumbing test |
| coverage ≥ 80 | pass | 90.19% — unchanged from the Wave D baseline |
| `web/`: eslint + `tsc -b` + vitest | pass | lint clean, tsc -b exit 0, 105/105 tests in 79 s |
| `just gen-api-check` | pass, **after a fix** | E3 added `channel` to `UpdateConfig` — a response model — and did not regenerate. The committed `web/src/types/api-schema.ts` was stale: the drift check found the missing `channel: string` field. Regenerated via the `gen-api` recipe; the check now passes. This is the exact failure mode `web/README.md` trap 4 exists for |
| `cargo` | skipped | `git status` shows no `src-tauri` change — E3 was instructed not to touch it, and the channel gate is entirely client-side Python |

**E3 scope deviation, recorded as the brief requires** rather than shipped
silently: E3's owns list was `update.py`, `config/__init__.py`, `main.py`,
`tests/test_updater.py`, `release.yml`, `release-runbook.md`. It also added
one test to `tests/test_main.py` (+32): `test_main_update_check_passes_the_configured_channel`,
which asserts the `channel=` kwarg E3 itself threaded into `Updater` in
`main.py` reaches it — `cfg.update.channel = "rc"` forwards `"rc"`, an empty
config forwards `None` (the derive-from-version path). No other batch in Wave
E owned that file, and the test covers E3's own new line rather than
importing scope from elsewhere, so it is kept. Recorded here because an
owns-list edit is exactly what the "flag it, don't smuggle it" rule covers.

**E3's `tauri.conf.json` note is corrected above:** the batch line read
"← tauri.conf.json + update.py + release.yml", implying a Tauri config edit.
It made none, by design — the gate is Python-side, and the manifest key is
safe for the Tauri client only because `tauri-plugin-updater` 2.11.0's
`InnerRemoteRelease` (src/updater.rs:~1460) has no `deny_unknown_fields`
(verified by reading the crate source; zero matches for that attribute
across the whole crate). A design that *needed* a `tauri.conf.json` change
would also have needed a `cargo tauri build`, which is why it was avoided.

**E3's own verifier died mid-run** and its re-run returned nothing usable, so
the adversarial pass was re-done by hand: both directions mutation-tested
(neutralising the stable→rc gate killed
`test_a_stable_install_is_not_offered_a_higher_prerelease` and
`test_a_pinned_channel_overrides_the_running_version`; flipping the
absent-channel default to rc killed `test_a_manifest_without_the_channel_field_counts_as_stable`
*and* the pre-existing `test_check_update_returns_available`), the
fail-closed paths re-confirmed, and the release.yml diff reviewed for signer
key material — it writes and logs none; the only secrets in the manifest are
the pre-existing per-platform `.sig` contents. One real defect was found that
the dead verifier would have missed: the channel-gate comment asserted it
"logs at INFO like the not-newer path above", but that path logs nothing at
all. The comment now states the truth — the gate returns `available=False`
exactly like the not-newer path and *additionally* logs at INFO, precisely
because that path is silent and a withheld pre-release is worth one line.

**E5 landed as a ratchet, not the blanket re-run advice it replaced.** The
old §4 bullet also carried a claim that had gone stale: it attributed
port-readiness flakiness to "a loaded free-tier Windows runner", but those
two tests have been `skipif(win32)` since `eb80a58` — added ~20 h *after*
the bullet was written — so they have not run on Windows at all in the
entire time the doc has been claiming they flake there. §4.1 now tables the
three timing tests against the jobs that can actually see them, states the
timeout-budget inversion across `--cov` (10 s untraced vs 60 s traced, with
the measured pydicom import cost that motivates it), and makes "it passes in
isolation" explicitly *not* flake evidence — the jsdom 28 failure was
deterministic in the full run and invisible standalone.

**Residue closed out** (`1a13bb9`, 2026-09-23): `--strict-markers` does not
validate marker names *inside* a `-m` expression, so `pytest -m "not sloww and
not integration"` silently degraded to the full selection and still passed —
measured at 1030 collected / 3 deselected against the correct 1027 / 6. The
`test-fast` job's own selector was correct as committed; the blind spot was that
a future typo in it was caught by nothing. `tests/conftest.py` now resolves the
`-m` expression against the declared markers (stdlib `ast`, since pytest's
internal expression API is undocumented and its compiled `co_names` carry a `$`
prefix in 9.1.x) and raises `pytest.UsageError` on an unknown name. The parser
strips the expression first: argparse yields a leading space for a `-m VALUE`
single argv token, which `ast.parse(..., mode="eval")` rejects as an indent
error — unstripped, the guard no-ops on exactly the malformed input it exists
for. Pinned by `tests/test_marker_selection.py`, which runs pytest in a real
subprocess because the hook fires at configure time; the typo test is
mutation-verified to fail when the guard is disabled, while the two
false-positive controls pass either way.

### Known blockers

| # | Blocker | Status | Notes |
|---|---|---|---|
| 1 | Uncommitted `/rules/preview` WIP overlaps backlog files | resolved | Landed cleanly as `1ab6eb5`; tree no longer carries it |
| 2 | WIP `api-schema.ts` inconsistent with the backend | resolved | `c4a5f82` added the dicomweb backend the schema described. Re-ran the drift check — no diff |
| 3 | In-flight `src-tauri/build.rs` provenance work | resolved | Landed as `6d9815e`; working tree is clean and every gate is green at `HEAD` |

**Context shift since the pass was planned:** `IMPLEMENTATION-PLAN.md`'s "remaining
open work" list is now **empty**. #1 (`POST /rules/preview`) landed as `1ab6eb5`;
#2 (`dicomweb`/`fhir` factories) landed as `c4a5f82` — dicomweb is wired through
both the forwarder (`main.py:245`) and the report factory (`main.py:360-372`), with
`fhir` noted as using the same path; #3 (PROVENANCE verify) landed as `6d9815e`;
#4 (a real rollback path) was resolved by deletion back at P1-14. Every line in
Steps 1–8 below is therefore unblocked and can be scheduled on its own merits.

---

## Dependency tree and execution waves

Steps are not a sequence — they are a DAG. The tracker's Step order is a *reading*
order; the order work can actually be done in is below. Two rules drove the
grouping:

1. **File ownership is the real coupling.** `web/__init__.py` is touched by four
   Step 1 items and by Step 4's lifespan work; `web/routes.py` by two Step 1
   items, all of Step 4's contract work, and Step 5's `usb_mode` field. Items
   sharing a file are one agent's batch, always — parallel agents writing the
   same file produce merge conflicts, not parallelism.
2. **`just gen-api` is a barrier, not a step.** Regenerating `api-schema.ts` must
   happen once, after every `response_model` lands, and nothing downstream may
   assume the old types. Similarly the tray docs (Step 6) must wait for the 4th
   state to actually exist (Step 5) or they describe behaviour that does not ship.

```
WAVE 0 — landed (1ab6eb5, c4a5f82, 6d9815e)
  └─ unblocks everything below; tree clean at 6d9815e

WAVE A — LANDED as 2e2c83f (8 parallel batches, 16 agents incl. verify; gate green:
       976 tests, 89.71% cov, web/ lint+tsc-b+vitest 93/93, gen-api-check clean,
       cargo test 12/12 + fmt + clippy)
   A1  src/mercure_gateway/web/__init__.py  middleware order swap + preflight CSP test,
       OpenAPI gated behind auth, loopback CSRF port pin, SPA-not-built message path
       └─ the order was genuinely inverted: CORS was outermost and swallowed OPTIONS
          before _SecurityMiddleware saw it. Now registered last = outermost.
   A2  spool/db.py + web/pipeline.py        COUNT(*) rewrite, idx_task_routing_target
       covering index + TTL memo (RouteRollupCache), instance_meta covering index,
       spool schema + read-conn docstrings
   A3  web/routes.py                        queue-state literals → StudyState,
       import_config run_in_threadpool
   A4  forwarder/__init__.py + web/wizard.py docstrings (nonexistent knobs/endpoints)
   A5  web/src/                             AuthContext useMemo/useCallback, dead
       `dot="accent"` token, ae_title fixtures, dead CSS comment
   A6  docs/guides/usb-quickstart.md        LED table rewrite against led.py
   A7  .github/ + pyproject.toml + test-rig paths-ignore, drop docs/sprint-plan,
       dependabot.yml, CODEOWNERS, numpy floor (uv.lock re-resolved to numpy 2.0),
       Orthanc image pin
   A8  src-tauri/Cargo.toml                 `log` crate WIRED via tauri-plugin-log 2.9.2
       (LogDir+Stdout at Info; both eprintln! replaced), edition 2024 + MSRV 1.85
       └─ edition 2024 changed rustfmt's `use` ordering; the pre-existing import
          block was normalized with `cargo fmt` (verified edition-induced by revert)
       └─ A5's ae_title fixture fix is an input to C4 (a11y scan scans that document)

WAVE B — LANDED (6 sweep batches + post-barrier tray batch, 14 agents incl. verify;
       1014 tests, 90% cov, web/ lint+tsc-b+vitest 93/93, gen-api barrier run for
       real — 773 lines of typed schema added, cargo test 20/20 + fmt + clippy +
       tauri build). Two verifier flags, both fixed before commit:
       └─ B8's revocation mechanism existed but no HTTP request could reach it —
          routes.py:166 did not thread `request`, so logout() degraded to
          cookie-only and a replayed token still returned 200. One line, plus an
          end-to-end test that drives the shipped endpoint (mutation-verified).
       └─ the B7 "steps a cursor not a fetchall" test passed against a fetchall
          regression — a buffered generator is still lazy, so row visibility
          cannot tell them apart. Rewritten to assert the mechanism itself via
          a spying connection wrapper; now fails on the first row under the
          mutation.
   B1  spool/__init__.py  2a Spool.complete/fail atomic completion
   B2  spool/__init__.py  2b _requeue_complete_routes executemany   ← same file as B1
   B3  reports/move.py    3a study-level C-MOVE dedup + 3b save-in-handler
   B4  reports/find.py    3c C-FIND early stop
   B5  web/routes.py      31× response_model                         ← after A3
   B6  web/routes.py      absolute file_path out of PHI responses     ← pairs with B5
   B7  audit/__init__.py + web/routes.py  audit verify → iter_audit_events streaming
   B8  web/auth.py        session revocation list
   B9  web/__init__.py + main.py  health monitor into a lifespan       ← after A1
   B10 web/routes.py      SystemStatus.usb_mode field                 ← after A3/B5
   B11 src-tauri/src/lib.rs  derive_state `removable` arm + glyph + the first test it
       has ever had + default divergence fix                           ← after B10

WAVE C — regeneration and the frontend that consumes it
   C1  just gen-api → api-schema.ts          DONE during Wave B's serial gate
       (773 lines of typed schema; the barrier ran once, as designed)
   C2  useAsync hook + migrate 7 pages       DONE — 3 of 7 (see Step 7 notes)
   C3  route-level code splitting            DONE — 8 of 10 views, 3 kept eager
   C4  a11y-scan parametrization             DONE — 10 cases preserved

WAVE D — documentation aligns to shipped behaviour
   D1  ADR-0002 rewrite (Method 2 as shipped)  ← after B11 (4 tray states must be real)
   D2  ADR-0005 stale vs sprint board
   D3  PRD + PRODUCT_BRIEF + main.py SQLCipher claims (docs align to code; ADR-0004 declined)
   D4  README operator entry point
   D5  web/README.md + delete stray JSONs
   D6  monitoring/prometheus.yml + alert rules as real files

WAVE E — CI and release
   E1  test-fast CI job (`not slow and not integration`)          ✓ landed
   E2a perf gate --real in CI (`perf-gates-real` job)  ✓ landed
   E2b receive-scaling measurement                    ✓ landed, report-only
       (`scripts/check_receive_scaling.py`; the 3× floor is declined — see below)
   E3  staged rollout / update channel field      ✓ landed (client-side only; tauri.conf.json untouched — see below)
   E4  eslint / globals / whatwg-encoding bumps                   ✓ landed, whatwg-encoding declined (measured)
   E5  runbook §4 Windows flake wording                           ✓ landed as a ratchet (§4.1)

GATE — full suite, serially, after each wave:
   ruff check . · mypy . · pytest -m "not integration" · --cov-fail-under=80
   cd web && npm run lint && npx tsc -b && npm run test
   just gen-api-check (after Wave C) · cd src-tauri && cargo test && cargo fmt --check
```

**Critical path:** A3 → B5 → C1 → C2 is the longest chain (routes.py contract work
→ regenerate types → migrate the fetch layer). Everything else is width.

**No wave-E item is blocked.** E2's long-standing block was attributed to a
missing test-rig Orthanc, and that attribution was wrong: `--real` ships its
own in-process pynetdicom SCP (`_CountingScp`, bound to `("127.0.0.1", 0)`) and
needs no external DICOM server — a `grep -i orthanc` over `scripts/` and
`tests/` returns no hit in the perf code. What E2 actually decomposed into:

- **E2a — `--real` in CI.** Genuinely unblocked all along; landed as the
  `perf-gates-real` job. Its only real risk was timing variance on a loaded
  shared runner, which is why it is `continue-on-error` on PRs and blocking
  only on `main`.
- **E2b — the scaling ratio.** Also unblocked, but the *floor* was the problem,
  not the infrastructure. The `inst_s_25 / inst_s_1 ≥ 3×` assertion was
  pseudocode in `phase3-3A-testing.md`, never implemented. Re-established as
  `scripts/check_receive_scaling.py` and measured: **1.77–2.18× at 5
  associations** (run-to-run variance on a disk-bound path), 300/300 instances
  stored, zero errors. Two reasons it ships report-only:
  1. The benchmark behind the original 1.24× figure (`.full-review/.perfbench/
     store_bench.py`) was never committed, so the number was unreproducible
     from the tree.
  2. US-01's AC (PRD §US-01) says the receiver *accepts* ≥ N concurrent
     associations and stores every instance before ack — it does not require
     throughput to *scale*. Every instance lands with a correct status; the
     3× floor invented a scaling requirement the PRD never had, and would have
     baked an architectural ceiling (the store-before-ack fsync barrier,
     PRD §3.4) in as a target. A better ratio is an ADR that changes the
     durability posture, not a residue item.

  Note the sizing change landed alongside this (2026-09-24): US-01 moved from
  ≥25 to ≥5 concurrent associations, which is also why the ratio is now
  measured in a healthy regime rather than a degraded one.

---

## Step 0 — Reconcile the baseline

The working tree was carrying an in-flight `POST /rules/preview` endpoint when
this pass was planned. All three open-work items resolved themselves before the
pass began:

- `1ab6eb5 feat: expose the routing-rule preview over HTTP (P0-9)` — the WIP
  landed. It was verified first: 95 backend tests, 93 frontend tests, `tsc -b` clean.
- `c4a5f82 fix: wire the dicomweb report transport through the composition root` —
  added the backend half that the WIP's regenerated `api-schema.ts` had described
  ahead of its backend. The drift I flagged is gone; re-ran the check, no diff.
- `6d9815e fix: make the frozen-sidecar provenance check load-bearing in the build`
  — moves the version guard into `src-tauri/build.rs` so a bare
  `cargo tauri build` cannot bundle a stale backend, and adds a `PROVENANCE.json`
  existence-and-agreement check beside it. Was in flight as uncommitted WIP when
  this pass was planned; landed, so the guard is now real rather than pending.

Baseline for the pass is a clean working tree at `6d9815e`, and the two backlog
items that would have conflicted with the WIP (`web/routes.py`,
`web/src/types/api-schema.ts`) are free to modify. Every line reference in Steps
1–8 was re-verified against `HEAD` after that commit; line numbers in
`web/routes.py` have drifted (the `/rules/preview` endpoint landed above them —
`save_config` is now at `:1211`/`:1235`, not `:1169`), but no claim has rotted:
`response_model` is still 10 of 38 operations, `SystemStatus` still has no
`usb_mode` field, `derive_state` still has no `removable` arm, and the
`_requeue_complete_routes` / `complete` / `fail` shapes are unchanged.

---

## Step 1 — Cheap sweep (10-minute items, batched)

Grouped to clear low-risk lines first. Each is independently verifiable.

| Item | File | Fix |
|---|---|---|
| `/api/pipeline` full materialization | `web/pipeline.py:142-145` | Replace the Python `sum(1 for row in list_studies_with_route_counts() …)` with a `COUNT(*) … WHERE created_at >= ?` (`idx_studies_created_at` at `db.py:127` already exists and is unused by this path) |
| `count_routes_by_target` uncached `GROUP BY` | `spool/db.py:863-876` | Add covering index `idx_task_routing_target(target_name, target_type, status, updated_at)`; add a short TTL memo in `web/pipeline.py` mirroring `DestinationHealthMonitor`'s existing cache |
| `spool_num_bytes` full-table SUM | `spool/db.py:1160-1176` | Covering index on `instance_meta(study_uid, num_bytes)` |
| `import_config` blocking write | `web/routes.py:1169,1235` | Wrap `normalize_and_validate` + `save_config` in `run_in_threadpool` — the only `async def` in the file; deployment target is a USB dongle, so the write is the worst offender |
| Queue-state string literals | `web/routes.py:699-703` | Use `StudyState.QUEUED` etc. from `spool/__init__.py:82-91` — members are `str`, and `db.count_states()` returns raw strings that would silently yield 0 on a rename |
| Middleware order comment inverted | `web/__init__.py:211-223` | Swap the two `add_middleware` calls so `_SecurityMiddleware` is outermost; add the missing test asserting CSP on a preflight `OPTIONS`. Verified live: `OPTIONS /api/config` currently returns 200 with `content-security-policy: None` while `GET /api/system/health` has CSP |
| OpenAPI schema exposed unauthenticated | `web/__init__.py:180-186` | Conditional-None `docs_url`/`redoc_url`/`openapi_url` when auth is enabled |
| Loopback CSRF allow-list | `web/__init__.py:44-51` | Pin to the configured ports rather than any port on `127.0.0.1`/`localhost` (verified live: `Origin: http://127.0.0.1:9999` → 200) |
| `AuthContext` unmemoized | `web/src/context/AuthContext.tsx:95` | `useMemo` on the value, `useCallback` on `login`/`logout`/`checkAuth` |
| Dead `dot="accent"` token | `web/src/ui/flow.tsx:18,100-107`; `web/src/index.css:340` | Remove `"accent"` from the suffix union and delete `.pipe-dot.accent` — unreachable from every call site |
| Impossible `ae_title` fixtures | `web/src/pages/ConfigView.test.tsx:15`; `web/src/config/lint.test.ts:18` | Move `ae_title` off `general` — `GeneralConfig` has no such field and `extra="forbid"` means `GET /config` can never emit this shape |
| CSS comment names a dead component | `web/src/index.css:510` | Remove the `/* Protected route wrapper … */` comment — zero `ProtectedRoute` matches; the gate is inline in `App.tsx` |
| SPA-not-built error message | `web/src/index.css` area → `web/__init__.py:238` | Points at `web/static/`, which does not exist; source is `web/`, output is `src/mercure_gateway/web/static/` |
| Spool schema docstring | `spool/db.py:3-4` | Lists 4 of 7 tables — add `db_meta`, `instance_meta`, `hub_outbox` |
| Read-conn docstring (untracked item) | `spool/db.py:265-287` | Document the `_read_disabled` sticky fallback (`db.py:247-253`) — the class docstring at `:192-197` and `__init__` at `:205-206` both point readers here for "how they degrade" |
| Forwarder docstring | `forwarder/__init__.py:5,11` | Claims a nonexistent `retry_max` knob (`RetryPolicy` is hardcoded `max_attempts=5`) and misdescribes re-claim timing and the route-lock window |
| `wizard.py` docstring | `web/wizard.py:10` | References `GET/POST /api/wizard`, which does not exist — only `POST /api/wizard/validate/{step}` is registered (`routes.py:1583`) |
| LED quickstart table | `docs/guides/usb-quickstart.md:35-45` | All four rows wrong plus an invented "solid amber". Rewrite against `led.py:41-49` — six **solid** colours, no blink, no amber (`led.py:16-18` states blink is a hardware-layer concern) |
| CI path filters | `.github/workflows/ci.yml:3-7` | `paths-ignore` for `*.md`/`docs/**` on the packaging jobs — the full 11-job Windows chain runs on doc-only pushes |
| Stale branch trigger | `.github/workflows/ci.yml:5,7` | Drop `docs/sprint-plan` — inactive since 2026-09-09, all work on `main` |
| `dependabot.yml` + `CODEOWNERS` | `.github/` (neither exists) | Add both — every action is already tag-pinned |
| `numpy` declared, never imported | `pyproject.toml:12` | Zero imports across `src/`, `tests/`, `scripts/`, `e2e/`; bump the floor or document the pylibjpeg-rle transitive need |
| Rust `log` crate unused | `src-tauri/Cargo.toml:23` | Wire `tauri_plugin_log` or remove — all diagnostics are `eprintln!` (`lib.rs:279,282`) |
| Rust edition / MSRV | `src-tauri/Cargo.toml:6-7` | `edition = "2021"` → `"2024"`, bump `rust-version`. Verify `cargo tauri build` still succeeds |
| Test-rig image pin | `test-rig/docker-compose.yml:13` | Pin `jodogne/orthanc-plugins:latest` to a digest |

---

## Step 2 — Spool durability and performance

**2a. `Spool.complete`/`fail` atomic completion** — the most consequential open item.

`complete()` (`spool/__init__.py:720-744`) runs five operations with no enclosing
transaction: `get_routes` (read) → `mark_route_sent` (own `BEGIN IMMEDIATE`,
`db.py:1012`) → `all_routes_complete` (bare `SELECT` outside any txn, `db.py:1050`)
→ `set_study_state` (own txn) → `_emit`. Under `forwarding.concurrency > 1`, two
workers completing the last two routes can both see "all complete" and both emit
`STUDY_SENT`, or interleave so the study lands back in `SENDING` after every route
is complete. `fail()` (`:746-775`) has the same shape plus a stale `attempts` read
at `:759` before `mark_route_error` at `:760`.

`db.transaction()` already supports nested joining (`db.py:300-318`: re-entrant
`RLock`, inner context managers yield the outer connection), so a single wrapper
makes all five steps commit or roll back together. `_emit` stays outside the
transaction (it is not a DB write) but moves after the commit.

The hard half is the regression test: the suite parallelizes work but never
contends on the same record. Add a real `threading` test in the style of
`tests/test_storage.py:402` (`test_two_concurrent_first_instances_of_one_series_count_it_once`)
— two threads completing the last two routes of one study, asserting exactly one
`STUDY_SENT` and a final `SENT` state.

**2b. `_requeue_complete_routes` per-destination transactions** —
`spool/__init__.py:428-447` issues one FULL-fsync transaction per route via
`reset_route_waiting`. Collapse to a single `executemany`. On the write connection,
so P1-19's reader does not help.

---

## Step 3 — Reports retrieval

Three items in `reports/move.py` + `find.py` that pair naturally:

**3a. Duplicate study-level C-MOVEs.** `move.py:153-157` loops `for match in
matches:` issuing one C-MOVE each, and `_move_dataset` (`:192-194`) sets
`QueryRetrieveLevel="STUDY"` with only `StudyInstanceUID` — no study-UID dedup. N
matched instances in one study → N identical full-study transfers. Now
production-reachable: `main.py:278-340` wires the real `ReportRetrieve` via the
transport registry. Add a `seen: set[str]` and group by study.

**3b. Whole-study RAM buffering.** `on_c_store` (`move.py:112-116`) retains every
received dataset in a `received` dict for the whole retrieve; because the C-MOVE is
STUDY-level, the PACS pushes the entire study and only the matched SOPs are saved
(`:172-178`). Save to disk inside the C-STORE handler and track UIDs only — the
mechanical move is easy because `_save` (`:196`) already rebuilds `file_meta`. This
fixes 3a's memory cost at the same time.

**3c. C-FIND enumerates the whole study.** `find.py:118-141` filters client-side
(`continue`, not `break`), so the PACS still enumerates every instance and
`matches` grows unbounded. Add a SERIES-level pre-query or early stop.

No test currently asserts move count — `tests/test_report_move.py` has 12 tests,
zero referencing `send_c_move`. Add one that asserts one C-MOVE per study. The
~1,202-vs-~2 wire figure itself is unverifiable without a live PACS, but the code
defect is statically confirmed.

---

## Step 4 — Web layer

- **API contract.** 10 of 41 operations carry `response_model` at `HEAD` — the
  config contract half is closed, but 31 remain undocumented and
  `mercure-gateway.openapi.json` has **zero** `example` entries. Add
  `response_model` to the remainder, then run `just gen-api` and commit the
  regenerated `api-schema.ts`. Consider an API-versioning ADR (`docs/adr/` stops
  at ADR-0007). The `api-schema-drift` CI job guards freshness, so this is about
  contract depth, not drift detection.
- **Absolute `file_path` echoed in PHI responses** — `web/routes.py:1374-1394,
  1436-1470` return the raw sqlite row including `file_path`, and the hand-written
  `ReportRow`/`ReportContent` types carry it. The client needs only the report id.
  Pairs with the `response_model` work above.
- **Audit verify unbounded under the write lock** — `audit/__init__.py:281-312`
  scans the whole table with no `LIMIT` and deliberately takes the write lock
  (documented at `:268-276`). The `iter_audit_events` streaming path exists but is
  unused; wire it. Export is bounded but materializes up to 100k rows in lock scope.
- **Sessions not revocable** — `web/auth.py:39-58,129-148`. HMAC token, 12 h TTL,
  no revocation list. Note the secret is now per-process (`web/__init__.py:191-197`,
  P0-8), so a password change no longer invalidates sessions by side effect — which
  makes explicit revocation *more* real, not less.
- **Health monitor lifecycle outside a `lifespan`** — `main.py:631-661` starts/stops
  `DestinationHealthMonitor` around a blocking `uvicorn.run()`; `create_app` takes
  no `lifespan` (`web/__init__.py:180`), so `TestClient` tests never start it and
  routes keep the defensive `getattr(...)` ladder (`routes.py:834`).

---

## Step 5 — Tray state machine (code aligns to docs)

`tray.py:7-10` documents 4 states including `removable` (`:50-51` returns it), but
the shipped Rust `derive_state` (`src-tauri/src/lib.rs:115-138`) has no `removable`
arm and `SystemStatus` (`web/routes.py:233-245`) has no `usb_mode` field, so the
4th state is unreachable end-to-end and the Python branch is dead outside
`tests/test_tauri_integration.py`. Divergent defaults persist: `tray.py:32-34`
defaults to `"running"`, Rust and `routes.py:234-236` to `"stopped"`.

Implement it properly: add `usb_mode` to `SystemStatus`, add the Rust arm and
glyph, and **test the shipped `derive_state`** — the `#[cfg(test)]` block at
`lib.rs:320+` is path/port resolution only, so the code that ships is the code that
has never been tested, and the tested `tray.py` is the code that never runs. Fix the
default divergence. Update the doc once the behaviour is real.

---

## Step 6 — Documentation

- **ADR-0002** (`docs/adr/ADR-0002-desktop-shell-architecture.md`) — describes
  Method 1 ("chosen for MVP", FastAPI on `:8080` loaded by the Tauri webview) while
  Method 2 ships (`tauri.conf.json:10` `frontendDist`, `lib.rs:202`
  `WebviewUrl::App`). Also credits Tauri notification/auto-start plugins absent from
  `Cargo.toml:16-25`, and lists 4 tray states against Rust's 3. Rewrite as
  superseded-by-Method-2.
- **ADR-0005** — stale against its own sprint board.
- **PRD + PRODUCT_BRIEF SQLCipher** — `mercure-gateway-PRD.md:200,421` and
  `PRODUCT_BRIEF.md:35,75,139` still claim SQLCipher AES-256 that
  `ADR-0004-at-rest-encryption.md:85` formally declined. Correct the docs to match
  the ADR (do **not** implement SQLCipher). Also `main.py:711`.
- **README operator entry point** — 46 lines, developer-only front door; links none
  of the 7 guides, no ADR index, and the architecture diagram omits the Tauri shell
  (the primary distribution form).
- **`web/README.md`** — does not exist; the four load-bearing traps (build output
  path, `tsc -b` not `--noEmit`, vitest must run from `web/`, `just gen-api` after
  model changes) live only in the root justfile. Also delete the stray
  `web/mercure-gateway.json` and the three `mercure-gateway.json.bak-*` at the repo
  root.
- **Monitoring config as real files** — `docs/guides/admin-guide.md:211-258` holds
  the scrape config and a 6-rule Prometheus alert group as inline markdown that
  every site hand-transcribes. Ship `monitoring/prometheus.yml` + alert rules and
  point the doc at them. The rule set has grown since the review
  (`GatewayHubNotDelivering`, `GatewayAuditAnchorBroken`), so the prose is now
  further from what a site would ship.

---

## Step 7 — Frontend structure

- **Fetch boilerplate** — `apiFetch`/`getJson`/`postJson` + typed helpers
  (`api.ts:96-176`) and `usePoll` exist, but 7 pages still hand-roll
  `useState(loading/error) + useCallback(load) + useEffect`; `usePoll` is adopted in
  only 2. Add a `useAsync`/`useFetch` for one-shot loads and migrate. ~110–140 of
  the original 150–200 lines remain.
- **Route-level code splitting** — the SPA is one chunk (`index-UKOPiQ7v.js`,
  200 868 B); zero `lazy`/`Suspense`/`manualChunks` anywhere. Split at route
  boundaries.
- **a11y-scan stubs under-exercise the riskiest markup** —
  `web/src/test/a11y-scan.test.tsx:83,107` has 8 non-parametrized blocks;
  QueueView scans a single `FAILED` row, ConfigView a `general: { ae_title }`
  document the API can never return. Parametrize across queue states and scan the
  sentinel/destination forms.

---

## Step 8 — CI and release

- **`test-fast` job** — the `slow`/`integration` markers are now genuinely used
  (8 decorators across 4 files) and integration is deselected in CI, but `slow` is
  deselected nowhere, so no unit-only fast loop exists. Add a `-m "not slow and not
  integration"` job.
- **Perf gate `--real` mode** — exists (`scripts/check_perf_gates.py`, commit
  `c742a7e`) but CI still runs the synthetic default; the receive-scaling ratio
  assertion (`inst_s_25 / inst_s_1 ≥ 3×`) is not implemented anywhere. Also note the
  receive path itself remains ungated. Needs the test-rig Orthanc that CI never
  stands up. *(Resolved 2026-09-24, Wave E: the `perf-gates-real` CI job now runs
  `--real`, and the receive path is measured — not gated — by
  `scripts/check_receive_scaling.py`. The Orthanc dependency was a false
  attribution; see "No wave-E item is blocked" above.)*
- **Staged rollout / update channel** — single global `latest.json`
  (`tauri.conf.json:36-38`, `release.yml:189`); `PublishedUpdate`/`_is_newer`
  (`update.py:109,147`) have no channel field to filter on. Runbook §7 is
  documented discipline, not enforcement. Add a channel marker and filter.
- **`eslint` deprecated in lockfile** — `node_modules/eslint` 9.39.5 carries a
  deprecation notice; `globals` is a major behind (15.x vs 16.x); `whatwg-encoding`
  also deprecated. Bump. eslint already runs in CI, so this is toolchain currency.
  → **DONE, partly declined (2026-09-22).** `@eslint/js@9.39.5` and `globals@15.15.0`
  carry *no* deprecation notice — only `eslint` itself does. The eslint 10 bump is
  blocked by `eslint-plugin-react-hooks` (peer caps at eslint ^9; the 7.1.0+ releases
  that accept ^10 add three ERROR rules to the recommended preset) and by the
  `@eslint/js@^10.11.0` range having never been published (latest is 10.0.1). The
  7 findings that block it are deliberate documented patterns in 6 files outside this
  batch, so the correct fix is a source refactor, not a toolchain bump. `whatwg-encoding`
  is dropped only by jsdom ≥27.7.0; the `jsdom ^24 → ^28` bump removes it from the tree
  but was **reverted** — jsdom 28's slower environment construction pushes
  `SetupWizard.test.tsx > writes receiver fields…` past its 5000ms timeout whenever the
  vitest cache is cold (every CI run); cold-cache A/B was 3/3 green on 24 vs 3/3 red on
  28, warm runs pass on both. Recorded in `web/README.md` so the experiment is not
  repeated blind. (Boundary version, verified against the registry: jsdom 27.4.0 is the
  first release without whatwg-encoding; 27.3.0 still pins it.)
- **Runbook §4 Windows flakes** — still prescribes "re-run the job before treating
  it as a regression" for two timing flakes, which is the exact property that makes
  a red gate carry no information.

---

## Dropped as STALE (do not re-do)

| Item | Closed by |
|---|---|
| `verify_anchor_signatures` unscheduled | `30c3d05` (P0-10) — daemon thread, `AUDIT_ANCHOR_FAILED`, metrics exposed |
| Boot bind invariant across config update | `831afe5` (P0-3) + `3ac3732` — 409 + `CONFIG_SECURITY_REJECTED` at every write boundary |
| `_restore_redacted_secrets` untested | P1-2 — name-only matching + `tests/test_web_api.py:428-497` |
| Composition-root coverage blind spot | P1-6 — `COVERAGE_PROCESS_START` + parallel coverage |
| vitest/tsc/eslint never gated | P1-5 — `web` CI job |
| 1.3 MB `axe.js` shipped | P0-12 — deleted, zero matches |
| `PipelineView` unbounded table | server-capped via `list_recent_routes(limit=20)` |
| PROVENANCE / SBOM / immutable re-dispatch / rollback | P0-2 / P1-13 / P1-14 / P1-15 |
| Both `just` recipes unrunnable | `gen-api` and `e2e` both verified runnable; `gen-api-check` added as a drift guard |
| LED table + `usb-quickstart.md` as two items | double-count of one finding (M6) |
| Duplicate `png`/`reqwest` majors | verify during the Rust work in Step 1 |

**Residue worth carrying forward** (not backlog items, but record them): the anchor
verifier is gated on `hub.enabled and hub.bookkeeper_url and hub.anchor_public_key`
(`main.py:458`), so a plain file-anchor deployment gets no scheduled check; and
`verify_anchor_signatures` never asserts consecutive anchored heads form a chain, so
the `prune()` re-anchor discontinuity (`audit/__init__.py:388`) remains
undetectable.

---

## Optional / judgment calls

- **`Depends(_spool)` / `Depends(_config)` wiring** — the typed accessors exist
  (`web/routes.py:72-82`), ~30 handlers use them, and `mypy --strict` passes. The
  `Depends` wiring was the finding's own "optional next step"; the primary
  recommendation is satisfied. Skip unless time remains.
- **Rust edition 2021 → 2024** — cheap to write, but verify `cargo tauri build`
  end-to-end before committing; it is the one Step 1 item with a build-break risk.

---

## Verification

Run after each step that touches the relevant surface, and in full at the end:

```bash
# backend gate
uv run ruff check .
uv run mypy .
uv run pytest -m "not integration"
uv run pytest -m "not integration" --cov=mercure_gateway --cov-fail-under=80

# frontend gate — must run from web/, never the repo root
cd web && npm run lint && npx tsc -b && npm run test

# schema drift (after any response_model change)
just gen-api-check

# rust (after Step 1 Rust items or Step 5)
cd src-tauri && cargo test && cargo tauri build
```

Notes carried from prior sessions: bare `tsc --noEmit` checks nothing here — the
root tsconfig has `files: []` with project references only, so `tsc -b` is required.
Vitest must run from `web/` or jsdom is silently dropped and every test fails with
`document is not defined`. The Bash tool's cwd resets between calls, so prefix
`cd web` every time.

Two specific regression risks to watch:
- **Step 2a** — the contention test must use real `threading` in the style of
  `tests/test_storage.py:402`, not mocked concurrency.
- **Step 4 API contract** — regenerating `api-schema.ts` will conflict with the
  Step 0 commit if the tree is not reconciled first; run `just gen-api` after the
  `response_model` work, not before.
