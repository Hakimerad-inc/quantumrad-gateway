# Phase 1: Code Quality & Architecture Review

**Source reports:** `phase1-1A-code-quality.md` (38 findings: 3 Critical / 8 High / 14 Medium / 13 Low)
and `phase1-1B-architecture.md` (43 findings: 2 Critical / 5 High / 17 Medium / 10 Low + 9 frontend items).

Both reviewers re-ran the existing gates as evidence: **ruff clean, mypy --strict clean (70 files),
`tsc -b` clean, eslint clean, 65 vitest tests pass, 754 Python tests pass / 5 skipped.**
Every finding below is a quality/correctness issue against a *green* build — none are broken-build symptoms.

---

## Code Quality Findings

### Critical

1. **C-1 — Path traversal via server-controlled UIDs in DICOMweb report persistence**
   `reports/dicomweb.py:177-184` (`_save`). Output paths are composed from UIDs parsed verbatim out
   of a *remote server's* JSON response — no `validate_uid()`. A malicious/buggy DICOMweb server can
   write DICOM bytes anywhere the process can write. The receive path solved this exact threat
   (`spool/__init__.py:62-70`); the reports tree did not.
2. **C-2 — Same traversal class over a DICOM association (C-FIND/C-MOVE)**
   `reports/move.py:170-177`, fed by `find.py:126-137`. Identical defect, different transport:
   UIDs read off a PACS C-FIND response reach the filesystem unvalidated.
3. **C-3 — Boot-time bind-security invariant defeatable at runtime; auth has no enable path**
   `web/routes.py:790` × `web/auth.py:88-105` × `main.py:99-130`. `_enforce_bind_security` raises
   `SystemExit` once at boot, but `PUT /config` swaps the live config object `require_auth` reads,
   so one authenticated request downgrades a network-bound appliance to unauthenticated with no
   re-check. Compounding: `main.py:127-129` and `docs/guides/admin-guide.md:58` direct operators to
   enable auth via a wizard step that does not exist (steps are receiver/destinations/reports/summary).

### High

4. **H-1 — Forwarding requests >128 presentation contexts, making whole studies permanently undeliverable**
   `forwarder/handlers/dicom.py:105-112`. N×4 contexts; the fallback branch alone requests 444
   (111 storage classes × 4) against pynetdicom's hard 128 limit. Any study with unreadable headers
   or ≥33 SOP classes is permanently undeliverable and fails with a confusing `ValueError`.
5. **H-2 — Disk-full purge loop never terminates when a delete fails** `disk.py:115-123` with
   `spool/__init__.py:905-917`. `purge_oldest_delivered()` returns `True` even when nothing was
   deleted, so the loop re-selects the same study, fails the same `rmtree`, and spins **with no
   sleep** — pegging a CPU on an already-failing disk.
6. **H-3 — Report C-STORE SCP thread and port leaked when `associate()` raises** `reports/move.py:112-144`.
   The `try/finally` starts after `start_server`, so one transient network failure leaks the reactor
   thread and port; the next retrieval permanently fails.
7. **H-4 — State-machine writes inside `_dispatch` are unguarded** `forwarder/__init__.py:229-248`.
   `spool.complete()` sits outside the `try`; a `SQLITE_FULL` during commit leaves the route `sending`
   forever after delivery already succeeded on the wire. Nothing re-claims `sending` routes.
8. **H-5 — `postJson` discards every failure detail** `web/src/api.ts:131-135`. A 400 with a specific
   reason and a 500 both collapse to `null` → "Retry failed" with no reason. `saveConfig` in the same
   file already implements the correct pattern.
9. **H-6 — Echo probe has no error boundary** `DestinationsView.tsx:263-273`. A raw `fetch` rejection
   leaves the badge stuck on "probing" forever with an unhandled promise rejection, and the button is
   disabled so the operator cannot retry. Related: the "expired" badge references `.badge.amber`, which
   does not exist in `index.css` (the token is `--yellow`) — it renders unstyled.
10. **H-7 — Tamper-evidence is built but never automatically verified** `audit/anchoring.py:242` +
    `audit/__init__.py:385-459`. `verify_anchor_signatures` has no production caller (tests only);
    `prune()` deletes and re-anchors events without first asserting the chain was intact.
11. **H-8 — Session cookie missing the `Secure` flag** `web/auth.py:121-127`. Correct for the loopback
    Tauri case, but if an operator puts the panel behind a TLS-terminating proxy (which ADR-0007
    explicitly permits), the token travels cleartext on the first hop. Should be conditional on scheme.

### Medium (code quality)

Untrusted-UID/context-budget duplication sharing one root cause with C-1/C-2/H-1 (M-1); 16-line
handler boilerplate repeated across all four delivery handlers with an unused `spool_dir` protocol
parameter (M-2); `main()` is a 193-line cc-20 composition function whose `exit_code` is never set
non-zero, so a boot failure can exit 0 (M-3); `_restore_redacted_secrets` is a cc-20 name-matching
heuristic (M-4); tray state implemented twice in Python and Rust and already drifted — the *live*
Rust copy drops the `removable` state (M-5); recovery-created studies never enqueued and errored
routes strand after a clean restart (M-6); SFTP `connect()` has no timeout and will try the SSH
agent and `~/.ssh` keys with no credentials (M-7); C-MOVE issued once per matched instance, each
moving the whole study — 40 SR instances → 40 full-study moves (M-8); C-STORE results snapshotted
before sub-operations are guaranteed complete (M-9); **no frontend test job in CI** — 65 vitest
tests including the new a11y scan never run in CI (M-10); duplicated byte-identical 1.3 MB `axe.js`
in two trees, referenced by nothing (M-11); bcrypt is not a declared dependency, so the *default*
password path is the single-round SHA-256 fallback — brute-forceable at billions/sec (M-12);
three different contracts for the same "parse JSON detail" operation (M-13); `get_report_content`
repeats its response dict across four branches (M-14).

### Low

Dead code/parameters (hotplug `_device_path`, `service_backend` dead branch, shipped `_StubFactory`
test double, `StudyState.RECEIVING` never written, `<circle r={0}>`, write-only `setConfig` state,
`text_log._path` private access); unreachable/string-coupled frontend branches (`ConfigView`
classifies messages by English prefix `msg.startsWith("Config")`, deciding both CSS class *and*
ARIA role); `enqueue_study` dead computation (`before` is provably always 0); unknown `report_type`
silently disables SOP-class filtering and matches *everything*; unknown SOP classes silently bucketed
as `pdf`; relative `Link: rel="next"` URLs break DICOMweb pagination silently; SLA tracking in-memory
only and unbounded; `STUDY_SENT` emitted twice on concurrent completion; `instance_meta` committed
before the durability barrier; duplicate-instance counters can drift under concurrent C-STORE;
`_value` dispatch ladder; redaction-sentinel round-trip depends on field-naming heuristics;
`textlog` rotation TOCTOU.

### Verified as *not* bugs (recorded to prevent re-litigation)

`DestinationsView.typeWarnings` prefix matching is safe (trailing `.` separator); `phi_scope`
correctly applied at read time, not write time (keeps the chain hash verifiable); `insert_route`
`ON CONFLICT DO NOTHING` prevents duplicate routes; the broad `except` handler boundaries all map
to result objects and swallow nothing; `store_instance` durability ordering is genuinely sound;
retry/backoff ordering is correct; the built SPA is no longer in VCS.

---

## Architecture Findings

### Critical

1. **C1 — The entire report-retrieval bounded context is unwired in production**
   `main.py:523-526`, `reports/__init__.py:230-231`. `ReportRetriever` is constructed and started
   without `finder`/`mover` injected, so every `retrieve()` raises `RuntimeError`, which the poller
   swallows as "report skipped." US-06 reports are stuck at `pending` forever, silently. The module
   docstring states the opposite of reality. Three overlapping, mutually-unaware abstractions
   (raw callables, a `ReportTransport` registry in `transport.py` never called outside tests, and a
   `DICOMReportTransport` adapter) compound it.
2. **C2 — Two divergent routing-rule engines; the documented one is dead code** `rules.py` vs
   `spool/__init__.py:632-663`. `RuleEngine` (documented `Tag=value` syntax, priority-wins) is
   reachable only from `rules_tester.preview_routing`, which no web route exposes. The production
   path implements its own matcher understanding only `modality:X` and *unions* matches, never
   consulting `rule.priority`. Any documented-form rule is silently ignored and falls through to
   the default route.

### High

3. **H1 — ADR-0007 bind-security invariant not preserved at runtime** — the same defect as code
   quality C-3; counted once. `_enforce_bind_security` runs at boot only, while `PUT /config`
   replaces `app.state.config` unvalidated.
4. **H2 — `Spool.complete`/`fail` state transitions are not atomic** `spool/__init__.py:697-752`.
   Three separate transactions across mark/read/promote; concurrent final routes can both set `SENT`
   and emit duplicate `STUDY_SENT`, double-stamp `retention_delivered_at`, and send two delivery
   confirmations to the hub for one delivery.
5. **H3 — 28 of 38 API operations are untyped, defeating the codegen pipeline built for them**
   `web/routes.py`, `types/api.ts:33-35`. Only 10 operations declare `response_model`; the highest-value
   resources (`GET`/`PUT /config`, `GET /studies/{id}`, `/pipeline`, `/reports/{id}/content`) return
   free-form dicts, so the SPA hand-writes interfaces for exactly those and nothing checks them.
6. **H4 — Redaction-sentinel restore can cross-wire credentials between destinations**
   `web/routes.py:705-766` — the same defect as code-quality M-4, viewed architecturally. Positional
   fallback on rename+reorder writes one destination's secret into another, and there is no assertion
   that no `"***"` sentinel remains — an unmatched one is persisted as a literal string, destroying
   the credential.
7. **H5 — The single shared SQLite connection is the throughput ceiling** `spool/db.py:180-211`.
   One connection serialized through one `RLock` with `synchronous=FULL`; each `store_instance`
   performs ≥2 FULL-fsync commits plus a file fsync, all on the receive hot path. Under the US-01
   target of 25 concurrent associations this is structural, and the perf gates do not measure
   sustained receive throughput.

### Medium (architecture)

`audit.prune()` re-anchors the chain and breaks continuity with externally anchored heads, and the
gap is undetectable (M1); frontend discards the backend's error detail — same as code-quality H-5/M2
(M2); three pagination conventions with `total` missing on most lists and silent truncation on the
diagnostics export used for incident triage (M3); two error conventions — HTTP 200 with an embedded
`error` key on `RenderError` and 200-with-empty-lines on unreadable logs, contradicting the
"never show OK from an unmeasurable state" principle the same file states for `/system/disk` (M4);
one resource with three overlapping study projections and a duplicated backoff formula (M5); app
assembly split across two sites so every route must `getattr(..., None)` and tests get a
half-initialized `app.state` (M6); `DestinationHealthMonitor` holds a stale config reference after
`PUT /config` (M7); no API versioning and an inconsistent `config_version` check (M8); `PUT /config`
has no concurrency control — last-writer-wins clobbering (M9); recovery leaves crash-stranded
studies in RECEIVED with no routes — same as code-quality M-6 (M10); `instance_meta` recorded before
the instance file is fsynced (M11); import cycles and private-symbol coupling, including a real
import-time cycle in `audit/`, the audit layer reaching into `spool.db` privates, and `hub_outbox`
owned by `spool/db.py` while its only consumer references it TYPE_CHECKING-only (M12); OS device
probing embedded in the config schema module (M13); `web/routes.py` is a 1271-line module spanning
six concerns and importing upward into two bounded contexts (M14); vendored `axe.js` ships to
production from `web/public/` and an untracked twin sits in the FastAPI static dir one `git add .`
from being committed (M15); stale OpenAPI `info.version` hardcodes `0.1.0` while canonical is
`1.1.0-rc3` (M16); ADR-0005 has drifted — DICOMweb now exists but the ADR still calls it deferred,
and `hl7_fhir.py` ships a transport whose `find`/`retrieve` raise `NotImplementedError` (M17).

### Low

`StudyState.RECEIVING` is a dead state; process-global `_start_time`; server path disclosure in
`get_report_content`; `service_action` 404 merges "no such action" with "no such resource" and the
500 forwards raw service-manager text; no rate limiting on `/login` and sessions are irrevocable;
silent bcrypt lockout with no distinguishing log line; orphan modules and a lazy `ui → main` cycle;
`verify_anchor_signatures` reads the unbounded anchor file into memory; **the middleware-ordering
comment is inverted** — `CORSMiddleware` actually intercepts first, so a browser POST with a
disallowed `Origin` gets a CORS 400 before the CSRF 403 branch is reached; `count_routes_by_target`
groups without a supporting index.

### Frontend-specific

Eight components re-implement the same async boilerplate (F1); `Dashboard` lives in `App.tsx` instead
of `pages/` (F2); `ServiceStatus` is field-for-field identical to the generated schema type (F3);
`navigate()` is dead code (F4); the highest-value type mirror (`DestinationsView`'s 70-line
destination field-set mirror) is unverifiable — the frontend half of H3 (F5); `ConfigView` classifies
messages by English prefix (F6); `AuthContext` value is not memoized (F7); three call sites bypass
the API client with raw `fetch`, so `/api/echo` is implemented twice with divergent behavior (F8).

**F9 (positive):** the 11 uncommitted files are a coherent WCAG pass — skip link + `#main` landmark,
AA contrast retunes, a two-layer focus ring, `role="alert"`/`role="status"` on every status surface,
and keyboard parity for clickable `<tr>`s and the SVG `PipeNode`. Changing the SVG from `role="img"`
to `role="group"` is a real correctness fix, not a checkbox — `role="img"` marks children
presentational and would have hidden the interactive destination nodes from assistive technology.

### ADR conformance

| ADR | Verdict |
|---|---|
| 0001 Receiver transport | Conforms |
| 0002 Desktop shell | Conforms |
| 0003 License / hub contract | Conforms (half-made, as the ADR itself states) |
| 0004 At-rest encryption | Conforms |
| 0005 Report retrieval | **Does not conform** — transports exist but are never wired (C1); HL7 placeholder raises `NotImplementedError` |
| 0006 Auto-update | Conforms (release-config placeholder pending, as designed) |
| 0007 Web admin transport posture | **Partially conforms** — the load-bearing claim is not preserved at runtime (H1/C-3) |

---

## Critical Issues for Phase 2 Context

Findings the security and performance reviewers must carry forward:

**Security-critical:**
- **C-1/C-2** — two path-traversal sinks where a *remote server's* UIDs reach the filesystem
  unvalidated (`reports/dicomweb.py:177`, `reports/move.py:170`). The receive path's `validate_uid`
  boundary exists but is not shared. Assess full file-write impact and whether DICOMweb responses
  can also be read (SSRF/information disclosure) — `reports/dicomweb.py` makes outbound HTTP from
  config-supplied URLs.
- **C-3/H1** — auth can be disabled at runtime on a network-bound appliance with no re-check, and
  the documented way to enable auth points at a nonexistent wizard step. The whole `web/auth.py`
  safety argument ("only safe because `main` refuses non-loopback") is violated mid-process.
- **H4** — a silent credential-corruption path on the most dangerous resource in the API.
- **H-8** — session cookie lacks `Secure` under a TLS proxy.
- **M-12** — bcrypt not a declared dependency, so the default password path is single-round SHA-256.
- **M-7** — SFTP with no `look_for_keys=False`/`allow_agent=False` can silently use a user-level
  SSH key on the appliance as a forwarding credential.
- **L3** — absolute server-side `file_path` echoed in PHI-bearing report responses.

**Performance-critical:**
- **H-1** — the 128-presentation-context ceiling makes some studies permanently undeliverable;
  verify against pynetdicom's hard limit and measure the fallback-path failure mode.
- **H5** — single shared SQLite connection + `synchronous=FULL` + ≥2 fsync commits per received
  instance on the receive hot path, under a 25-concurrent-association target. Receive throughput
  is unmeasured by the perf gates.
- **H-2** — an unthrottled busy-loop that pegs a CPU during the exact disk-failure condition the
  monitor exists to protect against.
- **M-8** — per-instance C-MOVE each moving an entire study (N× redundant full-study moves against
  a clinical PACS).
- **M10/H2** — duplicate audit events and double delivery confirmations under concurrency.

**Crash-safety context (already assessed as sound in core, with specific gaps):**
The spool's store-before-ack design is genuinely correct — file + directory fsync, WAL
`synchronous=FULL`, UID validation on receive, append-only audit triggers, and
reconciliation-via-recovery. Do not re-litigate the core mechanism. The remaining gaps are H2
(non-atomic complete/fail transitions), M10/M-6 (stranded RECEIVED studies and unre-armed timers),
and M11/L-9 (`instance_meta` before the fsync barrier).
