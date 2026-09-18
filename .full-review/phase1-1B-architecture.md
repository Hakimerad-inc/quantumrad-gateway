# Phase 1.1B — Architecture & Structural Integrity Review

**Target:** `dicom-gateway` (FastAPI backend ~11.7 KLoC Python + React 18/TypeScript admin panel ~6.9 KLoC + Tauri shell)
**Base commit:** `aab94b5`, plus uncommitted WIP in `web/` (11 modified files) and untracked a11y scaffolding.
**Evidence base:** direct reading of the source tree, 754 passing / 5 skipped Python tests (re-run during review), dependency-graph AST analysis, OpenAPI regeneration check.

---

## 0. Verdict

The Python core is genuinely well-engineered and materially better than at the 2026-09-08 review: store-before-acknowledge is now durably real (file + directory fsync, WAL `synchronous=FULL`), UID validation guards path traversal, SQL is parameterized throughout, the audit chain is append-only at the trigger layer, and handler boundaries systematically isolate failures. The crash-safety design of the spool is sound in its core mechanism.

The problems are **integration and coherence, not craft**:

1. One entire bounded context (report retrieval, US-06) is constructed but never wired — it cannot work in production, and its own module docstring claims the opposite.
2. There are **two routing-rule engines with different syntax and different semantics**; the richer one is dead code and the production one ignores the documented rule format.
3. The ADR-0007 security invariant is enforced at boot but **not preserved at runtime**.
4. Several abstraction layers exist in two parallel forms (report transports, study projections, pagination conventions, error contracts), and the codegen pipeline that was built to type the API covers only 10 of 38 operations.

---

## 1. Data-flow assessment: receipt → spool → rules → redaction → forward → audit anchoring

| Step | Implementation | Assessment |
|---|---|---|
| **Receipt** | `receiver/_on_c_store` → `Spool.store_instance` | Sound. Returns 0xC120 on persist failure rather than acking (no silent data-loss loop). All standard storage SOP classes and all transfer syntaxes accepted. |
| **Spool write** | `store_instance` (`spool/__init__.py:310`) | Sound ordering: validate UIDs → write file + `.tags` sidecar → fsync file **and directory entries** (`_fsync_instance`, `:388`) → `upsert_study_instance` → requeue/arm timer. The durability barrier genuinely precedes the DB row that says RECEIVED. |
| **Rule evaluation** | `Spool._route_targets` (`:632`) | **Defective — see C2.** Ad-hoc `modality:` matching that ignores the documented `Tag=value` engine. |
| **Redaction** | `redact.py` | Correct but narrower than the review brief implies: `redact.py` redacts **config JSON** (destination secrets, credential blocks, hub API key, password hash) and `audit.redact_phi` strips PHI keys from audit exports. There is **no DICOM-dataset de-identification** in the pipeline — patient identifiers are stored in the spool, in `.tags` sidecars, in `studies.patient_name/mrn`, and forwarded verbatim. This is consistent with the PRD ("built-in anonymization deferred to hub", PRD §13 / refinement §211), so it is a documented deferral, not a defect — but the module name `redact.py` invites the misreading. |
| **Forward** | `Forwarder._dispatch` (`forwarder/__init__.py:192`) | Sound. Claim-by-id retry keeps per-task backoff correct; backoff is awaited on an interruptible `Event`; retries never hold a queue slot. Handler registry is keyed per-destination, which correctly avoids the two-`dicom`-destinations misdelivery. |
| **Audit anchoring** | `AuditLog.append` → `_anchor_head` | Sound design: unkeyed SHA-256 chain (honest about local-attacker limits) + external append-only head anchor **outside the spool dir** (`main._audit_anchor_path`) + optional hub-signed Ed25519 anchors. Concurrent appends cannot fork the chain because the previous-hash read is inside `BEGIN IMMEDIATE`. |

### Crash-safety of the spool + recovery design

**Sound:** the failure model is coherent — the file/DB write pair is deliberately non-atomic, and `recovery.recover` is the reconciliation mechanism that makes that safe. Unclean shutdown (no `.shutdown` marker) triggers the scan; a clean shutdown writes the marker as its last fsynced act (`hotplug._write_shutdown_marker_fsynced`) and the scan is skipped so the DICOM port binds fast. The marker is cleared at the *start* of every boot, so a crash after clearing still results in a scan next boot. `PRAGMA synchronous = FULL` (not NORMAL) is the correct choice under WAL — NORMAL skips the commit fsync and would lose an already-acknowledged transaction on power loss (`spool/db.py:203-208`).

**Gaps (detailed in M15, M16, M9):** recovery does not reconcile per-instance provenance, does not re-arm lost auto-enqueue timers, and `prune()` breaks continuity with externally anchored heads.

---

## 2. Findings

### Critical

#### C1 — The entire report-retrieval bounded context is unwired in production
**Files:** `main.py:523-526`, `reports/__init__.py:76-77, 230-231`, `reports/transport.py`

`main.py` constructs the retriever and never injects its transports:

```python
report_retriever = ReportRetriever(config.reports, database, spool, audit=audit)
report_retriever.start()
```

`ReportRetriever.retrieve` unconditionally fails because `finder`/`mover` are both `None`:

```python
if self.finder is None or self.mover is None:
    raise RuntimeError("report transports (finder/mover) not configured")
```

The poller catches that `RuntimeError` and logs it as "report skipped" (`reports/__init__.py:113-118`), so every `POST /studies/{id}/reports` and `POST /reports/{id}/refresh` leaves the report stuck at `pending` **forever** — silently, with no error surfaced to the operator. The module docstring states the opposite of reality: *"the production wiring in `main.py` supplies the real ReportFinder and ReportRetrieve implementations."*

Compounding this, the bounded context carries **three overlapping, mutually-unaware abstractions**:
- `finder`/`mover` raw callables (what the retriever actually expects),
- `ReportTransport` protocol + `transport_for_query_source()` registry in `reports/transport.py` (pre-registered with `dicom`/`dicomweb`/`fhir` at import, **never called by anything outside tests**),
- `DICOMReportTransport` adapter wrapping the two — also never constructed in production.

`ReportConfig.query_source` is `None` by default and `enabled` defaults to `false`, so this is latent rather than actively failing on every install — but enabling the feature in config produces a permanently stuck queue with no diagnostic.

**Architectural impact:** High. A headline user story (US-06) is dead code; the transport registry and adapter are speculative generalization with zero production callers; and the docstring actively misleads future maintainers.

**Recommendation:** in `main.py`, build the transport from config and inject it (the registry already exists for exactly this):

```python
if config.reports.query_source is not None:
    t = transport_for_query_source(config.reports.query_source)
    report_retriever.finder = t.find
    report_retriever.mover = t.retrieve
```
Then delete one of the two abstraction styles — either keep the callable injection and drop `transport.py`, or convert the retriever to hold a single `ReportTransport`. Add a startup test asserting `retriever.finder is not None` when `reports.enabled` and `query_source` are set.

---

#### C2 — Two divergent routing-rule engines; the documented one is dead code
**Files:** `rules.py` (whole module), `rules_tester.py`, `spool/__init__.py:632-663`

`RuleEngine` implements the documented rule syntax (`Tag=value`, `*` wildcards, case-insensitive, `high/normal/low` priority where the **highest-priority match wins**). It is used by exactly one non-test caller — `rules_tester.preview_routing` — and `preview_routing` is **not exposed by any web route** (grep for `preview_routing`/`RuleEngine` in `web/` returns nothing). So the engine is unreachable from the product.

The production routing path implements its own inline matcher inside `Spool._route_targets`:

```python
for rule in rules:
    rule_text = rule.rule.strip().lower()
    if rule_text.startswith("modality:"):
        wanted = rule_text.split(":", 1)[1].strip().upper()
        if modality == wanted:
            matched_names.update(rule.targets)   # UNION of all matches
```

This differs from `RuleEngine` in three ways: it understands only `modality:X` (any `Tag=value` rule — the documented format — is silently ignored), it **unions** all matching rules instead of taking the highest-priority one, and `rule.priority` is never consulted anywhere on this path.

**Consequence:** the config schema advertises a general rule engine, the config linter validates rule *targets*, and there is no UI to author rules — so the only way to configure routing is hand-editing JSON, where a documented-form rule like `StudyDescription=TUMOR*` is accepted by the schema, never matched, and every study silently falls through to the default route (all destinations). A future "preview routing" endpoint wired to `RuleEngine` would report a different routing decision than the system actually performs.

**Architectural impact:** High. The routing contract is ambiguous; one of the two engines must be deleted or the two must be unified.

**Recommendation:** make `Spool._route_targets` delegate to `RuleEngine` (it already returns the filtered target list; the `rule_narrowed` signal can be derived as "a rule matched and narrowed the set"). Then either expose `preview_routing` from the API or delete `rules_tester.py` as dead code. Add a test asserting a `Tag=value` rule is honored end-to-end through `enqueue`.

---

### High

#### H1 — The ADR-0007 bind-security invariant is not preserved at runtime
**Files:** `main.py:99-130`, `web/auth.py:94-96`, `web/routes.py:769-804`

`_enforce_bind_security` raises `SystemExit` **once at boot**. But `require_auth` reads the *live* config object (`auth.py:94`) and `PUT /api/config` replaces `app.state.config` with no re-validation (`routes.py:790`). A gateway that legitimately booted with `auth_enabled=true` bound to `0.0.0.0` (the documented way to serve on the network, covered by `test_non_loopback_with_auth_allowed`) can be flipped to `auth_enabled=false` via the API — converting the entire PHI/credential/start-stop surface to **unauthenticated-on-the-network without a restart**. `web/auth.py`'s entire safety argument ("the API is open — which is only safe because `main` refuses to bind non-loopback") is silently violated mid-process.

**Architectural impact:** High — a documented hard boundary becomes advisory after boot, and the failure is invisible.

**Recommendation:** re-validate inside `update_config`/`import_config` when `web_ui.host` or `auth_enabled` moves toward a less-secure combination (reject with 409 and the same refusal text), or have `require_auth` consult a startup snapshot of the auth posture for the bind decision while still using live config for the credential check.

#### H2 — `Spool.complete` / `fail` state transitions are not atomic
**Files:** `spool/__init__.py:697-752`

`complete()` performs three separate transactions — `mark_route_sent`, then `all_routes_complete` (a read), then `set_study_state` + `set_retention_delivered` — without wrapping them in one `Database.transaction()`. With `forwarding.concurrency` > 1, the two final routes of the same study can complete simultaneously: worker A commits route t1, worker B commits route t2, both then read `all_routes_complete == true`, and both set `SENT` and emit `STUDY_SENT`. The study ends in the correct terminal state, but the audit chain carries a duplicated delivery event, `retention_delivered_at` is stamped twice, and any hub consumer receives two delivery confirmations for one delivery.

The same non-atomicity exists in `fail()` (`mark_route_error` → read attempts → `set_study_state`), where the `attempts >= max_attempts` check can read a stale value under concurrent failure.

**Architectural impact:** Medium-High. The state machine's own invariant ("SENT when every route is complete") is enforced by a read-modify-write that is not isolated, and the audit chain records an event that did not happen.

**Recommendation:** wrap each transition in `self._db.transaction()` and do the "all complete?" check inside the same `BEGIN IMMEDIATE`; alternatively add a conditional `UPDATE ... WHERE (SELECT ...) ` that only one worker can win.

#### H3 — 28 of 38 API operations are untyped, defeating the codegen pipeline that was built for them
**Files:** `web/routes.py` (38 endpoints), `web/src/types/api.ts:33-35`, `scripts/export_openapi.py`

Only 10 operations declare a `response_model` (`SystemStatus`, `StudyPage`, `QueueStats`, `DiskStatus`, `EchoTarget`, …). The other 28 return free-form `dict[str, Any]` — including the highest-value resources: `GET`/`PUT /config`, `GET /studies/{id}`, `GET /pipeline`, `GET /reports/{id}/content`, `GET /console/dashboard`. Regenerating the schema confirms these degrade to `unknown` in `api-schema.ts`, so the SPA hand-writes interfaces for exactly those endpoints in the "Endpoints returning ad-hoc dicts" block of `types/api.ts`, and **nothing checks those hand-written types against the backend**. `tsc -b` therefore guards 10 endpoints while the 10 that matter most are unchecked — the precise failure mode the M8 codegen work was introduced to prevent.

**Architectural impact:** Medium-High. The contract layer is decorative for the critical resources.

**Recommendation:** add Pydantic view models to the ad-hoc endpoints (redacted-config models already exist conceptually in `redact.py`); the codegen then does its job. Gate the frontend build on a regenerated-schema diff in CI.

#### H4 — Redaction-sentinel restore can cross-wire credentials between destinations
**Files:** `web/routes.py:705-766`

`_restore_redacted_secrets` matches destinations by name and falls back to **list position** for renames (`:728-732`). If a destination is renamed *and* the list is reordered in the same edit, the positional fallback resolves to a *different* stored destination and writes that destination's secret into the renamed one — a wrong credential persisted silently, surfacing only as a delivery failure after the restart the save demands. There is also **no post-restore assertion that no `"***"` sentinel remains**: an unmatched sentinel is persisted as the literal string, destroying the credential (the exact failure the function exists to prevent).

**Architectural impact:** High in a PHI/credential-handling system — a silent credential-corruption path on the most dangerous resource in the API, with no error signal.

**Recommendation:** require a stable server-assigned identity for destinations (an `id` the SPA echoes back), or at minimum assert zero sentinels remain after restore and return 400 naming the offending field otherwise.

#### H5 — The single shared SQLite connection is the throughput ceiling
**Files:** `spool/db.py:180-211`, `spool/__init__.py:310-370`

One connection is shared by receiver, forwarder, web and report-poller threads, serialized through a single `RLock`, with `synchronous = FULL`. Each `store_instance` performs at least **two** FULL-fsync commits (`insert_instance_meta`, `upsert_study_instance`) plus the instance-file fsync, all on the receive hot path, all serialized. Under the US-01 target of 25 concurrent associations this is a structural bottleneck independent of any query cost. The perf gates (`scripts/check_perf_gates.py`) measure forwarding latency and a 5-items/s CI-safe floor — they do not measure sustained receive throughput, so the ceiling is unmeasured rather than verified.

**Architectural impact:** Medium-High. Correctness-first choices that are individually right collectively cap the headline non-functional requirement.

**Recommendation:** add a receive-throughput gate to the perf suite; then, if it fails, batch the per-instance provenance write (defer `instance_meta` to a group commit, or move it off the ack path entirely since it is derived/rebuildable data — the module already notes the `.tags` sidecar is derived), and consider a small write pool.

---

### Medium

**M1 — `audit.prune()` re-anchors the chain and breaks continuity with externally anchored heads** (`audit/__init__.py:385-459`). Deleting old events requires recomputing every surviving hash from genesis so `verify()` still passes. But the pre-prune heads already written to `audit-heads.txt` / the signed JSONL were computed over the *original* chain, and nothing can ever verify against them again. `verify_anchor_signatures` only checks that each stored signature is valid — it never checks that consecutive anchored heads form a chain — so the gap is undetectable. `PRUNE_AUDIT` records `head_before`/`head_after`, which is honest, but tamper-evidence across a retention boundary is lost. *Recommendation:* make the anchor file record (head, prev_head) pairs so a re-anchor is at least visible as a discontinuity, and document that pruning narrows the verifiable window.

**M2 — Frontend discards the backend's error detail** (`web/src/api.ts:104-107`). `getJson` throws `new Error(`${res.status} ${res.statusText}`)`, so an operator saving an invalid config sees "400 Bad Request" rather than the backend's `"Invalid config: <reason>"` — while `ConfigView`/`DestinationsView`/`ReportsView` all surface `e.message` directly. *Recommendation:* parse `{"detail": …}` in the error branch and include it.

**M3 — Three pagination conventions; `total` missing on most lists** (`routes.py:506` page/page_size + `total`; `:924`, `:1032`, `:640`, `:674` limit/offset with no total). `/audit/export` caps at 100 000 rows in one body; `/diagnostics/export` hardcodes `limit=1000` and returns `count` with no `truncated` flag, so a support bundle silently omits older audit events — the artifact used for incident triage. *Recommendation:* one convention everywhere, plus a `truncated` flag on export endpoints.

**M4 — Two error conventions** — `get_report_content` returns **HTTP 200 with an embedded `error` key** on `RenderError` (`routes.py:1016-1025`), and `get_logs` returns **200 with empty lines** when the log is unreadable (`:1165-1169`), directly contradicting the principle the same file states for `/system/disk` ("a 'Disk OK' UI state must never be shown from an unmeasurable filesystem", `:214-217`). *Recommendation:* one typed error envelope; fail with a real status code when the state is unmeasurable.

**M5 — One resource, three overlapping projections** — `GET /studies/{id}`, `/studies/{id}/detail`, `/studies/{id}/routes` (`routes.py:541, 648, 554`) all return the study with routes in subtly different shapes; `detail` recomputes `next_retry_sec` from a locally instantiated `RetryPolicy` whose backoff formula (`5 * 2**(attempt-1)`, `:661-667`) duplicates `forwarder.RetryPolicy.delay`. Any route-row shape change must be made in three places. *Recommendation:* collapse to one resource with a `?include=` projection and import the forwarder's actual policy.

**M6 — App assembly is split across two sites** — `create_app` (`web/__init__.py:148-177`) sets config/spool/config_path, then `_run_web_admin` (`main.py:382-404`) sets receiver/forwarder/report_retriever/hub_status/health_monitor/service_controller/text_log_path *after* the app exists. Every route must therefore `getattr(..., None)` with silent degradation (`routes.py:141-147`, `:402-404`, `:892-895`), and tests calling `create_app` directly get a half-initialized `app.state` where components are `None` → 503. *Recommendation:* pass a single `Components` object into `create_app` so missing state fails fast at construction.

**M7 — `DestinationHealthMonitor` holds a stale config reference** (`web/pipeline.py:49`, built at `main.py:391`). After `PUT /config` adds or renames a destination, `app.state.config` is replaced but the monitor keeps probing the old set; new destinations show no health data until restart, and the pipeline view presents the absence as "no health" rather than staleness. *Recommendation:* read the destination set per probe through an accessor, or expose a `stale_since` marker.

**M8 — No API versioning, and the one version check that exists is inconsistent** — routes mount under a hardcoded `/api` (`web/__init__.py:196-197`), no ADR addresses HTTP versioning, and `config_version` (a *config-file schema* marker) is hard-rejected on `POST /config/import` (`routes.py:857-863`) but never checked on `PUT /config`. *Recommendation:* add `/api/v1` or record the decision in an ADR; make the version check consistent.

**M9 — `PUT /config` has no concurrency control** — full-replacement semantics with no etag/precondition: two browser tabs, or a hand edit of `mercure-gateway.json` between a GET and a PUT, resolve last-writer-wins and silently clobber the other change. Combined with the sentinel restore (H4) this is a realistic silent-data-loss path on the API's most dangerous resource. *Recommendation:* `If-Match`/etag keyed to the config digest.

**M10 — Recovery leaves crash-stranded studies in RECEIVED with no routes** (`recovery.py`, `spool/__init__.py:444-493`). Auto-enqueue timers are in-memory; a crash between `upsert_study_instance` and the timer firing (or that simply kills the armed timer) leaves the study RECEIVED with zero routes. Recovery's non-terminal set is `{RECEIVING, SENDING, ERROR}` — RECEIVED is untouched, and nothing re-arms the timer, so the study stays RECEIVED **forever** unless an operator notices and clicks Enqueue. It is visible in the queue (not silent), but the recovery design does not close its own strand window. *Recommendation:* on the recovery scan, for any RECEIVED study with no routing rows, either enqueue it or re-arm the timer.

**M11 — `instance_meta` is recorded before the instance file is fsynced** (`spool/__init__.py:353-379`). `_apply_transfer_syntax` writes the provenance row inside the write phase, then `_fsync_instance` runs. A crash between the write and the fsync leaves a provenance row for bytes that never reached stable storage; nothing reconciles it (recovery reconciles only study rows), and `spool_num_bytes` can over-report. *Recommendation:* move `insert_instance_meta` after the fsync barrier.

**M12 — Import cycles and private-symbol coupling across contexts** (dependency-graph AST analysis):
- `audit/__init__.py:476` ↔ `audit/anchoring.py:35` — a **real import-time cycle** surviving only by definition ordering; the `# noqa: E402` and the "kept at the bottom to avoid a circular import" comment are an explicit acknowledgment that the ordering is load-bearing.
- `spool/__init__.py:32` imports `AuditLog` at top level while `audit/__init__.py:398` reaches into `spool.db` for the private `_AUDIT_NO_DELETE`/`_AUDIT_NO_UPDATE` trigger constants — the data layer emits domain audit events and the audit layer depends on the data layer's internals.
- `reports/__init__.py:31` imports `Database` *alongside* `Spool` (`:30`), bypassing the facade the Spool module docstring insists callers use.
- `hub_outbox` — an integration concern — is owned by `spool/db.py` (`:153-158`, five methods, `_OUTBOX_MAX_ROWS`), and its only consumer, `hub_events.py`, references it **TYPE_CHECKING-only**. Renaming a `hub_outbox` column breaks a module that never imports it — the tightest hidden coupling in the codebase.
- `main.py:404` reads `text_log._path` instead of a public property.
- `audit/anchoring.py:36` imports `b64decode_strict`/`load_ed25519_public_key` from `update.py` — crypto primitives live in the auto-updater module.

*Recommendation:* extract `anchor_head_to_file` so the bottom-of-file re-export disappears; replace the spool→audit dependency with an observer/event interface and move the trigger constants into a shared schema-constants module; extract the hub outbox into `spool/hub_outbox.py`; expose a `path` property on `TextLog`; move the crypto helpers into a `crypto.py`.

**M13 — OS device probing lives inside the config schema module** — `config/__init__.py:635-782` (`_linux_mounts`, `_windows_removable`, `is_removable_volume`, `detect_usb_mode`, `apply_usb_defaults`) is an OS HAL embedded in a module whose job is pydantic models + persistence. It imports zero domain modules (a genuinely clean dependency-inversion hub, fan-in 23, fan-out 0 at import time) — but it mixes three responsibilities across 827 lines. *Recommendation:* split into `config/models.py`, `config/persistence.py`, `config/devices.py` (or fold device probing into `hotplug.py`).

**M14 — `web/routes.py` is a 1271-line module spanning six concerns** and importing upward into two bounded contexts (`forwarder.RetryPolicy` at `:655`, `reports.render` at `:974` — L4 API → L2 service). *Recommendation:* split by concern (config/studies/pipeline/reports/audit/diagnostics) and inject the retry policy instead of importing it.

**M15 — Vendored `axe.js` (1.3 MB) ships to production from `web/public/`** — Vite copies `public/` verbatim into the build, so the accessibility engine rides along in every release artifact served to browsers; an identical copy now sits **untracked** at `src/mercure_gateway/web/static/axe.js` (the Vite `outDir`), which `.gitignore` does not cover (it ignores only `static/assets/` and `static/index.html`) — one `git add .` from being committed into the backend package. `axe-core` is already a proper `devDependency` and `a11y-scan.test.tsx` already imports it correctly, so the vendored file is pure redundancy. *Recommendation:* delete both copies; optionally gitignore `src/mercure_gateway/web/static/*` with explicit exceptions.

**M16 — Stale OpenAPI `info.version`** (`web/__init__.py:163` hardcodes `version="0.1.0"` while canonical is `1.1.0-rc3`, `src/mercure_gateway/__init__.py:4`). `test_version_sync.py` checks five sources but not `info.version`, so the published contract — which is bundled into diagnostics exports — advertises 0.1.0. `__version__` is already imported in `routes.py`; wire it and add the app to the sync source list.

**M17 — ADR-0005 has drifted from the implementation** — the ADR states DICOMweb retrieval is "deferred to Sprint 08" and that a DICOMweb transport *will* slot in without changing the status machine; `reports/dicomweb.py` and the `"dicomweb"` registry entry now exist, but the ADR was never updated and (per C1) nothing wires any of it. Additionally `reports/hl7_fhir.py` ships a transport whose `find`/`retrieve` **raise `NotImplementedError`**, gated off behind a module-level `ENABLED = False` — a dead contract placeholder in production code. *Recommendation:* update ADR-0005's status/consequences, and either finish or delete the HL7 placeholder.

---

### Low

- **L1 — `StudyState.RECEIVING` is a dead state.** Nothing ever writes it; it appears only in the enum, the state-machine docstring, recovery's non-terminal set, and the pipeline queue histogram. Either implement the "RECEIVING on first instance, RECEIVED on study-complete" refinement or remove it.
- **L2 — Process-global `_start_time`** (`routes.py:83`) feeds `/system/status` and the uptime metric; any test importing `routes` shares one clock.
- **L3 — Server path disclosure:** `get_report_content` echoes the absolute server-side `file_path` in all three branches (`routes.py:987, 1003, 1011`) — PHI-bearing responses that need none of it.
- **L4 — `service_action` returns 404 for an unknown verb** (`routes.py:449`), merging "no such action" with "no such resource"; the `except Exception → 500` forwards raw service-manager text to the client.
- **L5 — No rate limiting or lockout on `/login`** with a single shared password and a 12 h token. The token machinery itself is sound (constant-time compare, `auth.py:52`). Sessions are also irrevocable — `logout` only deletes the cookie, and a password change invalidates every session (the secret is derived from the hash, `auth.py:83-85`), which is reasonable but undocumented.
- **L6 — Silent bcrypt lockout:** `verify_password` returns `False` when a `$2b$` hash is stored but bcrypt is absent (`auth.py:68-74`) — permanent lockout with no log line distinguishing "wrong password" from "bcrypt unavailable".
- **L7 — Orphan modules:** `led.py` and `tray.py` have zero intra-package importers (Tauri/Rust-side only); `ui/__init__.py:20` imports `main` back (a lazy fourth cycle class).
- **L8 — `verify_anchor_signatures` reads the whole anchor file into memory** (`audit/anchoring.py:257`) — fine at current scale, but the file is append-only and unbounded.
- **L9 — Middleware-ordering comment is inverted** (`web/__init__.py:179-181`): Starlette's `add_middleware` wraps so the *last* added runs outermost — `CORSMiddleware` (`:185`) actually intercepts first, so for a browser POST with a disallowed `Origin`, CORS answers 400 before the CSRF 403 branch is reached. The CSRF check still covers non-CORS paths; fix the comment or the order and add a test asserting the 403 path is reachable.
- **L10 — `count_routes_by_target`** (`spool/db.py:674-688`) groups by target without an index on `task_routing(target_name)`; minor at current volumes.

---

### Frontend-specific findings (React/TypeScript panel)

**F1 — No data-fetching layer: eight components re-implement the same async boilerplate.** `QueueView.tsx:32-47`, `ReportsView.tsx:18-30`, `AuditView.tsx:12-24`, `LogsView.tsx:11-31`, `PipelineView.tsx:42-65`, `ConfigView.tsx:16-33`, `DestinationsView.tsx:199-222`, `ServiceCard.tsx:23-31` each hand-roll `useCallback(load) + useEffect(load,[load]) + loading/error state`. That duplication is what produced the inconsistent error semantics in M2. *Recommendation:* a `useApi(fn, deps)` hook collapses ~120 lines and makes the contract uniform.

**F2 — `Dashboard` lives in `App.tsx:29` instead of `pages/`.** It is a ~100-line page component sitting in the shell module; every other page is in `pages/`. The a11y test has to `import { Dashboard } from "../App"`, pulling the entire shell (and its 16 icon imports) to reach one page. *Recommendation:* move to `pages/DashboardView.tsx`.

**F3 — A duplication removable today:** `ServiceStatus` (`api.ts:162-166`) is field-for-field identical to the generated `Schemas["ServiceStatusModel"]` (`api-schema.ts:983-1000`) — replace with `export type ServiceStatus = Schemas["ServiceStatusModel"];`. Separately, `EchoTarget` exists in the schema but both echo call sites hand-build untyped bodies.

**F4 — `navigate()` at `api.ts:279-281` is dead code** (zero call sites across `web/src`) — a hash-routing helper stranded in the data-access module.

**F5 — The frontend's highest-value type mirror is unverifiable.** Because `GET /api/config` has no `response_model` (H3), the pydantic destination union is never exported, so `DestinationsView.tsx:28-100` — a 70-line mirror of every destination type's required/optional/secret field set — has no compile-time link to the backend. The comment at `DestinationsView.tsx:22-27` correctly describes the failure mode (a missing required field → 400 naming a field the form never showed) while the code structure makes that exact regression uncatchable. This is the frontend-visible half of H3.

**F6 — `ConfigView.tsx:97-101` classifies its message by English string prefix** — `msg.startsWith("Config")` decides between `ok-note` + `role="status"` and `error-banner` + `role="alert"`. One reworded backend string silently flips an error into a status announcement. *Recommendation:* track an explicit `{kind, text}` union.

**F7 — `AuthContext` value is not memoized** (`AuthContext.tsx:95`); `login`/`logout`/`checkAuth` are recreated per render, so every auth change re-renders the whole tree, and `useEffect(checkSessionOnMount, [])` is fighting the exhaustive-deps lint rule rather than memoizing.

**F8 — Pages reach around the client with raw `fetch`.** `DestinationsView.tsx:154` (`/api/echo`) and `SetupWizard.tsx:21,33` (`/api/wizard/validate`, `/api/echo` again) bypass `api.ts`, so `/api/echo` is implemented twice with divergent behavior — `DestinationsView.tsx:149-170` maps 401→`"expired"` and sends `aet_source`; `SetupWizard.tsx:32-42` does neither.

**F9 — The WIP accessibility pass is architecturally sound.** The 11 modified files are a coherent WCAG pass: skip link + `#main` landmark (`App.tsx:222-225`), AA contrast token retunes and a two-layer focus ring (`index.css`), `role="alert"`/`role="status"` on every status surface, keyboard parity for the three clickable `<tr>`s and the SVG `PipeNode` (`flow.tsx:32-51`, `PipelineView.tsx:225-239`, `ReportsView.tsx:65-79`). Notably, changing the SVG from `role="img"` to `role="group"` (`PipelineView.tsx:142-145`) is a real correctness fix, not a checkbox — `role="img"` marks children presentational and would have hidden the interactive destination nodes from assistive technology. The one defect is M15 (the vendored `axe.js`).

---

## 3. ADR conformance

| ADR | Claimed | Actual | Verdict |
|---|---|---|---|
| 0001 Receiver transport | pynetdicom SCP behind `ReceiverTransport` protocol; DCMTK as documented fallback | Implemented; contract lives in `tests/test_receiver_transport.py` as intended; all storage SOP classes + all syntaxes | **Conforms** |
| 0002 Desktop shell | Tauri + FastAPI + React over localhost HTTP (Method 1) | Implemented; `vite.config.ts` builds the SPA into the FastAPI static dir | **Conforms** |
| 0003 License / hub contract | MIT; bookkeeper contract proven against a stub, Q7 deferred | `LICENSE` MIT; `test-rig/bookkeeper` + `tests/test_hub_bookkeeper_interop.py` exist | **Conforms** (half-made, as the ADR itself states) |
| 0004 At-rest encryption | OS FDE + ACLs + key-required DB guard; SQLCipher path declined by amendment | `db_meta` HMAC verifier + `DatabaseEncryptionError` + `test_db_encryption.py`; docstrings honestly state connections are plain | **Conforms** |
| 0005 Report retrieval | C-FIND/C-MOVE SR+PDF for MVP; DICOMweb deferred to Sprint 08 | **Transports exist but are never wired (C1)**; DICOMweb transport now exists though the ADR still calls it deferred; HL7 placeholder raises `NotImplementedError` | **Does not conform** |
| 0006 Auto-update | Tauri Updater + Ed25519, opt-in, fail-closed | `tauri.conf.json` updater present with `pubkey: "REPLACE_VIA_RELEASE_CONFIG"` placeholder; `Updater` wired fail-closed in `main._check_for_updates` | **Conforms** (release-config placeholder pending, as designed) |
| 0007 Web admin transport posture | Non-loopback refusal as the **hard boundary**; optional TLS; HSTS only over HTTPS | Refusal implemented at **boot only** — defeatable at runtime (H1); TLS pair validation, conditional HSTS, and CSRF origin check all implemented | **Partially conforms** — the load-bearing claim is not preserved |

---

## 4. What to fix first

1. **C1** — wire the report transports in `main.py` (or state clearly in the ADR and the module docstrings that US-06 is deferred, and remove the dead registry).
2. **C2** — unify routing on one rule engine; delete the other.
3. **H1** — make the bind-security invariant survive `PUT /config`.
4. **H4** — stable destination identity / sentinel assertion before a credential is silently corrupted.
5. **H2, M10, M11** — close the three remaining crash-safety gaps in the state machine and recovery path.

The codebase's structural hygiene (parameterized SQL, UID validation, fsync discipline, boundary isolation, honest docstrings) is a strength that makes these fixes cheap — none of them require rework of the core spool or audit design.
