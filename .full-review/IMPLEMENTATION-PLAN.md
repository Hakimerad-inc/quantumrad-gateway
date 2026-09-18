# Implementation Plan: All P0 + P1 Review Findings

**Source:** `.full-review/05-final-report.md` (115 findings; 12 P0 + 22 P1 in scope here)
**Verified:** every claim below was re-verified against the current tree after the review.
**Tracking:** task list in this session; update task status as work lands.

## Progress

| WS | Item | Status |
|---|---|---|
| 1 | P0-12 axe.js ×2 deleted | ✅ built static dir 1.5 MB → 244 KB; a11y suite still green on `axe-core` |
| 1 | P1-16 cookie `Secure` | ✅ transport-conditional; `logout` flag matches |
| 1 | P1-21 SFTP ambient creds | ✅ `look_for_keys=False, allow_agent=False` on both branches |
| 1 | P1-10 wizard merge | ✅ receiver→`receiver`; reports merge narrowed to `enabled` |
| 5.1 | P1-5 SPA CI gates | ✅ new `web` job: eslint + `tsc -b` + vitest |
| 5.2 | P1-6 subprocess coverage | ✅ `parallel=true` + `COVERAGE_PROCESS_START`; `main.py` 33%→65% in isolation, gate 88.3% (stable across 2 runs) |

**Two deviations from the plan, both measured not assumed:**

1. **5.2 needed a timeout fix the plan didn't anticipate.** The child's port
   probe allowed 10 s; under any `sys.settrace` pydicom's module-level import
   takes ~18 s (measured: 0.76 s untraced vs 17.5 s with a *trivial* no-op
   tracer — it is pydicom's import code, not coverage's). `wait_for_port` now
   takes 60 s when `COVERAGE_PROCESS_START` is set and 10 s otherwise, so the
   no-coverage path is unchanged.
2. **`.full-review` was added to ruff's `exclude`.** The 37 pre-existing ruff
   errors are all in `.full-review/.perfbench/*` — the review's preserved
   benchmark scripts, cited from four reports as reproducibility evidence, with
   hardcoded paths, never linted and never shipped. Deleting them would break
   the report citations; the config already documents this exact precedent for
   `.claude` and `e2e`. `force-exclude` keeps it authoritative under explicit
   paths (what a hook passes).

The plan's prediction that the 80% gate **might tick down** did not
materialise: 88.30% → 88.33% across two consecutive runs. The newly-counted
composition-root lines were mostly covered ones — the dark region was
*execution*, not *untested code*.

## Corrections to the review text (carry forward)

1. The 444-context blowup is the **C-STORE SCU** (`forwarder/handlers/dicom.py:97-116`), not
   C-MOVE/C-GET. The C-MOVE SCU requests a single context.
2. The disk purge loops **do** terminate when no purgable SENT studies remain (`break` on
   `purge_oldest_delivered() → False`). They are uncapped-in-count and unthrottled, not
   unbounded-in-principle.
3. Bare `setInterval` polling is **2 files / 3 intervals** (`LogsView.tsx:29`, `PipelineView.tsx:63,87`),
   not ~6 files.
4. `just gen-api` is worse than described: `scripts/export_openapi.py:25-29` writes the schema to
   **stdout**, so the recipe cannot run on a clean checkout at all.

Two findings are more severe than the review text: the frozen sidecar at `src-tauri/binaries/`
is not merely version-stale (rc1) — it **lacks `_enforce_bind_security`** entirely (still has
`_warn_insecure`), so a locally-built installer would boot an unauthenticated admin panel on the
LAN rather than refusing.

---

## WS1 — Trivial wins (day 1)

- **1.1 P0-12** Delete both `axe.js` copies: `web/public/axe.js` and
  `src/mercure_gateway/web/static/axe.js` (both 1,305,279 B, untracked, unreferenced; the a11y
  test imports `axe-core` from node_modules). Note `static/axe.js` is NOT covered by the
  `.gitignore` `static/` rules — a stray, not an ignored build artifact.
- **1.2 P1-16** Cookie `Secure`: `web/auth.py:121-127` → `secure=request.url.scheme == "https"`.
  Follow-up (not this change): proxy case behind a `web_ui.trust_proxy_headers` flag.
- **1.3 P1-21** SFTP ambient creds: `forwarder/handlers/sftp.py:103-115`, add
  `look_for_keys=False, allow_agent=False` to **both** `connect()` branches.
- **1.4 P1-10** Wizard wrong-section merge: `web/src/pages/SetupWizard.tsx:79` →
  `current.receiver = { ...current.receiver, ...data.receiver }`; narrow the `reports` merge to
  `enabled` only (`query_source` is a string in the wizard, an object in the model). Add
  `SetupWizard.test.tsx` asserting the `general` key set. **Must land before WS2.1.**

---

## WS2 — The config contract (P0-4, P0-3, P0-8, P1-1, P1-2, P1-17, P1-9)

Root framing: *the config schema is the product's contract, but nothing enforces it.*

### 2.1 P0-4 — `extra="forbid"` + self-healing loader
- `config/__init__.py`: add `class _StrictConfigModel(BaseModel): model_config = ConfigDict(extra="forbid")`
  and re-parent all 18 models (GeneralConfig, ReceiverConfig, BaseDestination → all 8 destination
  types, ForwardingRule, ReportQuerySource, ReportConfig, HubReporting, AuditConfig, UpdateConfig,
  StorageConfig, ForwardingConfig, WebUIConfig, CredentialEntry, CredentialsConfig, USBModeConfig,
  GatewayConfig). Config does NOT propagate to nested classes, so each needs it.
- `load_config`: on a ValidationError containing **only** `extra_forbidden` errors, prune the
  offending `loc` paths from a deep copy, log them, write the original to a fixed-name
  `<config>.unknown-keys.bak` (suppress on read-only media), re-validate (real errors still raise).
  One prune-retry pass suffices; do not loop. Log-only, no audit event (DB doesn't exist yet at
  `main.py:468`).
- `PUT /api/config` stays **strict**; `POST /api/config/import` shares the loader's normalization
  via an extracted `normalize_and_validate(payload, *, source)`.
- **Do not bump `config_version`.**

### 2.2 P0-3 + P0-8 + P1-17 — the auth chain
Ground truth: **no deployed config has a bcrypt hash** (all on-disk configs/backups have
`auth_password_hash: ""`) — removing the bcrypt branch locks out nobody.

- **(a) Runtime bind re-check.** Extract `insecure_bind_reason(config, *, environ) -> str | None`
  into `config/__init__.py`; `main._enforce_bind_security` becomes a thin wrapper raising
  `SystemExit(reason)` (existing `test_web_security.py` cases call it directly with `environ={}` and
  pass unchanged). In `routes.py:update_config`, after validation and **before** `save_config`:
  emit a `CONFIG_SECURITY_REJECTED` audit event and raise **409** (the document is valid; the
  appliance's posture conflicts with it). **Not a `model_validator`** — it would fire on every
  construction/`model_copy` including `decrypt_config_from_storage`, while plain attribute mutation
  (`cfg.web_ui.host = "0.0.0.0"`) never passes through a validator at all.
- **(b) PBKDF2 replaces bcrypt.** Delete the bcrypt branch (`web/auth.py:68-74`) — its
  `except ImportError: return False` is a permanent-lockout vector since bcrypt isn't a declared
  dependency (only transitive via paramiko). Add `hash_password()` / extend `verify_password()`
  with `pbkdf2$<iters>$<salt hex>$<dk hex>` (200k iters, ~60 ms, stdlib only; bound iters to
  10k–2M to avoid self-DoS). **Keep the legacy `sha256$salt$hex` branch** — the only format in the
  wild. Three creation surfaces:
  1. **CLI rescue** — `mercure-gateway --set-web-password [--config PATH]` in `main.py` via
     `getpass`; load-modify-save the on-disk file only.
  2. **`POST /api/web-ui/password`** — `current_password` required when `auth_enabled`; rate-limited.
  3. **Wizard "Admin Password" step** — posts plaintext; SPA never holds a hash.
- **(c) Decouple the session secret.** `create_app` sets `app.state.session_secret = secrets.token_hex(32)`
  so a password change / config save doesn't invalidate every live session. Single-worker is
  documented, not solved.
- **(d) Hash validation on write** (`WebUIConfig`): `field_validator` requiring
  `pbkdf2$|sha256$|$2[aby]$` format, and `model_validator` rejecting `auth_enabled` with an empty
  hash (makes the permanent lockout unreachable). Both run **before** the 409 bind check. Update
  the `test_config_encryption.py:59` fixture that sets a bogus `"HASH"`.
- **(e) Rate limiting** — new `web/ratelimit.py`, stdlib only, per-app state on `app.state`
  (fresh bucket per TestClient). 5 failures / 60 s, escalating *expiring* lockouts (30→60→120 s,
  cap 900 s). Keyed on `request.client.host`. Honest tradeoff: behind a proxy one attacker can
  exhaust the bucket for everyone — honor `X-Forwarded-For` only behind a trust flag, document it.

### 2.3 P1-1 — cleartext secrets default
`save_config` writes all destination secrets + the admin hash in cleartext unless
`MERCURE_MASTER_PASSWORD` is set — never on a default install, on a USB appliance. **Fix:** at
first boot with no resolvable master password, generate a random one and persist via `keyring`
(already declared). Cleartext becomes explicit opt-in
(`MERCURE_GATEWAY_ALLOW_PLAINTEXT_SECRETS=1`) with startup + lint warnings. Fallback when keyring
is unavailable: 0600 file beside the config, path logged. Land **after** 2.2(c).

### 2.4 P1-2 — redaction sentinel cross-wiring
`routes.py:725-737`. Two failure modes: **rename + reorder** (the positional fallback
`current_dest_list[index]` assumes payload order matches stored order — any UI sort/reorder makes a
renamed destination inherit the *wrong* stored secret), and **duplicate names** (the by-name dict
collapses; `config/lint.py:89-120` warns but never blocks). **Fix:** drop the positional fallback;
a `***` sentinel with no name match → 400 naming the destination (re-enter rather than silently
persist a wrong credential). Add a `model_validator` rejecting duplicate destination `name`s.
The credential-entry / hub / web-UI / update restorations (`:739-764`) are unaffected.

### 2.5 P1-9 — codegen contract
Fix `just gen-api` first (add `--output` to `export_openapi.py`, preferred over relying on a
shell redirect). Then `response_model` on the config endpoints first (the other 34 operations are
a follow-up; config is the GA requirement), regenerate `api-schema.ts`, delete hand-written TS
types as the generator covers them — starting with `fetchConfig` (`web/src/api.ts:186-188`), whose
`Record<string, unknown>` return type directly enabled the wizard bug. Finish with a schema-drift
CI guard (`gen-api --check` in the `quality` job). Currently 5 of 21 exported types derive from the
schema.

---

## WS3 — Dead features & correctness (P0-6, P0-7, P0-5, P0-9, P0-11, P1-12)

### 3.1 P0-6 + P0-7 — revive US-06
`main.py:523-526` constructs `ReportRetriever` with no `finder`/`mover`, so every poll hits
`RuntimeError("report transports (finder/mover) not configured")` (`reports/__init__.py:228-231`),
swallowed to a warning (`:126-129`) and the report goes `FAILED`. The real implementations are
complete and tested (`reports/find.py:63`, `reports/move.py:58`, registry
`reports/transport.py:114` — currently imported **only by tests**). **Fix:** wire the transports
via the registry in `main.py`; the `reports/__init__.py:6-11` docstring then becomes true. Add the
E2E spec whose absence let this ship. Then correct the P0-7 docs (see WS8).

### 3.2 P0-5 — SCU presentation-context budget
`forwarder/handlers/dicom.py:97-116`. The fallback `... or list(_STORAGE_CONTEXTS)` requests all
111 classes when files are unreadable or use unknown SOP classes; × 4 syntaxes = **444 contexts**,
and pynetdicom raises `ValueError` at the 129th `add_requested_context`. The
`except ConnectionError` at `:90-95` doesn't catch it, so it escapes `deliver()` and crashes the
route — for exactly the degraded studies the fallback was written to rescue. **Fix:** (a) bound the
contexts — apply the compressed-syntax multiplication only to classes actually present, cap the
total at ~120 with deterministic priority; (b) widen the `except` to `ValueError` →
`DeliveryResult(ok=False)` so the study retries. `tests/test_sop_classes.py:41-43` only asserts
`len(STORAGE_SOP_CLASSES) <= 120` — add a test for the 4× path.

### 3.3 P0-9 — unify the routing-rule engines
`rules.py:RuleEngine` (`Tag=value` glob, priority-ranked single winner) is reachable **only** via
`rules_tester.py` and tests, and raises `RuleSyntaxError` on every deployed rule (`modality:CT` has
no `=`). The live router is `Spool._route_targets` (`spool/__init__.py:632-663`): `modality:<X>`
prefix, exact match, unions all matches, ignores priority. **Fix:** teach `_compile` the
`modality:` grammar as a first-class branch mapping to the `Modality` tag; add
`match() -> (targets, matched_any)` — `matched_any` is what `enqueue`'s "narrowed to nothing → stay
RECEIVED" guard (`spool/__init__.py:602-615`) needs and is **not** set-derivable. Semantic
decision: **union at the best matching priority** (identical to today for deployed configs;
priority override preserved for glob rules). `_route_targets` delegates to a cached `RuleEngine`
and fails **open** on `RuleSyntaxError` (route to all targets + `logger.error`) — stranding studies
is worse; the lint check and the new `POST /rules/preview` endpoint surface bad rules.
`tests/test_routing_rules.py` must pass **unchanged** as the backward-compat proof. Extend
`config/lint.py` to compile each rule so fail-open isn't silent.

### 3.4 P0-11 — bound the purge loops
`disk.py:115-123` and `_enforce_spool_cap` at `:146-152`. Both `break` when nothing purgable
remains, but neither has an iteration cap or sleep, and each iteration deletes files + a DB row
under the process-wide RLock. **Fix:** cap iterations (~100), sleep between them
(`self._stop_event.wait(0.05)`), emit a `purge_stalled` / `purge_iterations` metric when the cap
hits, and log at warning rather than silently "succeeding".

### 3.5 P1-12 — transport timeouts
`ae.associate()` with no timeout: `dicom.py:71-77`, `:162-167`, `reports/find.py:112`,
`reports/move.py:123`; paramiko `connect()` no timeout: `sftp.py:103-115`. Every other handler
passes one (`dicomweb.py:50` =60, `rsync.py:49` =300, `reports/dicomweb.py:161` =30). **Fix:** add
config-sourced timeouts (DIMSE association ~30 s; SFTP connect ~30 s) and a test that a hung peer
fails within budget instead of blocking forever.

---

## WS4 — Audit observability & provenance (P0-10, P0-2, P1-13, P1-14, P1-15)

### 4.1 P0-10 — hub delivery is unobservable
All three sub-claims verified. (a) `hub_events.py:108-111` `is_running` is thread liveness —
nothing in the delivery path flips it. (b) `queue_size` appears in **no** metric;
`mercure_gateway_hub_streaming` is set **once at boot** (`main.py:349-354`) and never mutated, so a
hub rejecting everything forever looks identical to healthy. (c) `verify_anchor_signatures`
(`audit/anchoring.py:242`) has **no production caller** — tests only; `/api/audit/verify` replays
internal chain hashes, which cannot detect a whole-chain rewrite. **Fix:** emit
`hub_outbox_depth` (from the existing `queue_size` property), `hub_delivery_failures_total`,
`hub_events_evicted_total` (pruning at `:159-162` logs only), and a `hub_delivering` gauge driven
by the delivery worker. Schedule `verify_anchor_signatures` on a **timer** — it's one fetch +
signature check, cheap, unlike `AuditLog.verify()` which the metrics route deliberately avoids for
DoS reasons (`routes.py:248-250`); anchor verification does NOT belong in the scrape path.
Reviewer's ranking: first on payoff-per-effort in the entire review.

### 4.2 P0-2 — sidecar provenance
- `scripts/package_backend.py:111-120`: emit `PROVENANCE.json` beside the bundle (version, commit,
  build host, PyInstaller version, timestamp) and **assert the frozen `__version__` matches
  canonical**, failing the packaging build on drift.
- Add the version assertion to the existing Windows smoke test — **that single assertion would have
  caught the drift.**
- Delete the stale on-disk snapshot, or add a `just refresh-sidecar` recipe and regenerate.
- CI check that `src-tauri/binaries/` is either absent or version-current.

### 4.3 P1-13 + P1-15 — release provenance & artifacts
`release.yml:187-198` `--clobber` overwrites artifact **and** `.sig` together, so a re-dispatched
run's new signature over a different artifact verifies perfectly against the same public key
compiled into every updater. **Fix:** publish `sha256sum` in the release body + out-of-band (the
printed runbook — `latest.json` deliberately omits `checksum_sha256`, per the comment at
`release.yml:132-137`); make re-dispatch non-destructive (versioned asset names or fail-on-exists);
generate and attach an SBOM for the Python sidecar. Add a changelog and a migration guide (none exist).

### 4.4 P1-14 — rollback path
`Updater.rollback()` (`update.py:302-305`) has **zero production callers** — only
`tests/test_updater.py`. The Tauri updater is forward-only; the runbook has no "a bad update
shipped" section; `Restart=always` doesn't help when the binary is *new and wrong*. **Fix:**
document the real procedure (reinstall the previous signed installer; spool data and config
survive) and **delete the dead `rollback()`** — dead rollback code operators may assume works is
worse than none. A real CLI rollback for the headless deployment, if wanted, is a separate item.

---

## WS5 — CI & verification layer (P1-5, P1-6, P1-7, P1-8)

The *deploy* half of the pipeline is mature; the *verify* half has the holes.

### 5.1 P1-5 — missing frontend gates
`.github/workflows/ci.yml` (9 jobs): **no vitest, no eslint**. `npm run build` (= `tsc -b && vite build`)
runs in 4 jobs, so the SPA *is* type-checked in CI — that's the correction to the review's Phase 3
framing; the gaps are vitest (65 tests incl. the a11y scan, no signal) and eslint (local/CI lint
drift unbounded). **Fix:** add a `web` job running `tsc -b`, `eslint src`, `vitest run` from `web/`.
(Use `bash -c 'cd web && ...'` — cwd resets between Bash calls; npx from the repo root creates a
stray `node_modules`.)

### 5.2 P1-6 — composition-root coverage
`pyproject.toml:90-101` gates `fail_under = 80` while `main.py` reports ~58.8% — subprocess coverage
is never captured, so the gate cannot see the composition root where bind-security, hub reporting,
the disk monitor, and report anchoring live. **The mechanism is smaller than expected:** the venv's
`a1_coverage.pth` (pytest-cov) already calls `coverage.process_startup()` in every interpreter at
startup — `sitecustomize.py` is absent precisely because the `.pth` hook replaces it. Only the env
var and parallel mode are missing.
- `pyproject.toml`: add `parallel = true` to `[tool.coverage.run]` (children write
  `.coverage.<host>.<pid>.<rand>`; pytest-cov's `Central.finish()` combines them, and
  `Central.start()`'s `erase()` clears stale files from crashed runs). Deliberately **not**
  `concurrency = ["multiprocessing"]` — the children are plain `subprocess.Popen`.
- `tests/conftest.py`: set `COVERAGE_PROCESS_START` in `pytest_configure` (not a fixture — must be
  live before any child spawns), gated on `--cov` and using an absolute `config.rootpath` path.
- `.gitignore`: add `.coverage.*`.
- Comment in `tests/test_main.py` that `env=`/`cwd=` overrides on the spawns silently break coverage.
- **Prediction to flag honestly:** this adds previously-uncounted executable lines to the
  denominator (`main.py`'s `__main__` block, the uvicorn path, shutdown `finally`) *and* their hits.
  The net effect on the 80% gate is genuinely uncertain and **can tick down**. If it does, cover
  those branches — do not revert the fix.

### 5.3 P1-7 — throughput gate measures a fake
`scripts/check_perf_gates.py:27-41` — `_StopwatchHandler.deliver` increments a counter and returns
`DeliveryResult(ok=True)` with **no I/O**, against a 5 items/s floor. **Fix:** a real-handler mode
behind the existing `slow`/`integration` markers (declared but unused — 2 uses of `slow`, zero of
`integration`, so every push currently pays the full matrix). Real mode writes instances to a temp
spool and delivers through an actual socket; keep the fast synthetic check as the CI default and
run the real mode nightly / on the integration path. Context: measured on real ext4, 25
associations yield 0.69 inst/s each at 1338 ms median — 25× offered load buys 1.24× throughput.

### 5.4 P1-8 — OpenAPI version
`web/__init__.py:161-165` hardcodes `version="0.1.0"` while the product ships 1.1.0-rc3
(`src/mercure_gateway/__init__.py:4`); `sync_version.py` drift-tests five mirrors and misses the
sixth — the one a consumer sees, and the codegen input. **Fix:** `version=__version__` in
`create_app`; add the FastAPI app as a sixth `_KINDS` entry.

---

## WS6 — Performance (P1-4, P1-18, P1-19)

### 6.1 P1-18 — merge the two fsync commits
Per received instance, `Spool.store_instance` commits **two** separate `BEGIN IMMEDIATE` +
`synchronous=FULL` transactions — `insert_instance_meta` (from `_apply_transfer_syntax`) and
`upsert_study_instance` — with the file fsync barrier between them. Measured: merged is 10.1 ms vs
16.9 ms, ~26% of the receive critical section, **zero durability loss**. **Fix:** add
`Database.store_received_instance(...)` — one transaction that computes `new_series` **inside** the
`BEGIN IMMEDIATE` (this *closes* the num_series race rather than preserving it — `has_series` takes
`_lock` today so a second association storing the same study blocks; keeping the check inside the
write lock preserves that), inserts instance meta, and upserts the study with its
`num_series`/`num_instances` deltas. `_apply_transfer_syntax` becomes a pure function returning
`(received_syntax, stored_syntax, num_bytes)`. **Honest behavior change:** provenance stops being
best-effort — today an `insert_instance_meta` failure is caught-and-logged and the receive succeeds;
merged, a meta failure rolls back the study upsert and `_on_c_store` returns `0xC120` so the
modality retries. Justified: the realistic failures are DB-level (disk full, corrupt file) that
would fail the upsert anyway, and "study row with no provenance" is worse than a retry; orphaned
files are reconciled by `recovery.py`. `insert_instance_meta` stays public
(`test_disk_monitor.py:167`, `test_schema_migration.py:193`).

### 6.2 P1-19 — read path off the write connection
One connection + one process-wide `RLock` (`db.py:190`) serializes receiver, forwarder, web, and
report poller — the mechanism behind the 1.46× scaling factor, and a US-10 isolation-invariant
violation. Largest single performance lever; scheduled last for that reason. **Fix:** keep `_conn`
+ `_lock` for writes (`BEGIN IMMEDIATE` requires it; `:memory:` test dbs can't be shared), and add a
lazily-created **per-thread read-only connection** (`mode=ro` URI; WAL readers see the last
committed snapshot without the write lock). Route the pure-SELECT list methods there
(`list_studies`, `count_states`, `list_audit_events`, `list_reports`, `spool_num_bytes`, the hub
pending-event readers, …). **Carve-outs that stay on `_conn`:** reads feeding write decisions
(`has_series` — now inside the merged transaction; the `num_series` counter), reads that must see
uncommitted state, lazy generators (`iter_audit_events` — cursor interleave), and everything inside
`transaction()`. `:memory:` returns `_conn` unchanged so `mem_database()` tests are unaffected.
**Risk to verify on Linux AND the Windows CI matrix before shipping:** `mode=ro` against a WAL
database can fail outright on some configurations; the fallback returns `_conn` with a warning
rather than raising. If flaky, open a normal connection and rely on discipline (every routed method
is a hand-written SELECT).

---

## WS7 — Frontend (P1-20, P1-11)

### 7.1 P1-20 — fetch races and poller stacking
`web/src/api.ts:94-99` `apiFetch` has no `AbortController` support (zero matches for
`AbortController`/`signal:` in `web/src/`) — no request is cancellable: a slow page-1 response can
overwrite a fresh page-2 render, and in-flight fetches complete after unmount. **Minimal fix
(required):** thread `signal` through `apiFetch` → `getJson`/`postJson`; pass an `AbortController`
from the 2 polling files (`LogsView.tsx:29`, `PipelineView.tsx:63,87`); convert bare `setInterval`
to self-rescheduling `setTimeout` so polls can't overlap; gate on `document.hidden`. **Optional,
separate decision:** TanStack Query deletes ~120–150 lines of duplicated boilerplate, but the
correctness fix is `AbortController` alone — do not adopt Query *for* the race.

### 7.2 P1-11 — CSP `frame-src`
`web/__init__.py:61-78` — `frame-src` absent, `object-src 'none'` blocks fallback, so the PDF
rendering path (a `data:` iframe against `default-src 'self'`) likely renders blank. **Fix:** add
`frame-src 'self' data:`. Extend `tests/test_web_security.py:29-42`, which asserts header
**presence only** — no test anywhere asserts CSP **content** (zero matches for `frame-src`/
`object-src` in `tests/`), so the gap and the `style-src 'unsafe-inline'` relaxation are unenforced.

---

## WS8 — Documentation (P0-7, P1-22)

- **P0-7** (after 3.1): correct the US-06 claims in `reports/__init__.py:6-11`,
  `docs/adr/ADR-0005`, `docs/guides/user-guide.md:29`, `docs/guides/admin-guide.md:36`,
  `docs/qa/security-review-package.md:16`, and the sprint-05/sprint-08 ✅ rows.
- **P1-22:** six places document an auth-enabling path that does not exist —
  `docs/guides/admin-guide.md:58` ("set a password hash via the Setup wizard"),
  `docs/guides/secrets-and-env-overrides.md:60-63`, `docs/sprints/sprint-06.md:14,33`,
  `docs/sprints/sprint-09.md:25`, and `config/__init__.py:403`'s field description. Rewrite against
  the PBKDF2 scheme and the new CLI / endpoint / wizard surfaces. `web/wizard.py` contains no
  password handling today.

---

## Sequencing & Dependencies

```
WS1 (trivial wins)          ← independent; day 1
  └─ 1.4 wizard merge BEFORE 2.1 extra="forbid"
WS2 (config contract)       ← 2.1 → 2.2 → 2.3; 2.5 independent
  └─ 2.2(d) hash validators BEFORE 2.2(a) 409 check (else the wrong error surfaces)
WS3 (dead features)         ← 3.1 before 3.5; 3.3 independent
WS4 (audit/provenance)      ← 4.2 benefits from 5.4's version mirror
WS5 (CI)                    ← 5.1 + 5.2 should land EARLY: the safety net for everything else
WS6 (performance)           ← 6.1 (merge) BEFORE 6.2 (read replica)
WS7 (frontend)              ← 7.1 after 2.5 if adopting generated types
WS8 (docs)                  ← last; must describe what shipped
```

**Recommended order:** WS1 → 5.1 + 5.2 (install the safety net) → WS3 (3.1 is a precondition for
P0-1/P0-7 meaning anything) → WS2 → 5.4 + 4.2 → 4.1 + 4.3 + 4.4 → WS5 remainder → 6.1 → WS7 →
6.2 → WS8.

**Explicitly out of scope** (the review's "what to not do"): the vite 5→8 / vitest 2→5 major bump
(dev-only, advisory data was inconsistent — re-verify first); TanStack Query for the race;
route-level code splitting for the Tauri origin; re-litigating the CSRF origin check (refuted by a
live test — the defect is the comment, not the control); treating 85.84% coverage as evidence the
composition root is tested.

---

## Verification Plan

Per workstream, the proving test (all are additions to the existing 754-test suite):

| Item | Proof |
|---|---|
| 1.1 | `find . -name axe.js` → nothing; build output shrinks 6.1×; a11y vitest still passes |
| 1.2 | TestClient (HTTP) → no `Secure`; TLS config → `Secure` present |
| 1.3 | Unit test asserting `look_for_keys=False` reaches `connect` |
| 1.4 + 2.1 | Stray-key config boots, is cleaned, `.bak` written, key named in log; `PUT` unknown key → 400 naming `general.ae_title`; real validation errors still raise |
| 2.2 | `PUT` non-loopback + auth off → 409, config not persisted, `app.state.config` unchanged, `CONFIG_SECURITY_REJECTED` audit row; PBKDF2 round-trip; legacy `sha256` still verifies; garbage hash → 400; `auth_enabled` + empty hash → 400; N wrong passwords → 429 + `Retry-After` |
| 2.4 | Rename+reorder payload with `***` → 400 naming the destination, no secret persisted; duplicate names → 400 |
| 2.5 | `just gen-api` runs on a clean checkout; schema-drift guard catches an un-regenerated `api-schema.ts` |
| 3.1 | New E2E spec: a requested report reaches `RETRIEVED` end-to-end through the real transports |
| 3.2 | Degraded-study path (unknown SOP class) requests ≤120 contexts and returns `DeliveryResult(ok=False)` — retries, doesn't crash |
| 3.3 | `tests/test_routing_rules.py` passes **unchanged**; new tests for modality parse, case-insensitivity, union at best priority, empty `modality:` → `RuleSyntaxError` |
| 3.4 | Purge loop under sustained load: iterations bounded, `purge_stalled` metric emitted, CPU flat |
| 3.5 | Hung peer fails within the configured timeout instead of blocking forever |
| 4.1 | Hub metrics correct with a bookkeeper returning 401 forever — `hub_delivering` 0, failures rising, depth > 0; timer-run anchor verification detects a rewritten chain |
| 4.2 | Packaging build fails on a version-mismatched frozen sidecar; Windows smoke test asserts reported version == tag |
| 4.3 | Re-dispatched release run does not overwrite existing assets; release body has sha256s; SBOM attached |
| 5.1 | CI `web` job red on a failing vitest test and on an eslint violation |
| 5.2 | `uv run pytest --cov=mercure_gateway tests/test_main.py` shows `main.py`'s `__main__` block covered; number stable across two runs |
| 5.3 | Real-handler mode performs actual socket + disk I/O (assert via a temp spool, not a counter) |
| 6.1 | One `BEGIN` per `store_instance` (trace counter); meta failure → no study row and no meta row; two associations, same study → `num_series` correct |
| 6.2 | Reader doesn't block a writer (long read in thread A; B still commits); reader sees committed state only; `mem_database()` round-trips unchanged |
| 7.1 | Unmounting mid-fetch aborts; page-2 render isn't overwritten by a slow page-1 |
| 7.2 | CSP content asserted: `frame-src 'self' data:` present |

**Full-suite gate after each workstream:** `uv run ruff check .` · `uv run mypy .` (strict, 70 files)
· `uv run pytest` · `bash -c 'cd web && npm run build && npx eslint src && npx vitest run'`.
Note 5.2 will change the coverage number — treat a *drop* as expected signal about uncovered
composition-root branches, not as a regression to revert.
