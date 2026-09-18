# Phase 4: Best Practices & Standards

**Source reports:** `phase4-4A-framework.md` (26 findings: 1 Critical / 7 High / 9 Medium / 9 Low — verified
against the installed tree, `uv.lock`/`package-lock.json`/`Cargo.lock`, `npm audit`, and live runtime
inspection of the built FastAPI app) and `phase4-4B-cicd.md` (24 findings: 2 Critical / 6 High / 11 Medium /
5 Low — verified by executing the broken `just` recipes and inspecting the frozen sidecar on disk).

**Orchestrator note:** the two reviewers converged on the same root cause from different directions and
produced one genuine correction to a claim carried forward from Phase 3. Deduplication below.

---

## Framework & Language Findings

### The single best framing of the whole review

4A identified that four findings previously reported separately are **one root choice**: *the config
schema is the product's contract, but nothing in the stack enforces it.*

1. `extra="forbid"` on the config models — makes invalid input visible instead of silently dropped.
2. `response_model` on the config endpoints — makes the contract machine-readable.
3. A working `just gen-api` + CI drift guard — makes the contract enforced.
4. Delete the hand-written TS interfaces as the generator replaces them; the SPA's `as` casts vanish.
5. `version=__version__` in `create_app` + the sync script — makes the contract versioned.

Steps 1–2 are the substantive work; 3–5 are an afternoon each and compound.

### Critical

1. **F-C1 — Pydantic `extra="ignore"` silently discards unknown keys.** This is the *framework-level
   generalization* of Phase 3's H1 (the wizard's discarded Receiver step). No model in the config tree
   sets `model_config`, so Pydantic v2's default eats `ae_title`/`port` written into the wrong section
   without a peep — the operator validates, saves, gets 200 and a success message, and the receiver keeps
   its defaults forever. **The fix removes the whole class, not just this instance:** any future mis-targeted
   key from the SPA, a typo'd import, or a fleet template error becomes a visible 400 instead of silent
   data loss. `PUT /config` already returns validation errors as 400, so no new plumbing is needed.
   *Correction to Phase 3's framing:* this is not a wizard bug with a one-line fix — it is a schema-permissiveness
   bug of which the wizard is the first known victim.

### High

2. **F-H1 — The codegen pipeline built to type the API covers almost nothing: 5 of 21 exported SPA types
   derive from the schema.** Verified: `app.openapi()` reports 38 paths / 39 operations; `types/api.ts`
   derives exactly 5 types from the 62 KB generated `api-schema.ts`, leaving 16 hand-written. This is the
   same defect as Phase 3's G1 (34 of 39 operations with no `response_model`) seen from the consumer side —
   the counts agree and the fix is the backend's `response_model`, not more hand-written TS. The highest-traffic
   untyped surface is config: `fetchConfig(): Promise<Record<string, unknown>>` forces `as Destination[]`
   casts that the compiler cannot check — and a real `ReceiverConfig` type **would have surfaced the wizard
   bug at compile time**.
3. **F-H2 — 1.3 MB vendored `axe.js` ships in every production build, referenced by nothing.** Duplicate of
   Phase 2's Perf-C3, confirmed and extended: the a11y test imports `axe-core` from `node_modules`, so the
   vendored copy is pure dead weight, and Vite's `publicDir` copies it verbatim into the served static dir
   — so it lands inside every Tauri installer. It also exists **twice** (source + built copy). Both reviewers
   independently flagged it; delete both.
4. **F-H3 — 65 vitest tests, zero CI execution.** Converged on by both reviewers (4A-H3, 4B-H-1) and already
   Phase 3's T-11. **4B's correction to the record:** CI *does* type-check the SPA — `npm run build` is
   `tsc -b && vite build` and runs in `build-spa`, `e2e`, both packaging jobs, and `release`. The `justfile`'s
   `tsc --noEmit` trap is a local-only hazard, not a CI problem. The real gaps are vitest **and eslint**,
   which never run; `just lint` exists but has no CI equivalent, so local/CI lint drift is unbounded.
5. **F-H4 — Dev-toolchain advisories, permanently invisible to CI by policy.** Extends Phase 2's Low with
   specifics: vite 5.4.21 (high — path traversal; plus a **Windows-relevant** `launch-editor` NTLMv2
   disclosure, and this product's primary surface is a Windows desktop app where developers run `npm run dev`),
   esbuild 0.21.5 (moderate), `@vitest/mocker` via vitest 2.1.9 (moderate). All devDependencies — the correct
   assessment — but `npm audit --omit=dev` can never surface them and there is no separate dev audit.
   **Reviewer caveat, carried forward honestly:** the registry in this environment returned the full advisory
   set once and `total: 0` on two later calls, so re-verify severities before acting. The version-gap
   conclusion (vite 5→8, vitest 2→5) does not depend on that data.
6. **F-H5 — Hand-rolled fetching has a request-ordering race.** Same family as Phase 2's Perf-H13
   (poller stacking) but a distinct instance: no `AbortController`, so a slow page-1 response can overwrite
   a fresh page-2 render — the pagination controls say "Page 2" while the table shows page 1. Same shape in
   6 files. Minimal fix is threading `signal` through `apiFetch` (3 lines); the framework-level fix is
   TanStack Query, which deletes ~120 lines of duplicated boilerplate. **The AbortController fix is not
   optional; Query is.**
7. **F-H6 — No route-level code splitting: one 200 KB chunk, no `manualChunks`.** Verified: zero `React.lazy`
   in the SPA, all 9 pages eagerly imported. Honestly assessed as *mostly a non-issue* for the Tauri
   `tauri://` origin (no network to wait on) — it matters for the headless HTTP deployment, and because the
   absence of chunk boundaries is what let `axe.js` sit in the main graph (F-H2).
8. **F-H7 — OpenAPI advertises `version="0.1.0"` while the product ships 1.1.0-rc3.** Duplicate of Phase 3's
   H3, with the fix and the extension to `sync_version.py`'s `_expected()` specified. This is the sixth
   version mirror and the one an API consumer actually sees; it is also the codegen input, so generated TS
   carries a version matching nothing.

### Medium

**F-M1** — FastAPI DI bypassed: `_spool`/`_config` are plain helper functions called inside handlers rather
than declared `Depends`, so they can't be cached, documented, or typed (`request.app.state` is `Any` under
mypy --strict). **Note the correctness subtlety:** because `routes.py:790` swaps `app.state.config` at
runtime, per-request resolution is *required* — a cached dependency would serve the startup config forever.
The `Depends` form makes that guarantee explicit instead of relying on every handler remembering it.
**F-M2** — the health monitor's lifecycle lives in `main.py`, not the app; a `lifespan` context would make
`TestClient` tests start/stop it and remove the defensive `getattr` ladder (a missing state entry becomes a
startup error rather than a silent 503). **F-M3** — `json.loads(cfg.model_dump_json())` round-trips in 5
places where `model_dump(mode="json")` does it in one pass; mechanical and grep-able. **F-M4** — middleware
order inverted vs the comment, with **new runtime-verified evidence**: preflight `OPTIONS` responses carry
**no** CSP, X-Frame-Options, or Strict-Transport-Security, and the CSRF check does not run before CORS as
the comment claims. Phase 2 established the CSRF check itself works for disallowed origins (the control is
not broken); this is comment-correctness plus a low-impact header gap on preflight responses.

**F-M5** — `fetchConfig` untyped end-to-end; the frontend face of F-H1. **F-M6** — duplicate `png` (0.17 +
0.18) and `reqwest` (0.12 + 0.13) majors compiled because the manifest lags Tauri's crates; bumping both to
match Tauri dedupes the tree in an artifact distributed as an installer. **F-M7** — `import_config` is
`async def` for the right reason (`await request.form()`) then does blocking validation + a synchronous file
write on the event loop — worst offender on a USB dongle, this product's deployment target; offload the tail
via `run_in_threadpool`. Also a dead `StarletteUploadFile` import.

### Low

`AuthContext` recreates its value object and all three closures every render (`useMemo` + `useCallback`
fix); `setInterval` polling can overlap and land out of order (self-rescheduling `setTimeout` fix — same
family as F-H5); **`numpy` declared as a direct runtime dependency but never imported by project code**
(pylibjpeg-rle brings it in transitively for RLE decompression; if kept as documentation, bump the floor
from the EOL-looking `>=1.26`); Rust `log` crate declared and never used with no logger initialized (`eprintln`
goes nowhere on a Windows GUI app — either wire `tauri_plugin_log` or remove it); Rust tray state as `u8`
constants plus a **write-only** `AtomicU8` never read after the clone (expose it to the SPA via a command,
or delete the shared atomics); `queue_stats` uses string literals where a `StrEnum` exists, so a state
rename silently returns 0; Rust edition 2021 / MSRV 1.77.2 (do alongside F-M6); `eslint` 9.39.5 marked
deprecated in the lockfile and `globals` a major behind; **`just gen-api` discards the schema it then tries
to read** (duplicate of 4B-M1 / Phase 3 H6 — the entry point of the F-H1 pipeline is broken).

---

## CI/CD & DevOps Findings

### The headline

> The **deploy** half of this pipeline is unusually mature for a project this size. The **verify** half has
> the holes: the two test gates that would catch a regression in the thing that ships (frontend tests, real
> throughput) are absent or measuring a fake, the frozen sidecar that becomes the appliance has no provenance
> or version assertion, and the audit chain can silently stop reaching the hub with no metric, no alert, and
> no scheduled integrity check.

### Critical

1. **D-C1 — The frozen sidecar has no provenance, no version assertion, and the one on disk is a stale,
   less-secure rc1.** Duplicate of Phase 2's C-3 / Phase 3's C3 with the scope correction preserved
   (published CI releases re-freeze from source, so published rc2/rc3 artifacts are **not** rc1; the exposure
   is local `cargo tauri build` plus the structural absence of any gate). **4B's contribution is the fix
   shape:** the version-sync guard actively drift-tests five mirrors — the sixth, the artifact actually
   shipped to hospitals, is invisible because it sits in a gitignored directory. Three of the four
   recommendations are cheap: emit a `PROVENANCE.json` beside the bundle, assert the frozen `__version__` in
   `package_backend.py`, and have the existing Windows smoke test additionally assert the reported version
   equals the tag. **That single assertion would have caught the drift.**
2. **D-C2 — Hub audit delivery is unobservable and can silently evict regulatory evidence.** *New finding.*
   `hub_events.py:108-111` exposes `is_running` as **thread liveness, not delivery health** — so the one
   gauge that looks like it covers this, `mercure_gateway_hub_streaming`, stays `1` while events are being
   dropped. Over-capacity events are pruned with only a log line; `queue_size` exists but appears in no
   metric. **A hub returning 401 forever (rotated key, expired credential) is indistinguishable from a
   healthy hub on the scrape feed.** Compounded by `/api/audit/verify` replaying only internal chain hashes
   while `verify_anchor_signatures` — the check that would detect a whole-chain rewrite — has no production
   caller (Phase 3's T-7, now seen at full operational consequence). For an unmanned clinical box, audit
   integrity is *latent*: true only when an operator thinks to ask. The reviewer's priority ranking puts
   this **first**: cheapest fix, highest operational payoff, gauges are one function and the anchor check
   already exists as a script.

### High

3. **D-H1 — The coverage gate passes on a number that miscounts the composition root.** Duplicate of Phase 3's
   T-17 with the fix: `main.py` reports 58.8% while the ≥80% gate passes on the aggregate, because `main()`
   tests spawn subprocesses with no `COVERAGE_PROCESS_START`. **The gate that is supposed to detect "someone
   removed a wire in the composition root" cannot see the composition root** — and the composition root is
   where bind-security enforcement, hub reporting, the disk monitor and head anchoring all live.
4. **D-H2 — The throughput gate measures a handler that does no I/O.** Restated from Phase 2/3 with the
   measured numbers (25 associations → 0.69 inst/s each, 1338 ms p50; 25× load buys 1.24× throughput) and
   the operational consequence spelled out: undelivered studies buffer to the spool, and the failure mode is
   silent disk fill followed by `purge_on_disk_full` eviction of delivered studies — the appliance quietly
   degrades to a capacity it was never validated against.
5. **D-H3 — Release assets are mutable after publication and nothing immutable records what was signed.**
   *New finding, extends Phase 3's G3.* The signing is genuinely strong (Ed25519 `.sig` sidecars, custody
   record, `verify_release_sig.py` correctly handling minisign prehash) — the residual gap is *provenance*:
   a re-dispatched run `--clobber`-overwrites the artifact **and** its `.sig` together, so a new signature
   over a different artifact verifies perfectly against the same public key compiled into every installed
   updater. **The trust model degrades to "the signing key was never misused" rather than "this artifact is
   this artifact."** Fix: sha256 in the release body and out-of-band (the printed runbook), GitHub artifact
   attestations, and non-destructive re-dispatch.
6. **D-H4 — There is no rollback path for the application, and the one `rollback()` that exists is unreachable.**
   *New finding.* Data recovery is well handled; application rollback is not: the Tauri updater is forward-only,
   `Updater.rollback()` has no caller in `web/` or the service controllers, and the runbook has no
   "a bad update shipped, now what" section. `Restart=always` restarts a broken binary indefinitely — it
   does not help when the binary is *new and wrong*. Reviewer's blunt note: **dead "rollback" code that
   operators may assume works is worse than none** — expose it or delete it.
7. **D-H5 — The disk-full purge loop can spin indefinitely and pin a core.** Duplicate of Phase 2's Perf-C2 /
   Phase 3's T-9, now with the mechanism and the irony stated: the loop has no iteration cap and no sleep,
   does not terminate when delivered studies keep arriving faster than they are purged, and is silent while
   "succeeding" — so **the one component watching capacity can become the load**, with no log line.

### Medium

**D-M1** — `just gen-api` cannot run (shared with F-L9); **D-M2** — `just e2e` is broken *and verified by
execution* (`web/playwright.config.ts does not exist` — Playwright resolves `-c` relative to cwd), so the
E2E suite runs in CI and nowhere else, and the fastest local signal path is a doc that gets it right while
the recipe doesn't. **D-M3** — Playwright TS is never type-checked and `@playwright/test` is in no lockfile
(`npm install --no-save` in CI only, resolved on dev boxes from a `node_modules` *outside the repo*, which
the CI comment documents as normal). **D-M4** — monitoring config exists only as prose in the admin guide;
no `monitoring/` directory, so every site hand-transcribes alert rules and a rule correction requires
operators to notice the doc changed. **D-M5** — no changelog, no migration guide, no SBOM for the Python
sidecar (Phase 3's G2/G3; `pip-audit` scans `pyproject.toml` at build time but the artifact a hospital
installs has no attached dependency inventory; `uv.lock` is committed but never published *with* the release).
**D-M6** — the runbook is stale in ways that will bite on the GA tag (rc1 title; `packaging.md` documents
bare `cargo tauri build`, which fails on RC tags for a reason only `release.yml` knows). **D-M7** — no staged
rollout or update channel: the instant an RC tag lands, `latest.json` points at it and every update-enabled
site is offered it (bounded today only by `_check_for_updates` being opt-in and off by default). **D-M8** —
the `slow`/`integration` marker taxonomy is declared but unused (2 uses of `slow`, zero of `integration`),
so every push pays the full matrix cost and the runbook's own Windows-flake advice ("re-run the job before
treating it as a regression") means **a red Windows run carries no information — the worst property a
required gate can have**. **D-M9** — supply-chain hardening partial: actions pinned to tags not SHAs, no
dependabot, no CODEOWNERS. **D-M10** — the test rig uses `jodogne/orthanc-plugins:latest` and nothing in CI
ever stands it up, so the "external DICOM server" half of integration testing is whatever image happened to
be pulled. **D-M11** — environment parity: dev runs from source with placeholder stubs, which is precisely
how a stale snapshot came to sit in `src-tauri/binaries/` unnoticed.

### Low

No path filters (a README edit runs the full Windows packaging matrix); the known-Critical vitest advisory
is permanently invisible to CI by policy; a stale CI branch trigger (`docs/sprint-plan`); no artifact
retention set on packaging outputs; **the `justfile`'s own claim — "the commands you type here are the same
ones CI executes" — is currently untrue for two recipes.**

### Verified mature (do not re-litigate)

Signed-installer provenance with minisign prehash handled correctly; the systemd unit (`Restart=always` with
backoff, an `ExecStartPost` health probe that keeps the unit out of `running` until the API actually serves,
per-site values via EnvironmentFile, and a documented per-directive rationale for what is *not* hardened and
why); the metrics endpoint (PHI-free by construction, deliberately defensive about a missing spool dir,
deliberately excluding chain verification from the scrape path); the backup/restore drill with four explicit
pass criteria including "stop and do not resume sending if the chain does not verify"; all three dependency
audit jobs running as blocking gates with the dev-omission reasoning written inline; and the version-sync
guard, which actively drift-tests the sync logic rather than just the five files' current values.

---

## Phase 4 Convergence Notes

Three findings were independently converged on by both reviewers, which raises confidence: the vendored
`axe.js` (F-H2), the absent vitest/eslint CI gate (F-H3), and the broken `just gen-api` (F-L9/D-M1).

Phase 4 produced **one correction to the record** (4B): CI does type-check the SPA. The `tsc --noEmit` trap
the memory note warns about is real but local; it is not the CI defect. The accurate statement is that CI
runs `tsc -b` but never runs vitest or eslint.

Phase 4 also **upgraded the framing** of the wizard data-loss bug from "a one-line wrong-section error" to
the schema-permissiveness root cause (F-C1), and supplied the compile-time guard (a generated `ReceiverConfig`
type) that would have caught it. That reframing is what Phase 5's action plan should carry.
