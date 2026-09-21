# Session handoff — dicom-gateway P2/P3 sweep

**Purpose:** let a fresh, clean session pick up the remaining waves of the P2/P3
implementation with no archaeology. Read this, then read
`.full-review/P2-P3-BACKLOG.md` (the source of truth), then start.

**Standing user request (verbatim, still governing):** *"next i have planned out
the P2 & P3 in a paralel session. i want you to reconcile and update it basesd on
the last three tasks we commited and create a propper implimentation with maped
out dependency tree, to do list along with the tracking for the implimentation. i
want the implimentation to follow a multi-agent orchestrated workflow."*

The reconciliation is done (the tracker). The implementation is in flight. Waves
A, B and C have landed. **D, E and the final gate remain.**

---

## 1. Where things stand

```
c58fbeb fix: Wave C of the P2/P3 sweep — useAsync, route-level code splitting, a11y parametrization
8f9a57a fix: Wave B of the P2/P3 sweep — spool atomicity, report retrieval, web layer, tray state
082d97a docs: mark Wave A landed in the P2/P3 tracker (29 done, 25 todo, 1 blocked)
2e2c83f fix: Wave A of the P2/P3 sweep — middleware order, hot-path queries, hygiene
979f682 docs: reconcile the P2/P3 backlog with the three landed commits
6d9815e fix: make the frozen-sidecar provenance check load-bearing in the build
```

**Working tree: clean.** `git status --short` is empty at the time of writing.

**Tally:** `46 done · 8 todo · 0 dropped · 1 blocked` of 64 tracked lines.

Remaining todo rows in the tracker (Step 6 = Wave D, Step 8 = Wave E):

| Row | Wave |
|---|---|
| Update tray docs once behaviour is real | D (was D1's gate; B11 made the behaviour real, so this is now unblocked) |
| ADR-0002 rewrite (Method 2 as shipped) | D1 |
| ADR-0005 stale vs sprint board | D2 |
| PRD + PRODUCT_BRIEF SQLCipher claims | D3 |
| README operator entry point | D4 |
| `web/README.md` + delete stray JSONs | D5 |
| Monitoring config as real YAML files | D6 |
| `test-fast` CI job | E1 |
| Staged rollout / update channel | E3 |
| `eslint` / `globals` / `whatwg-encoding` bumps | E4 |
| Runbook §4 Windows flake wording | E5 |

**E2 stays blocked** — the perf gate's `--real` mode needs a live Orthanc on the CI
runner; the receive-scaling ratio assertion is unverifiable without one. Tracked as
blocked, not forced. Do not unblock it.

---

## 2. The environment (these will bite you otherwise)

These are all lessons learned the hard way. None of them are in the repo's docs.

- **Paths.** Repo root is `/home/dev/Documents/mercurie/dicom-gateway`. The venv is
  `.venv/` at the repo root. **`python`, `pytest`, `ruff`, `mypy` and `just` are NOT
  on PATH** — invoke them as `.venv/bin/pytest`, `.venv/bin/ruff`, `.venv/bin/mypy`.
  `just` is not installed at all; read the recipes from `justfile` and run the
  commands by hand.
- **Bash cwd resets between every tool call.** Prefix `cd /home/dev/Documents/mercurie/dicom-gateway`
  (or `.../web`) every single time. This once caused 40 fake vitest failures when a
  stray `node_modules` at the repo root was picked up.
- **Frontend tooling runs from `web/`.** `cd web && ...`. Never run `npx` at the
  repo root — it resolves to a stub `tsc` there.
- **Bare `tsc --noEmit` checks NOTHING.** `web/`'s root tsconfig is references-only
  (`files: []` + project references). Green vitest plus a broken bundle is a real
  failure mode this project has hit. **Always `npx tsc -b`.**
- **Vitest must run from `web/`** or jsdom is dropped and every test fails with
  `document is not defined`.
- **The classifier times out intermittently** ("Atria-Dawn-Preview is temporarily
  unavailable (timed out)") and blocks Write/Bash, sometimes for minutes at a
  stretch. `sleep 20–45` and retry. It also got in the way of a workflow's own
  subagent review once — always re-verify a wave's work yourself after it lands.
- **Long-running gates exceed the 120s default Bash timeout.** The full pytest suite
  is ~390–440s; `cargo tauri build` is >600s. Run them in the background
  (`run_in_background: true`) or with an extended `timeout`.
- **`test_receiver_wire.py::test_25_concurrent_associations` is flaky under
  full-suite load.** It passes in isolation (3.3s) and in its own file (9/9). If it
  is the only failure in a full run, re-run it alone before treating it as real.

### The serial gate — run after each wave, never inside an agent

```bash
cd /home/dev/Documents/mercurie/dicom-gateway
.venv/bin/ruff check .
.venv/bin/mypy .
.venv/bin/pytest -m "not integration" --cov=mercure_gateway --cov-fail-under=80 -q

cd web && ./node_modules/.bin/eslint . && npx tsc -b && ./node_modules/.bin/vitest run

# back at repo root — schema drift guard:
.venv/bin/python scripts/export_openapi.py --output /tmp/api-schema-check.json
cd web && ./node_modules/.bin/openapi-typescript /tmp/api-schema-check.json -o /tmp/api-schema-check.ts \
  && diff -u src/types/api-schema.ts /tmp/api-schema-check.ts && echo "api-schema.ts is up to date"
```

Rust legs (`cd src-tauri && cargo test && cargo fmt --check`, plus `cargo tauri build`
for a wave that touches Rust) only when `src-tauri/` actually changed. Wave C
skipped them correctly — only `static/assets/` (gitignored build output) moved.

**`just gen-api` (regen) vs `just gen-api-check`:** regen only when a wave changed
backend response models — it rewrites `web/src/types/api-schema.ts` and is a
**barrier**, not a step. Wave B needed the regen (+773 lines); Wave C needed only
the check (clean). Never run the regen while frontend agents are editing `web/src/`.

### Committing

- **Commit by explicit path list.** `git add <file1> <file2> ...`, then verify
  `git status --short | grep -vE "^[AM]  "` is empty. This keeps unrelated work out
  of a wave's commit.
- **Backticks in a commit message break the shell.** Use `git commit -F <file>`.
- **The `uv.lock` trap:** `uv run` rewrites `uv.lock`, so a `uv run` anywhere in the
  commit path leaves it unstaged and pre-commit rejects the commit. Avoid `uv run`
  entirely near commits; use `.venv/bin/` directly. If pre-commit still fights you,
  `git -c core.hooksPath=/dev/null commit --no-verify` works, and re-run ruff
  afterwards to confirm clean.
- Sign every commit message with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

## 3. The methodology that is working

Keep it. Three waves have now landed on it and two real defects were caught by it.

### Shape of a wave

1. **Size the batches from recon, not assumption.** Read the actual files and line
   references before writing a batch prompt. Wave C's recon changed the honest scope
   of C2 from 7 pages to 3 — four pages had behaviour the hook could not express.
   That is a better outcome than shipping a migration that silently deleted a guard.
2. **Partition by file ownership.** Each batch owns a disjoint set of files. That is
   the real coupling; the wave DAG's "steps" are only a reading order. Put the owned
   paths in the prompt and forbid touching anything outside.
3. **Run the batches concurrently** via the `Workflow` tool (the user's explicit
   multi-agent request is the opt-in; medium size, ~15 agents). Two phases:
   - **Sweep** — the batches, each with a `RESULTS_SCHEMA` forcing structured output
     (`filesTouched`, `testsAdded`, `verification.commands/results`, `deviations`,
     `notes`).
   - **Verify** — one adversarial verifier per batch, fed the batch's own structured
     report, told to read the diff, re-run the gate itself, mutation-test the new
     tests, and report cross-batch file collisions. Default to `NEEDS_ATTENTION`
     when a claim cannot be confirmed: an unverified claim is not a pass.
4. **Triage the verify results yourself.** `collisions` and `needsAttention` first.
   Real flags get fixed in-tree before the commit — never deferred to "a follow-up".
   Wave B's B8 flag was a security mechanism that shipped unreachable from its own
   endpoint; Wave C's was a test named for a property it could not observe.

### What to put in a batch prompt

Ground every claim in real line references and tell the agent what NOT to touch and
why. The prompts that produced the best work each carried:

- **The finding**, with file:line.
- **What to implement**, including the non-obvious variants at each call site
  (Wave C's C2 prompt spelled out that one page fetches on a `page` dependency, two
  have no loading flag at all, and two are pollers that must NOT be converted).
- **The guard that must survive.** Name the specific behaviour that a naive refactor
  would delete (Wave B: the audit invariant; Wave C: ConfigView's
  `setText(prev => prev ? prev : …)` edit-guard that stops a slow load clobbering
  an operator's keystrokes).
- **A caveat to respect rather than oversell** (Wave C's C3 carried the report's own
  warning that code splitting is not a network win for the Tauri origin).
- **The tests must be able to fail.** Ask the agent to mutate its own code and watch
  a test go red. A refactor test that passes both before and after is documentation.

### Test-writing lesson worth carrying

**Assert the mechanism, not just the consequence.** Wave B produced three
intermediate test versions that all passed against the regression they were meant to
catch — because row visibility and lock-freedom are *identical* under both the
streaming and the `fetchall()` implementation on a file-backed database. The test
that works spies on the seam (`fetchone` vs `fetchall`) and asserts what actually
differs. Before trusting a test, ask: what two implementations does this distinguish?

Same shape in Wave C: jsdom cannot observe chunk latency, so "Dashboard is not behind
a Suspense boundary" is unobservable behaviourally and the test has to pin the
observable part of the decision instead.

---

## 4. Safety constraints

- **Never commit the Tauri signer private key, never log it in CI.** Standing user
  constraint, verbatim. Check what a release-related batch touches before committing.
- **`*.master-password` is a live secret.** `.gitignore:89` documents it: it is the
  0600 key to every encrypted secret in the config beside it. Deleting it locks an
  operator out of their own appliance.
- **D5's "delete stray JSONs" needs care.** Recon as of this commit — none of these
  are tracked by git; all are gitignored local runtime artifacts:
  - `mercure-gateway.json` (repo root, 1643 B) — local scratch config, gitignore:56.
  - `web/mercure-gateway.json` (1612 B) — the **running gateway's live config**,
    also gitignore:56. Not stray. Deleting it wipes a local operator config.
  - `mercure-gateway.json.master-password` (43 B) — the live secret above.
  - `mercure-gateway.json.bak-20260911-023759`, `.bak-20260913-200447`,
    `.bak-20260913-201355` — gitignore:102 (`*.bak-*`). These are the genuine
    strays.

  The original finding was written when the tree looked different; re-scope it. The
  defensible action is to delete only the `.bak-*` files, leave the live config and
  the master-password sidecar alone, and say exactly why in the commit. Note that
  `product-refinement-spec.md` and `usb-dongle-gateway-spec.md` refer to
  `mercure-gateway.json` as a *concept* (the config filename, not these files), so
  no doc link breaks.
- **Do not implement SQLCipher.** D3 corrects the docs to match ADR-0004, which
  formally declined it. The fix is to the prose, not the code.

---

## 5. Wave D — what's left, and the recon already done

All six are file-disjoint documentation work, which parallelizes cleanly. Every one
requires **reading the shipped code and verifying claims against it** — a docs batch
that copies an existing claim is how the drift started. The verifier for a docs wave
must independently confirm each claim against code, not just against the diff.

| Batch | Files | Notes |
|---|---|---|
| D1 | `docs/adr/ADR-0002-desktop-shell-architecture.md` | Describes Method 1 ("chosen for MVP", FastAPI on `:8080`) while Method 2 ships (`tauri.conf.json:10` `frontendDist`, `lib.rs:202` `WebviewUrl::App`). Also credits Tauri notification/auto-start plugins absent from `Cargo.toml:16-25`, and lists 4 tray states against Rust's 3 — **B11 made the 4th state real**, so re-verify against current `derive_state` before writing. Rewrite as superseded-by-Method-2. |
| D2 | `docs/adr/ADR-0005-report-retrieval-strategy.md` | Stale against its own sprint board. Read the ADR, find what it claims, check each claim against `reports/`. |
| D3 | `mercure-gateway-PRD.md:200,421`, `PRODUCT_BRIEF.md:35,75,139`, `main.py:711` | SQLCipher AES-256 claims that `docs/adr/ADR-0004-at-rest-encryption.md:85` formally declined. Align docs to the ADR. |
| D4 | `README.md` | 46 lines, developer-only front door; links none of the 7 guides, no ADR index, architecture diagram omits the Tauri shell (the primary distribution form). |
| D5 | `web/README.md` (new) + the `.bak-*` deletions above | Document the four load-bearing traps (build output path, `tsc -b` not `--noEmit`, vitest must run from `web/`, `just gen-api` after model changes) — they live only in the root justfile today. **See §4 before deleting anything.** |
| D6 | `monitoring/prometheus.yml` + alert rules (new dir) + `docs/guides/admin-guide.md:211-258` | The scrape config and a 6-rule alert group are inline markdown every site hand-transcribes. Ship them as files and point the doc at them. The rule set has grown since the review (`GatewayHubNotDelivering`, `GatewayAuditAnchorBroken`) — derive from the actual metrics, not from the prose. |

Wave D's gate is cheap: docs-only, so the Python/frontend suites should be
unchanged, but run ruff (D3 touches `main.py`) and the full pytest anyway. Deleting a
file D5 thought was referenced is the thing to check.

**Ordering:** D1's gate was B11, which has landed — it is unblocked. Everything in D
is width; there is no critical path inside it.

---

## 6. Wave E — what's left

| Batch | Files | Notes |
|---|---|---|
| E1 | `.github/workflows/ci.yml` | Add a `-m "not slow and not integration"` job. The `slow`/`integration` markers are genuinely used (8 decorators across 4 files); integration is already deselected but `slow` is deselected nowhere, so there is no unit-only fast loop. |
| E2 | — | **BLOCKED.** Perf gate `--real` needs a test-rig Orthanc on the CI runner. Leave blocked. |
| E3 | `src-tauri/tauri.conf.json`, `update.py`, `.github/workflows/release.yml` | Single global `latest.json` (`tauri.conf.json:36-38`, `release.yml:189`); `PublishedUpdate`/`_is_newer` (`update.py:109,147`) have no channel field to filter on. Runbook §7 is documented discipline, not enforcement. **Touches Rust — the gate needs `cargo tauri build`.** |
| E4 | `web/package.json` + lockfile | `eslint` 9.39.5 carries a deprecation notice; `globals` a major behind (15.x vs 16.x); `whatwg-encoding` also deprecated. eslint already runs in CI, so this is toolchain currency. |
| E5 | the runbook | §4 prescribes "re-run the job before treating it as a regression" for two timing flakes — the exact property that makes a red gate carry no information. |

E3 is the only one with real risk (Rust + release plumbing). Run it last and gate it
with the full build.

---

## 7. After the waves

Task #10 in the tracker: a full gate run across everything — ruff, mypy, pytest with
coverage, the whole frontend gate, `just gen-api-check`, and `cargo test` +
`cargo tauri build`. Expect the pytest suite to take ~7 minutes and the Tauri build
longer; background them.

Then update `.full-review/P2-P3-BACKLOG.md` to the final tally, which should read
`57 done · 0 todo · 0 dropped · 1 blocked` (11 remaining rows, E2 the sole blocked).

---

## 8. Pointers

- **Source of truth:** `.full-review/P2-P3-BACKLOG.md` — the wave DAG, per-step
  detail with file:line, the "Dropped as STALE" table (do not re-do those), and the
  per-wave gate-results tables appended as waves land. Keep it current: flip rows as
  they land, and if a wave honestly changed its own scope (Wave C did), correct the
  tracker line rather than leaving it aspirational.
- **Final review report:** `.full-review/05-final-report.md` — the findings the whole
  sweep is built from. Its caveats are worth quoting at agents (Wave C quoted the
  code-splitting one verbatim).
- **Agent memory:** `~/.claude/projects/-home-dev/memory/MEMORY.md` — index of
  durable facts, including `gate-frontend-on-tsc-b`, `bash-cwd-reset` and
  `p2p3-backlog-reconciled`.
- **Wave scripts:** `/tmp/wave-c-script.js` (and Wave B's, if still present) are
  worked examples of the sweep+verify shape — schemas, collision detection, and how
  the "must not touch" constraints are phrased. They are in `/tmp`, so they may be
  gone; the shape is also recoverable from this document.
