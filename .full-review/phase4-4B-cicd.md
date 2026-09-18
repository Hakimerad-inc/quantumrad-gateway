# Phase 4-4B — CI/CD & Operational Practices Review

**Scope:** dicom-gateway (Python/FastAPI + React/TS + Tauri/Rust), base commit `aab94b5` plus
uncommitted WIP in `web/`.
**Surface reviewed:** `.github/workflows/{ci,release}.yml`, `justfile`, `scripts/`, `systemd/`,
`docs/dev/{packaging,release-runbook}.md`, `src-tauri/` build config, and the observability
surface (`web/routes.py` metrics/health endpoints, `textlog.py`, `hub_events.py`, `disk.py`,
`audit/anchoring.py`).

**Headline:** The *deploy* half of this pipeline is unusually mature for a project this size —
signed installers with a documented key-custody record, a supervised systemd unit with a boot
health probe, a PHI-free Prometheus endpoint, a durable hub outbox, and a backup/restore drill
with pass criteria. The *verify* half has the holes: the two test gates that would catch a
regression in the thing that ships (frontend tests, real throughput) are absent or measuring a
fake, the frozen sidecar that becomes the appliance has no provenance or version assertion at
all, and the audit chain — the device's regulatory reason for existing — can silently stop
reaching the hub with no metric, no alert, and no scheduled integrity check.

---

## Findings by severity

| Severity | Count |
|----------|-------|
| Critical | 2 |
| High | 6 |
| Medium | 11 |
| Low | 5 |
| **Total** | **24** |

---

## Critical

### C-1 — The frozen sidecar has no provenance, no version assertion, and the one on disk is a stale, less-secure rc1

**Files:**
- `src-tauri/binaries/mercure-gateway/_internal/mercure_gateway/__init__.py` — `__version__ = "1.1.0-rc1"`
- `src-tauri/binaries/mercure-gateway/_internal/mercure_gateway/main.py:91` — `_warn_insecure()` only, called at `main.py:411`; **no** `_enforce_bind_security`
- `src-tauri/tauri.conf.json:31` — `"resources": ["binaries/mercure-gateway/"]`
- `.gitignore:80` — `src-tauri/binaries/` (0 tracked files)
- `scripts/package_backend.py:111-120` — `copy_to_tauri()` writes no manifest, no hash, no version record

**Verified on disk (not inferred):** the PyInstaller snapshot currently sitting in
`src-tauri/binaries/mercure-gateway/` is build-time `1.1.0-rc1` (repo is rc3) and its `main.py`
contains the *pre-hardening* `_warn_insecure`, which logs a warning and binds anyway. The
`_enforce_bind_security` SystemExit that `web/auth.py` says its security model "rests on" does
not exist in that code. A developer or integrator running `cargo tauri build` locally bundles
*this* snapshot — an unauthenticated admin panel (PHI, credentials, receiver start/stop) that
binds to a non-loopback address instead of refusing to boot.

**Scope (established in Phase 2, restated for accuracy):** published CI releases re-freeze the
sidecar from source — `release.yml:77` runs `package_backend.py` before bundling — so published
rc2/rc3 artifacts are not rc1. The exposure is local builds plus the structural absence of any
gate that would notice.

**Operational risk:** an appliance image built outside CI can carry a version the release
runbook does not acknowledge and a security posture the docs claim is enforced. Because the
directory is gitignored, nothing in version control, CI, or the `test_version_sync.py` guard
ever looks at it. The version-sync guard covers five mirrors (`sync_version.py:100-106`) but the
sixth — the artifact actually shipped to hospitals — is invisible.

**Recommendation:**
1. Emit a manifest next to the bundle: `package_backend.py` writes
   `src-tauri/binaries/mercure-gateway/PROVENANCE.json` with `__version__`, git commit, build
   host, PyInstaller version, and a sha256 over the tree.
2. Add a build-time assertion in `package_backend.py` (and a `just build-backend` gate) that
   refuses to copy when the frozen `__version__` != canonical version — the same check
   `sync_version.py --check` makes for the other five mirrors.
3. Have the Windows packaging job's existing frozen-backend smoke test
   (`ci.yml:200-214`) additionally `GET /api/system/health` and assert the reported
   `version` equals the tag/canonical version. That single line would have caught the drift.
4. Stop committing a stale snapshot to a gitignored path that doubles as the local build input.
   Have `cargo tauri build` depend on a fresh freeze (or a `just build` recipe that runs
   `package_backend.py` first), and document that in `docs/dev/packaging.md:25-31`.

### C-2 — Hub audit delivery is unobservable and can silently evict regulatory evidence

**Files:**
- `src/mercure_gateway/hub_events.py:108-111` — `is_running` is **thread liveness**, not delivery health
- `src/mercure_gateway/hub_events.py:159-162` — `enqueue_hub_event(..., max_rows=self._outbox_cap)` prunes when over cap
- `src/mercure_gateway/web/routes.py:296-306` — metrics expose `hub_registered` / `hub_streaming` only; no outbox depth
- `src/mercure_gateway/web/routes.py:1044-1050` — `/api/audit/verify` replays internal chain hashes only; does **not** compare against the external head anchors
- `src/mercure_gateway/audit/anchoring.py:242` — `verify_anchor_signatures` has only test/drill callers
- `systemd/README.md:90-92` — the recommended integrity check is "a cron'd one-liner" that nothing actually ships

**The gap, end to end:** when the hub is unreachable the delivery worker backs off
(`hub_events.py:295-327`) and events accumulate in the durable `hub_outbox`. Once past the cap,
`feed()` evicts oldest-undelivered rows (`hub_events.py:161`, deletion at `:173-177`) with only a
log line. `HubEventStreamer.pending` / `queue_size` exists (`:102-106`) but is surfaced nowhere —
not in `/system/status`, not in `/system/metrics`. The one gauge that looks like it covers this,
`mercure_gateway_hub_streaming`, stays `1` while events are being dropped, because it reflects
whether the worker thread exists, not whether anything is arriving at the bookkeeper. A hub that
returns 401 forever (rotated key, expired credential) is indistinguishable from a healthy hub on
the scrape feed.

**Compounding it:** the tamper-evidence story has two independent parts — the chain's internal
hash linkage and the external head anchors held outside the spool (`main.py:221-229`).
`/api/audit/verify` checks only the first, and a whole-chain rewrite recomputes internal hashes
consistently. The check that would detect that — `verify_anchor_signatures` — exists as a script
(`scripts/verify_audit_anchors.py`) and is exercised in the D2 drill, but nothing schedules it in
production. For an unmanned clinical box, audit integrity is therefore *latent*: true only when
an operator thinks to ask.

**Operational risk:** silent loss of the compliance record that justifies the device, with no
alarm. On a PHI-handling appliance this is the highest-consequence failure mode that is also the
least visible.

**Recommendation:**
1. Add to `/api/system/metrics`: `mercure_gateway_hub_outbox_depth` (from
   `queue_size`/`load_pending_hub_events`), `mercure_gateway_hub_delivery_failures_total`
   (counter, incremented in the worker's failure path), and
   `mercure_gateway_hub_events_evicted_total`. Change `hub_streaming` semantics or add a
   distinct `hub_delivering` gauge so liveness and delivery health are not conflated.
2. Promote the admin-guide alert rules from prose to shipped IaC (see M-4) and add
   `GatewayHubBacklogGrowing` on the new outbox gauge.
3. Ship the integrity check instead of documenting it: a systemd timer (or a `restart`-safe
   in-process daily job) that runs chain+anchor verification and writes a
   `mercure_gateway_audit_chain_valid` gauge / ERROR textlog line. `/api/audit/verify` replays
   the whole chain per call by design, so schedule it hourly-to-daily, never as a scrape.
4. Extend that check to call `verify_anchor_signatures` (or a new `AuditLog.verify_with_anchors`)
   so the external-anchor binding is verified automatically, not only during drills.

---

## High

### H-1 — No vitest and no eslint job in CI; the `quality` job is Python-only despite its name

**Files:** `.github/workflows/ci.yml:10-21` (`quality` runs `ruff` + `mypy` only),
`.github/workflows/ci.yml:74-90` (`build-spa` runs `npm run build` only), `web/package.json:11-14`.

**Correction to the Phase 3 note:** CI *does* type-check the SPA — `npm run build` is
`tsc -b && vite build`, and that runs in `build-spa`, `e2e`, both packaging jobs, and `release`.
The `tsc --noEmit` trap the `justfile:69-74` comment warns about is not the CI problem. The real
gaps are that **11 vitest files / 65 test cases (including the new a11y scan) and `eslint` never
run in CI at all.** `just lint` runs `npm run lint` locally; CI has no equivalent.

**Operational risk:** a broken React component or an accessibility regression on a clinical UI
merges on a green pipeline. Lint drift between local pre-commit and CI is also unbounded — no
gate enforces that the two agree.

**Recommendation:** add a `web` job: `npm ci`, `npx tsc -b`, `npm run lint`, `npm run test`
(vitest run, non-watch). It needs no backend and is ~1 minute. Alternatively extend `build-spa`
to run `test` and `lint` before the build.

### H-2 — The coverage gate passes on a number that miscounts the composition root

**Files:** `pyproject.toml:90-101` (no `concurrent`, no `[tool.coverage.paths]`, no subprocess
config), `scripts/package_backend.py` / `main()` tests spawning `subprocess.Popen`.

**The issue:** tests that exercise `main()` launch subprocesses without
`COVERAGE_PROCESS_START` / `concurrent=true` / a `sitecustomize` hook, so coverage from the child
is never captured. `main.py` — the wiring point where every operational behaviour (bind-security
enforcement, hub reporting, disk monitor, head anchoring) is composed — reports 58.8% while the
≥80% gate passes on the aggregate. The signal is dead precisely where integration risk lives.

**Operational risk:** the gate that is supposed to detect "someone removed a wire in the
composition root" cannot see the composition root.

**Recommendation:** add `[tool.coverage.run] concurrent = true` plus a `sitecustomize.py` (or
`pytest-cov`'s subprocess recipe) so child processes write data files, and
`[tool.coverage.paths]` so the files collapse across runs. Verify by asserting `main.py` line
coverage in the report, not just the total.

### H-3 — The throughput gate measures a handler that does no I/O; real receive throughput is unmeasured and ungated

**Files:** `scripts/check_perf_gates.py:27-41` (`_StopwatchHandler.deliver` returns
`DeliveryResult(ok=True)` without touching a socket or disk), `:22` (5 items/s floor),
`.github/workflows/ci.yml:92-104`.

**Measured (Phase 3, restated):** 1 association = 14 inst/s @ 65 ms p50; 25 associations = 17.3
inst/s total, **0.69 inst/s each, 1338 ms p50**. 25× load buys 1.24× throughput. A single CT
modality can far exceed 0.69 inst/s. The gate sees none of this — it measures a fake handler
against a floor chosen for CI safety, not against a real destination.

**Operational risk:** this is a capacity-planning blind spot on a store-and-forward medical
device. Undelivered studies buffer to the spool; the failure mode is silent disk fill and
retention/`purge_on_disk_full` eviction of delivered studies to make room — i.e. the appliance
quietly degrades to a capacity it was never validated against.

**Recommendation:** add a `real_handler` perf mode using `folder`/DICOM-to-loopback delivery
against the test-rig Orthanc (`test-rig/docker-compose.yml`) — gated behind the `slow`/`integration`
markers and a nightly or release-triggered workflow rather than every push. At minimum, assert
per-association throughput under N concurrent associations and record the numbers as release
evidence in the runbook, so a regression between rc and GA is visible.

### H-4 — Release assets are mutable after publication and nothing immutable records what was signed

**Files:** `.github/workflows/release.yml:187-198` (`gh release upload --clobber`, re-dispatchable
via `workflow_dispatch`), `release.yml:138-186` (`latest.json` carries `signature` but no sha256),
`docs/dev/release-runbook.md:22-31` (key custody).

**The strength, stated plainly:** Ed25519 `.sig` sidecars, a documented custody record with a
key-file fingerprint, and `scripts/verify_release_sig.py` that correctly handles minisign
prehashed (blake2b-512) signatures. This is better than most. The residual gap is *provenance*,
not signing: a re-dispatched run re-builds and `--clobber`-overwrites the artifact *and* its
`.sig` together. A new signature over a different artifact verifies perfectly against the same
public key compiled into every installed updater. Nothing immutable — no hash published to a
location a compromised key could not also rewrite — pins what was originally released.

**Operational risk:** the trust model degrades to "the signing key was never misused" rather than
"this artifact is this artifact". For a medical appliance with an auto-updater, that is the
difference between signed and *verifiably attested*.

**Recommendation:**
1. Record `sha256` per asset in the release body (and in `latest.json`) *and* somewhere
   out-of-band — the custody record in the runbook is the natural place; a key compromise that
   can rewrite releases cannot rewrite a printed/mirrored runbook.
2. Adopt GitHub artifact attestations (`actions/attest-build-provenance`) so each asset carries a
   signed, workflow-bound provenance statement the key alone cannot forge.
3. Make re-dispatch non-destructive: fail if the release already has assets unless an explicit
   `--force` input is passed, and log the overwrite loudly.

### H-5 — There is no rollback path for the application, and the one `rollback()` that exists is unreachable

**Files:** `src/mercure_gateway/update.py:302` (`Updater.rollback()`), `web/routes.py` (no
route exposes it — grep finds no caller in `web/`, `service_controller.py`, or
`service_backend.py`), `src-tauri/src/lib.rs:190` (`tauri_plugin_updater` — forward-only),
`docs/dev/release-runbook.md` (no rollback section), `docs/guides/backup-restore.md` (covers
*data* restore, not version revert).

**The issue:** data recovery is well handled — the backup/restore doc with its four pass criteria
(a–d) is genuinely good. Application rollback is not: the Tauri updater can only move forward,
`Updater.rollback()` is test-only, and the runbook has no "a bad update shipped, now what"
section. The only documented revert is uninstall + reinstall a previous installer on each
affected box, discovered at incident time.

**Operational risk:** on an unmanned clinical appliance, a bad signed update takes the
store-forward path down with no scripted revert and no rehearsed procedure. `Restart=always`
(`systemd/mercure-gateway.service:37`) restarts a broken binary indefinitely — it will not help
when the binary is *new and wrong*.

**Recommendation:**
1. Add a rollback section to the release runbook: keep the previous installer + `.sig` on the box
   (or a known mirror), document the downgrade sequence, and note that the audit chain and spool
   survive a version change so a downgrade is non-destructive.
2. Expose `Updater.rollback()` (and `apply_update`) through the admin API so the existing
   machinery is operable, or delete it — dead "rollback" code that operators may assume works is
   worse than none.
3. Rehearse a downgrade in the D-series drill format (`docs/qa/d2-backup-restore-drill.md` is the
   template) so the procedure is proven before it is needed.

### H-6 — The disk-full purge loop can spin indefinitely and pin a core in production

**File:** `src/mercure_gateway/disk.py:115-123` — the inner `while pct >= self._warning_pct:`
loop re-measures only after a successful purge and breaks only when
`purge_oldest_delivered()` returns `False`.

**The issue:** the loop has no iteration cap and no `_stop_event.wait()` inside it. It does not
terminate when (a) delivered studies keep arriving faster than they are purged — a live gateway
under load can satisfy `purge_oldest_delivered()` forever while usage never drops — or (b)
deletion succeeds at the DB level but usage does not fall (files held open by an in-flight send,
overlayfs/quota timing, NFS). Meanwhile `_loop()`'s exception blanket (`disk.py:96-97`) is outside
this inner loop, so nothing catches it. Result: the disk-monitor daemon thread pegs a core and
never returns to its 30 s poll — the one component watching capacity becomes the load.

**Operational risk:** the exact failure mode this code exists to prevent (disk full on an
unmanned box) can be replaced by a self-inflicted CPU spin, with no log line indicating why,
because the loop is silent while "succeeding".

**Recommendation:** bound the loop — a maximum purge-iterations-per-pass cap plus a
`self._stop_event.wait(short)` between iterations, and log when the cap is hit ("purged N
studies; usage still X% — operator attention required"). Also surface
`purge_oldest_delivered()`'s failure to reduce usage as a metric
(`mercure_gateway_purge_stalled`) so the stall is alertable.

---

## Medium

### M-1 — `just gen-api` cannot run; the checked-in `api-schema.ts` has no working regeneration procedure

**Files:** `justfile:95-97`, `scripts/export_openapi.py:25-29` (writes to `sys.stdout`), absence
of `mercure-gateway.openapi.json` at repo root (verified — does not exist).

The recipe runs `export_openapi.py` with no redirection, then reads
`../mercure-gateway.openapi.json`, which the first line never produces. The checked-in
`web/src/types/api-schema.ts` therefore cannot be regenerated as documented, and nothing in CI
compares a freshly exported schema against it — so backend/frontend contract drift is
undetectable until a runtime 4xx in production.

**Recommendation:** fix the recipe to `uv run python scripts/export_openapi.py > mercure-gateway.openapi.json`
(matching the script's own docstring usage), then add a CI gate asserting a fresh export is
byte-identical to the committed `api-schema.ts` — same shape as the version-sync guard.

### M-2 — `just e2e` is broken; the local E2E loop is dead

**Files:** `justfile:52-55` (`cd web && ... npx playwright test -c playwright.config.ts`),
`playwright.config.ts` (repo-root-relative).

**Verified by execution:** `Error: .../web/playwright.config.ts does not exist` — Playwright
resolves `-c` relative to cwd. CI works because it runs the same command from the repo root
(`ci.yml:141`); the `just` recipe does not. So the E2E suite runs in CI and nowhere else, and the
fastest signal path for a developer is a doc (`e2e/README.md`) that also gets it right.

**Recommendation:** drop the `cd web` from the recipe (the config and specs are root-relative) or
run from root with an explicit `--config`. Note CI installs browsers with
`npx playwright install --with-deps chromium` — the recipe does that from `web/` too, which is
harmless but inconsistent.

### M-3 — Playwright TypeScript is never type-checked and `@playwright/test` is in no lockfile

**Files:** `.github/workflows/ci.yml:132` (`npm install --no-save @playwright/test`), no root
`package.json` (verified), `e2e/tsconfig.json` (`"include": ["tests/**/*.ts"]` — excludes
`playwright.config.ts` and the root `global-setup.ts` / `global-teardown.ts`).

The E2E dependency is installed ad hoc in CI only, audited by nothing (`npm audit` scopes to
`web/`), and resolved on dev boxes from a hoisted `node_modules` *outside the repo* — the CI
comment at `ci.yml:127-131` documents this dependency on a parent-dir install as normal. Three
config files and every spec are TS that no `tsc -b` project covers.

**Recommendation:** add a minimal root `package.json` declaring `@playwright/test` as a
devDependency with a committed lockfile. That gives `npm ci` at root, removes the
parent-`node_modules` dependency, puts `@playwright/test` in the audit surface, and lets a
`tsc --noEmit -p e2e/tsconfig.json` (widened to include the config and setup files) become a gate.

### M-4 — Monitoring configuration exists only as prose; nothing installs or versions it

**Files:** `docs/guides/admin-guide.md:162-187` (scrape config + four alert rules, inline in
markdown), `systemd/README.md:82-92` (points at the admin guide).

There is no `monitoring/` directory, no `prometheus.yml`, no alert-rules YAML, no Ansible/role
that deploys them. Every site hand-transcribes alert rules from a doc — which means every site
has different rules, none are reviewed, and a rule correction requires operators to notice the doc
changed. The scrape target and the metrics endpoint themselves are solid (PHI-free by
construction, defensive about a missing spool dir at `routes.py:317-355`); it is the delivery of
the alerting layer that is missing.

**Recommendation:** ship `monitoring/prometheus.yml` + `monitoring/alerts.yml` (the four rules
already written, plus the new hub-outbox and audit-integrity rules from C-2) and reference them
from the systemd README so a site copies files rather than transcribing prose. Even unorchestrated,
version-controlled-and-copied beats documented-and-handwritten.

### M-5 — No changelog, no migration guide, no SBOM for the Python sidecar

**Files:** `.github/workflows/release.yml:187-198` (uploads installers, `.sig`, `latest.json`),
`docs/dev/release-runbook.md:1` (title still "v1.1.0-rc1").

`gh release create --generate-notes` produces an auto commit list, not a changelog. There is no
CHANGELOG file (verified absent), no operator-facing "what changed / what to reconfigure" for a
site upgrading an appliance that may sit unattended for months, and no SBOM for the frozen Python
sidecar — `pip-audit` scans `pyproject.toml` at build time, but the artifact a hospital installs
has no attached dependency inventory. Rust has `Cargo.lock` committed; the Python equivalent is
`uv.lock`, which is committed but never published *with* the release.

**Recommendation:** generate a CycloneDX/SPDX SBOM from the frozen bundle in the release job and
attach it as a release asset (the `.dist-info` metadata is already present post-freeze); adopt a
Keep-a-Changelog format with a migration/behaviour-change section per release.

### M-6 — The runbook is stale in ways that will bite on the GA tag

**Files:** `docs/dev/release-runbook.md:1` (rc1 title; all examples `v1.1.0-rc1`),
`docs/dev/packaging.md:30` (documents bare `cargo tauri build`, which on an RC tag emits MSI and
fails on the pre-release identifier — the workaround lives only in `release.yml:33-37` and is not
reflected in the operator doc), runbook §1 (local pre-tag checks do not include the sidecar
version assertion from C-1 or a `just test-web` run).

**Recommendation:** version the runbook per release (or genericise the tag examples), move the
MSI/`--bundles nsis` caveat into `packaging.md` next to the documented build command, and extend
the §1 pre-tag checklist to include the checks CI does not enforce (frontend tests, frozen-sidecar
version match, schema-unchanged from M-1).

### M-7 — No staged rollout or update channel; every update-enabled appliance sees an RC the moment the tag lands

**Files:** `src-tauri/tauri.conf.json:37-39` (single global `latest.json` endpoint),
`.github/workflows/release.yml:138-186` (one manifest for all installs),
`src/mercure_gateway/main.py:271-304` (`_check_for_updates` is opt-in and off by default — this
is what keeps the risk bounded today).

The Tauri manifest is global and unversioned by audience: the instant an RC tag is pushed,
`latest.json` points at it and every site that enabled updates is offered it. There is no stable
vs. rc channel, no staged percentage, and no requirement that an artifact pass clean-VM UAT
*before* it becomes the offered version. The runbook's §3 sequence (install on a clean VM, *then*
publish `v1.1.0`) is the right discipline for GA but is not enforced for RCs.

**Recommendation:** add a `channel`/`prerelease` marker to the manifest and have the updater
filter on it (`_is_newer` at `update.py:147` is already the right hook), or publish RCs to a
separate `latest-rc.json` and keep `latest.json` for GA only. At minimum, document that pushing an
RC tag immediately offers it to every update-enabled site.

### M-8 — The `slow`/`integration` marker taxonomy is declared but unused, so there is no fast CI feedback loop

**Files:** `pyproject.toml:85-88` (both markers declared), `.github/workflows/ci.yml:60-72`
(matrix runs the *full* suite on ubuntu **and** windows), `docs/dev/release-runbook.md:107-112`
(which itself concedes the Windows leg has known timing flakes and advises "re-run the job before
treating it as a regression").

Two uses of `slow`, zero of `integration`, while the suite is overwhelmingly integration-flavoured.
Every push pays the full matrix cost, and the runbook's own flake advice ("just re-run it") means a
red Windows run carries no information — the worst property a required gate can have.

**Recommendation:** a `test-fast` job (`-m "not slow and not integration"`) on every push; the
full matrix on PRs to main and on tags. Then make the Windows flake real evidence: capture per-run
timings, and either fix or quarantine the two named tests (`test_main.py` port-readiness,
`test_receiver_wire` 25-association burst) rather than documenting them as re-runnable.

### M-9 — Supply-chain hardening is partial: actions pinned to tags, no dependabot, no CODEOWNERS

**Files:** `.github/workflows/ci.yml` and `release.yml` (all actions at `@v4`/`@v10.1.0`/`@v2`,
not commit SHAs), `rustsec/audit-check@v2` and `dtolnay/rust-toolchain@stable` (unpinned third
party), no `.github/dependabot.yml` and no `.github/CODEOWNERS` (both verified absent).

For a signed-release medical pipeline this is the one layer where tag-pinning is the norm and
SHA-pinning is the standard. The audit *jobs* exist (pip-audit `--strict`, `npm audit --omit=dev
--audit-level=high`, RUSTSEC) — this is about the CI runner itself being a tampering target.

**Recommendation:** SHA-pin actions (or add `frizbee`/a pinning pre-commit), add
`dependabot.yml` covering github-actions + npm + pip, and a CODEOWNERS so packaging/release
changes require a maintainer review.

### M-10 — The test rig is not pinned and not run in CI, so integration environment drift is invisible

**Files:** `test-rig/docker-compose.yml:13` (`jodogne/orthanc-plugins:latest`),
`test-rig/Makefile`, `e2e/seed.py` (a *different* gateway harness — seeded, isolated ports).

The integration environment uses a floating `:latest` Orthanc image and nothing in CI ever stands
it up, so the "external DICOM server" half of integration testing is whatever image happened to be
pulled. Two harnesses (test rig + e2e seed) also means two notions of "an isolated gateway", with
no shared fixture.

**Recommendation:** pin the Orthanc image to a digest, and add a nightly/PR-label-gated job that
runs `make demo` or the integration-marked suite against it. If the rig stays dev-only, say so in
`docs/dev/test-rig.md` so it is not mistaken for a CI-backed environment.

### M-11 — Environment parity: nothing reproduces the production build except the release tag — the exact thing that drifted in C-1

**Files:** `docs/dev/packaging.md:33-42` (placeholders path for dev), `e2e/seed.py`, `test-rig/`.

Config separation is a genuine strength here — `gateway.env` EnvironmentFile vs. config JSON vs.
keyring vs. master-password file (`systemd/gateway.env.example`, `docs/guides/secrets-and-env-overrides.md`)
is well layered and the docs are explicit that the unit file itself never carries site values.
The parity gap is at the *artifact* layer: dev runs `uv run` from source, e2e runs a seeded
gateway from source, and the only place the frozen-sidecar production form is exercised is the
release tag and the two CI packaging jobs (which re-freeze, so they are healthy). The local
developer path uses placeholder stubs — which is precisely how a stale snapshot came to sit in
`src-tauri/binaries/` unnoticed (C-1).

**Recommendation:** make `just build-backend` + a frozen-sidecar smoke the default local demo path
(the CI smoke at `ci.yml:200-214` is the template), so the production form is exercised on dev
boxes rather than only on tags.

---

## Low

- **L-1 — No path filters; full packaging matrix runs on doc-only pushes.** `ci.yml:3-7` triggers
  on every push to `main`, so a README edit runs `package-windows` (Windows minutes) and
  `package-linux`. A `paths-ignore` filter for `docs/**` and `*.md` cuts cost without losing signal.
- **L-2 — The known-Critical vitest advisory is permanently invisible to CI by policy.**
  `ci.yml:49-53` excludes devDependencies with `--omit=dev` — a defensible call, and the comment
  says why (vite/vitest never ship in the built SPA). The consequence is that GHSA-5xrq-8626-4rwp
  (vitest 2.1.9, CVSS 9.8) is never surfaced anywhere automated. Recommend tracking dev-tree
  advisories in a weekly non-blocking job so the version gets bumped rather than forgotten.
- **L-3 — Stale branch trigger.** `ci.yml:5-7` includes `docs/sprint-plan` as a CI branch; verify
  that is still an intended target, or remove it.
- **L-4 — Bundle artifacts have no retention set.** `ci.yml:142-148` sets 7 days for Playwright
  traces but the `package-*` jobs and `release.yml:97-107` leave the default (90 days). Fine, but
  worth a deliberate choice given these are PHI-adjacent build outputs.
- **L-5 — `justfile` docstring drift.** `justfile:96-97` and `justfile:52-55` describe recipes
  that do not work (M-1, M-2); `justfile:7` claims "the commands you type here are the same ones
  CI executes", which is currently untrue for two recipes.

---

## What is genuinely good (do not re-litigate)

- **Signed-installer provenance:** Ed25519 `.sig` sidecars, a custody record with fingerprint and
  rotation procedure (`release-runbook.md:9-31`), and `verify_release_sig.py` — which correctly
  handles the minisign prehash format rather than naive raw Ed25519 over file bytes.
- **Systemd unit:** `Restart=always` with backoff, an `ExecStartPost` health probe that keeps the
  unit out of `running` until the API actually serves, per-site values via `EnvironmentFile`, and
  security directives with a documented per-directive rationale for what is *not* hardened and why
  (`systemd/mercure-gateway.service:48-64`).
- **Metrics endpoint design:** PHI-free by construction with the reasoning in the docstring;
  deliberately defensive about a missing spool dir so the disk series cannot silently disappear
  (`routes.py:317-355`); deliberately excludes chain verification from the scrape path because it
  replays the DB per call (`routes.py:248-251`).
- **Backup/restore:** consistent-snapshot guidance honouring SQLite WAL semantics, an online
  `sqlite3 .backup` alternative, and four explicit restore pass criteria including "stop and do not
  resume sending if the chain does not verify" (`backup-restore.md:123-148`).
- **Dependency gates:** pip-audit `--strict`, `npm audit`, RUSTSEC all run as blocking CI jobs —
  with the reasoning for the dev omission written inline rather than left to inference.
- **Version-sync guard:** `test_version_sync.py` actively drift-tests the sync logic, not just the
  five files' current values.

---

## Suggested priority order

1. **C-2** — outbox/eviction metrics + scheduled anchor verification. Cheapest fix with the
   highest operational payoff; the gauges are one function, the anchor check already exists.
2. **C-1** — provenance manifest + frozen-version assertion in `package_backend.py`, and a
   `--check` in the packaging smoke. Also delete/refresh the stale rc1 snapshot on disk.
3. **H-1** — a `web` CI job (tsc / eslint / vitest). ~1 minute of CI for 65 tests that currently
   never run.
4. **H-6** — bound the purge loop before it pins a core on a real box.
5. **H-4** — sha256 + build provenance on release assets; non-destructive re-dispatch.
6. **M-4** — ship the alert rules as files.
