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

| Item | Status | Notes |
|---|---|---|
| `/api/pipeline` → `COUNT(*)` | todo | |
| `count_routes_by_target` index + TTL memo | todo | |
| `spool_num_bytes` covering index | todo | |
| `import_config` `run_in_threadpool` | todo | |
| Queue-state literals → `StudyState` | todo | |
| Middleware order swap + preflight test | todo | |
| OpenAPI gated behind auth | todo | |
| Loopback CSRF port pin | todo | |
| `AuthContext` `useMemo` | todo | |
| Dead `dot="accent"` token | todo | |
| `ae_title` fixtures | todo | |
| CSS `ProtectedRoute` comment | todo | |
| SPA-not-built error message path | todo | |
| Spool schema docstring (4→7 tables) | todo | |
| Read-conn `_read_disabled` docstring | todo | untracked item |
| Forwarder docstring (`retry_max`) | todo | |
| `wizard.py` docstring | todo | |
| LED quickstart table | todo | docs align to code |
| CI `paths-ignore` for docs | todo | |
| Drop `docs/sprint-plan` trigger | todo | |
| `dependabot.yml` + `CODEOWNERS` | todo | |
| `numpy` floor | todo | |
| Rust `log` crate | todo | |
| Rust edition 2024 / MSRV | todo | build-break risk — verify `cargo tauri build` |
| Test-rig image pin | todo | |

### Step 2 — Spool durability and performance

| Item | Status | Notes |
|---|---|---|
| 2a `Spool.complete`/`fail` atomic completion | todo | needs real-threading contention test |
| 2b `_requeue_complete_routes` single `executemany` | todo | |

### Step 3 — Reports retrieval

| Item | Status | Notes |
|---|---|---|
| 3a Study-level C-MOVE dedup | todo | add a move-count test |
| 3b Save-in-handler, no RAM buffering | todo | pairs with 3a |
| 3c C-FIND early stop | todo | |

### Step 4 — Web layer

| Item | Status | Notes |
|---|---|---|
| `response_model` on 31 remaining ops | todo | then `just gen-api` |
| Absolute `file_path` out of PHI responses | todo | |
| Audit verify → `iter_audit_events` streaming | todo | |
| Session revocation | todo | |
| Health monitor into a `lifespan` | todo | |

### Step 5 — Tray state machine

| Item | Status | Notes |
|---|---|---|
| `SystemStatus.usb_mode` field | todo | |
| Rust `derive_state` `removable` arm + glyph | todo | |
| Test the shipped `derive_state` | todo | first test it has ever had |
| Fix divergent defaults (`running` vs `stopped`) | todo | |
| Update tray docs once behaviour is real | todo | |

### Step 6 — Documentation

| Item | Status | Notes |
|---|---|---|
| ADR-0002 rewrite (Method 2 as shipped) | todo | |
| ADR-0005 stale vs sprint board | todo | |
| PRD + PRODUCT_BRIEF SQLCipher claims | todo | docs align to code; ADR-0004 declined |
| README operator entry point | todo | |
| `web/README.md` + delete stray JSONs | todo | |
| Monitoring config as real YAML files | todo | |

### Step 7 — Frontend structure

| Item | Status | Notes |
|---|---|---|
| `useAsync` hook + migrate 7 pages | todo | |
| Route-level code splitting | todo | |
| a11y-scan parametrization | todo | |

### Step 8 — CI and release

| Item | Status | Notes |
|---|---|---|
| `test-fast` CI job | todo | |
| Perf gate `--real` in CI + scaling ratio | todo | needs test-rig Orthanc |
| Staged rollout / update channel | todo | |
| `eslint` / `globals` / `whatwg-encoding` bumps | todo | |
| Runbook §4 Windows flake wording | todo | |

### Gate

| Check | Status | Notes |
|---|---|---|
| `ruff check .` | todo | |
| `mypy .` | todo | |
| `pytest -m "not integration"` | todo | |
| coverage ≥ 80 | todo | |
| `web/`: eslint + `tsc -b` + vitest | todo | must run from `web/` |
| `just gen-api-check` | todo | after `response_model` work |
| `cargo test` + `cargo tauri build` | todo | after Rust items |

**Tally:** 3 done · 51 todo · 0 dropped · 0 blocked — of 64 tracked lines
(51 implementation + Step 0 + 7 gate checks + tray sub-items). ~11 findings were
dropped up front as stale (see the DROPPED table below), so these 51 represent the
open surface of the original 81.

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

WAVE A — parallel, zero cross-dependencies
   A1  src/mercure_gateway/web/__init__.py  middleware order swap + preflight CSP test,
       OpenAPI gated behind auth, loopback CSRF port pin, SPA-not-built message path
   A2  spool/db.py + web/pipeline.py        COUNT(*) rewrite, idx_task_routing_target
       covering index + TTL memo, instance_meta covering index, spool schema + read-conn
       docstrings (same file as the indexes — one batch)
   A3  web/routes.py                        queue-state literals → StudyState,
       import_config run_in_threadpool
   A4  forwarder/__init__.py + web/wizard.py docstrings (nonexistent knobs/endpoints)
   A5  web/src/                             AuthContext useMemo/useCallback, dead
       `dot="accent"` token, ae_title fixtures, dead CSS comment
   A6  docs/guides/usb-quickstart.md        LED table rewrite against led.py
   A7  .github/ + pyproject.toml + test-rig paths-ignore, drop docs/sprint-plan,
       dependabot.yml, CODEOWNERS, numpy floor, Orthanc image pin
   A8  src-tauri/Cargo.toml                 `log` crate (wire or remove), edition 2024 + MSRV
       └─ A5's ae_title fixture fix is an input to C4 (a11y scan scans that document)

WAVE B — needs Wave A's files settled; B1–B11 interleave by file
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
       has ever had + default divergence fix                          ← after B10

WAVE C — regeneration and the frontend that consumes it
   C1  just gen-api → api-schema.ts          ← barrier: after B5/B6, single pass
   C2  useAsync hook + migrate 7 pages       ← after C1 if adopting generated types
   C3  route-level code splitting            ← independent of C1/C2
   C4  a11y-scan parametrization             ← after A5's fixture fix

WAVE D — documentation aligns to shipped behaviour
   D1  ADR-0002 rewrite (Method 2 as shipped)  ← after B11 (4 tray states must be real)
   D2  ADR-0005 stale vs sprint board
   D3  PRD + PRODUCT_BRIEF + main.py SQLCipher claims (docs align to code; ADR-0004 declined)
   D4  README operator entry point
   D5  web/README.md + delete stray JSONs
   D6  monitoring/prometheus.yml + alert rules as real files

WAVE E — CI and release
   E1  test-fast CI job (`not slow and not integration`)
   E2  perf gate --real in CI + scaling ratio     ← BLOCKED: needs test-rig Orthanc
   E3  staged rollout / update channel field      ← tauri.conf.json + update.py + release.yml
   E4  eslint / globals / whatwg-encoding bumps
   E5  runbook §4 Windows flake wording

GATE — full suite, serially, after each wave:
   ruff check . · mypy . · pytest -m "not integration" · --cov-fail-under=80
   cd web && npm run lint && npx tsc -b && npm run test
   just gen-api-check (after Wave C) · cd src-tauri && cargo test && cargo fmt --check
```

**Critical path:** A3 → B5 → C1 → C2 is the longest chain (routes.py contract work
→ regenerate types → migrate the fetch layer). Everything else is width.

**Genuinely blocked:** E2. The `--real` perf mode needs a live Orthanc on the CI
runner; the receive-scaling ratio assertion (`inst_s_25 / inst_s_1 ≥ 3×`) is
unverifiable without one. Tracked as blocked, not forced.

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
  stands up.
- **Staged rollout / update channel** — single global `latest.json`
  (`tauri.conf.json:36-38`, `release.yml:189`); `PublishedUpdate`/`_is_newer`
  (`update.py:109,147`) have no channel field to filter on. Runbook §7 is
  documented discipline, not enforcement. Add a channel marker and filter.
- **`eslint` deprecated in lockfile** — `node_modules/eslint` 9.39.5 carries a
  deprecation notice; `globals` is a major behind (15.x vs 16.x); `whatwg-encoding`
  also deprecated. Bump. eslint already runs in CI, so this is toolchain currency.
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
