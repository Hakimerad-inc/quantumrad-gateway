# Phase 2: Security & Performance Review

**Source reports:** `phase2-2A-security.md` (25 findings: 3 Critical / 6 High / 7 Medium / 9 Low, with live PoCs)
and `phase2-2B-performance.md` (3 Critical / 12 High / 5 Medium / 3 Low, with real on-ext4 benchmarks).

**Verification note:** the security subagent's own safety classifier was unavailable during its run, so the
orchestrator independently re-verified every load-bearing claim below against the source at `aab94b5`. All
confirmed: the rc1/`_warn_insecure` bundle diff, the `routes.py:790` config swap + `auth.py:94` live read,
both unvalidated `_save` path compositions, the missing `bcrypt` dependency, the wizard step list, and the
unauthenticated static/docs mounts. One Phase 1 suspicion was **refuted** by the security reviewer with a live
test (see "Refuted" below).

---

## Security Findings

### Critical

1. **C-1 — Arbitrary file write from a remote DICOMweb server (CVSS 9.8)**
   `reports/dicomweb.py:177-184`. `_save` composes the output path from `match.study_uid` and
   `match.sop_instance_uid`, both parsed verbatim out of a remote server's QIDO-RS JSON response.
   PoC executed live: a reply carrying `../../../../../../tmp/poc_owned` wrote a file *outside* the reports
   directory. Target of concern: the out-of-DB audit-head anchor (`~/.local/share/mercure-gateway/audit-heads.txt`),
   whose overwrite forges or invalidates the tamper-evidence chain. Content is constrained to
   DICOM-parseable bytes, so this is an integrity/availability write rather than arbitrary code.
   **Reachability is honestly reported as latent:** `main.py:525` never injects `finder`/`mover`, so the
   poll loop fails before fetching remote bytes — but the transports are fully implemented and
   pre-registered, one integration line from live, and `query_source.type: "dicomweb"` is documented config.

2. **C-2 — Same traversal via a PACS C-FIND response (CVSS 9.8)**
   `reports/move.py:170-177`. Identical defect over DIMSE; both path components are remote-controlled, and
   the C-FIND `StudyInstanceUID` need not even be the one the gateway asked for. PoC executed live.

3. **C-3 — The shipped installer is not the audited source (CVSS 8.1)**
   `src-tauri/binaries/mercure-gateway/_internal/`. `tauri.conf.json` bundles this PyInstaller snapshot
   into every installer (`resources: ["binaries/mercure-gateway/"]`), but it is **v1.1.0-rc1** while the repo
   is rc3, and 22 of 35 modules differ. The security-critical difference: its `main.py` has only
   `_warn_insecure`, which logs and binds anyway — the `_enforce_bind_security` `SystemExit` that
   `web/auth.py` says its no-op-when-auth-off behaviour "rests on" **does not exist in what ships**.
   An operator deploying the shipped installer with `host: "0.0.0.0"` gets an unauthenticated
   PHI/credentials/start-stop API on the network. This also means source-level review describes code the
   installer does not run.

### High

4. **H-1 — Auth disableable at runtime with no re-check (CVSS 8.7)**
   `web/routes.py:790` × `web/auth.py:94`. PoC executed live: one authenticated `PUT` with
   `auth_enabled: false`, then a fresh cookieless client got `200` on `/api/studies`, `/api/config`, **and
   `POST /api/system/stop`** — and the flip persisted to `mercure-gateway.json`. No audit event is emitted
   for the auth-mode change.
5. **H-2 — Authentication cannot be enabled by any supported path (CVSS 7.5)**
   Wizard steps are exactly `["receiver","destinations","reports","summary"]` in both `web/wizard.py:20` and
   `SetupWizard.tsx:5` — no auth step — yet `main.py:127` and `admin-guide.md:58` both direct operators to
   one. There is no CLI, and **no code anywhere creates a password hash** (verified by grep for
   `hashpw`/`gensalt`/`hash_password`: zero hits). The model label still claims "Bcrypt hash… Set via the
   setup wizard." On a PHI device the practical operator flow becomes "leave auth off."
6. **H-3 — Redaction sentinel cross-wires or destroys credentials (CVSS 6.8)**
   `web/routes.py:705-766`. PoC executed live: renaming *and* reordering two SFTP destinations made the
   gateway persist the lab server's password into the PACS destination entry. A sentinel with no stored
   counterpart is saved literally as `"***"`, silently destroying that credential.
7. **H-4 — `bcrypt` undeclared; the default admin password is single-round SHA-256 (CVSS 7.5)**
   `pyproject.toml` has no `bcrypt` (verified); it is importable only as a paramiko transitive, so a plain
   `uv pip install .` silently falls through to the single-round SHA-256 fallback — brute-forceable at
   billions/sec offline. Separately, the docstring's claimed "fallback" for bcrypt hashes **fails closed**
   into a permanent lockout rather than downgrading.
8. **H-5 — Session cookie lacks `Secure` (CVSS 6.5)** `web/auth.py:121-127`. Correct for loopback Tauri, but
   replayable on the first hop under the TLS-terminating proxy deployment ADR-0007 explicitly permits. The
   token is an HMAC over only the expiry, so capture is full admin takeover.
9. **H-6 — Config secrets written cleartext to disk by default (CVSS 7.4)**
   `config/__init__.py:599-618`. `credentials.encrypted` defaults to `true` but encryption only fires when
   `MERCURE_MASTER_PASSWORD` is set, which nothing ever sets; `PUT /api/config` calls `save_config` with no
   master-password parameter. SFTP passwords, SSH **private keys**, S3 secret keys, the hub API key and the
   admin hash all land as plaintext — on a USB-dongle appliance designed to be carried around. `GET /config`
   and `/diagnostics/export` redact correctly, so this is at-rest only and easy to miss.

### Medium

**M-1 — SSRF via server-controlled pagination (CVSS 7.5):** `reports/dicomweb.py:157-161` follows whatever
URL a remote server puts in `Link: rel="next"` — PoC live: a reply pointing at `http://127.0.0.1:9/...` was
fetched. A compromised PACS can pivot the appliance into an internal-network probe, including cloud-metadata
endpoints. **M-2 — No rate limiting or lockout on `/api/login`** (30 consecutive wrong passwords, no
throttle, no audit event). **M-3 — SFTP uses ambient credentials:** no `look_for_keys=False`/`allow_agent=False`,
so the operator's personal SSH key or agent is tried *before* the configured password — PHI can be delivered
authenticated as the operator's key, with a successful-delivery audit event. **M-4 —
`auth_password_hash` is settable as an arbitrary pre-computed string**, and `""` with `auth_enabled=true`
yields a permanent admin lockout. **M-5 — `/api/echo` is an unrestricted outbound network probe** with a
service-distinguishing oracle, unauthenticated in the C-3/H-1 state. **M-6 — CSRF allow-list accepts any
loopback port** (`http://127.0.0.1:9999` passed). **M-7 — OpenAPI schema exposed unauthenticated:** `/docs`,
`/redoc`, `/openapi.json` all return 200 while `/api/system/health` correctly 401s — 30 KB enumerating every
PHI endpoint and config field.

### Low

Absolute server-side `file_path` echoed in PHI responses; sessions never rotated/revocable server-side;
dev-tooling advisories (vitest 2.1.9 GHSA-5xrq-8626-4rwp **Critical 9.8**, plus vite/esbuild — all
devDependencies, dev-machine only); the 1.3 MB `axe.js` in the served static dir; Rust transitive advisories
(all low, upstream Tauri); **CSP omits `frame-src`, so PDF report iframes likely render blank**; full study
UIDs in `title` tooltips; the inverted middleware-ordering comment; docs/labels claiming bcrypt.

### Verified clean / refuted

SQL is parameterized throughout; the C-STORE traversal is closed (`validate_uid` on receive); the audit
chain is append-only at the trigger layer and `verify()` recomputes from the previous computed hash so a
row-plus-hash rewrite cannot forge it; `phi_scope` correctly applied at read time; the auto-updater is
fail-closed in all three cases and the `REPLACE_VIA_RELEASE_CONFIG` placeholder is rejected by length; no
XSS vectors in the SPA (no `dangerouslySetInnerHTML`/`eval`); no secrets in the client; no hardcoded secrets
in the repo; keyring fails closed; `redact_config` covers all secret fields; fsync ordering is sound.

**One Phase 1 item refuted:** the suspicion that the inverted middleware comment (`web/__init__.py:179-181`)
makes the CSRF check unreachable is **wrong**. The comment is genuinely backwards — CORS *is* outermost — but
Starlette passes non-allowed origins straight through for non-preflighted requests, so the origin check still
fires. Verified live: `Origin: http://evil.example` → `403 origin not allowed`. Comment defect, not a control
failure.

---

## Performance Findings

### The measured receive-path ceiling

Benchmarked against production `Spool.store_instance` on real ext4 (`/dev/sda2`, not tmpfs):

| Concurrency | Throughput | Per association | Median latency | p95 |
|---|---|---|---|---|
| 1 association | 21.4 inst/s | 21.4 inst/s | 41 ms | 56 ms |
| 25 associations (US-01 target) | **31.3 inst/s** | **1.25 inst/s** | **718 ms** | 1 428 ms |

1→25 associations buys only **1.46× throughput** while latency grows 17.5× — a fully serialized critical
section. Per instance: 1 file fsync (~10 ms) + 2 FULL WAL commits (~8.5 ms each), all inside one process-wide
`RLock`. Demand at 25 modalities × 10 img/s = 250 inst/s against ~31 supplied → backlog grows unboundedly at
~220 inst/s. **`check_perf_gates.py` measures only forwarding latency and a 5 items/s floor against a fake
handler that does no I/O — US-01's 25-association target is asserted as a config default and never verified
under load.** `synchronous=FULL` is correct and must stay; the fix is to reduce barrier *count* and let them
overlap.

### Critical

1. **The 128-presentation-context fallback makes studies permanently undeliverable**
   `forwarder/handlers/dicom.py:105-112`. Reproduced by executing the code against pynetdicom 3.0.4: the
   fallback requests 111 classes × 4 = 444 contexts, pynetdicom hard-caps at 128 and raises `ValueError`.
   Since `sop_classes_for_files` silently skips unreadable files, the fallback fires for exactly the degraded
   studies it was written to rescue → converted to a delivery failure by the blanket `except`, retried 5×,
   permanently FAILED.
2. **The disk-full purge loop spins at 100% CPU forever when a delete fails** `disk.py:115-123` +
   `spool/__init__.py:905-917`. `purge_oldest_delivered()` returns `True` even when `_purge_study_dir` fails
   the `rmtree` and returns early without deleting the row, so the loop re-selects the same study with **no
   sleep**, on an already-failing disk, never recovering.
3. **1.3 MB test-only `axe.js` shipped and served publicly** `web/public/axe.js`. Vite copies `public/` into
   the build; FastAPI mounts it at `/` (`web/__init__.py:201`) **after** the authenticated router include, so
   it is unauthenticated. 240 KB gzip — a **6.1× increase** over the entire 66 KB app payload — for a file
   nothing loads (the a11y test imports from `node_modules`). `.gitignore` does not cover it, so one
   `git add -A` commits 1.3 MB.

### High

4. **Two FULL-fsync commits per instance** — merging into one transaction measures 10.1 ms vs 16.9 ms:
   ~26% of the receive critical section, lifting the ceiling ~31 → ~45 inst/s **with zero durability loss**.
5. **One shared connection + one `RLock`** serializes receiver, forwarder, web, and report poller — the
   mechanism behind the 1.46× scaling factor. A lock-free read-only second connection on the same WAL file
   is the recommended first step. This also violates US-10's isolation invariant at the DB layer: any slow
   admin query stalls DICOM ingestion.
6. **`/api/pipeline` materializes the entire studies table per poll** — `list_studies_with_route_counts()`
   with no `limit`, counted in Python, polled every 2 s; the `created_at` predicate is already indexed, so a
   `COUNT(*)` answers it in microseconds.
7. **`_requeue_complete_routes` issues one FULL-fsync transaction per destination** — a 3-destination
   gateway pays ~52 ms instead of 27 ms on the re-opened-study path that re-sending modalities hit constantly.
8. **`count_routes_by_target()` is an unbounded uncached GROUP BY** — 300 k rows scanned per 2 s poll under
   the shared lock; no covering index.
9. **Reports retrieve buffers the entire study's datasets in RAM** — because the C-MOVE is study-level, the
   store SCP receives the whole study and every dataset is held until the association ends.
10. **Duplicate study-level C-MOVEs** — N matched report instances trigger N identical full-study moves; a
    typical study (600 CT + 1 SR + 1 PDF) transfers ~1 202 instances instead of ~2, strictly serial.
11. **No transport timeouts** — `ae.associate()` passes no `timeout`; SFTP `connect()` has none at all
    (compare `dicomweb.py:50` `timeout=60`). With `concurrency=3`, three hung destinations halt all delivery.
12. **`spool_num_bytes()` is a full-table SUM called inside the purge loop** — K+2 full scans per purge cycle.
13. **Frontend pollers stack requests** — bare `setInterval` (does not await its callback), zero
    `AbortController`, zero `document.hidden` gating anywhere; the whole SVG diagram re-renders every 2 s
    with zero `React.memo` in the codebase; no route-level lazy loading across 9 pages.

### Medium / Low

`Spool.complete`/`fail` non-atomic across 3 transactions (double `STUDY_SENT`); unbounded pipeline
destination table re-polled every 4 s; C-FIND enumerates the whole study with no early stop; unmemoized
`AuthContext`; ~150-200 lines of duplicated fetch boilerplate blocking the poller fixes; perf gates don't
cover the receive path; single-process design makes horizontal scaling impossible (documented as a boundary,
not a bug); `_requested_at` grows for process lifetime.

### Positive confirmations

No N+1 in the study endpoints (single joined + paginated queries); the hub outbox is a bounded
`deque(maxlen=1000)` with a durable table and explicit row cap; `_enqueue_timers` cancelled on shutdown;
no unbounded in-memory growth on the receive or forwarding paths; the disk monitor is isolated on its own
thread; the forwarder's retry sleep is interruptible.

---

## Critical Issues for Phase 3 Context

Findings that affect testing and documentation requirements:

**Testing gaps:**
- **No receive-throughput gate exists** — the single most important non-functional requirement (US-01) is
  unmeasured. Benchmarks are preserved in `.full-review/.perfbench/` as a starting point.
- **No SCU-side presentation-context budget test** — there is one for the SCP side (`test_sop_class_budget`);
  the SCU side, where the 444-context failure lives, has none.
- **No test covers rename-plus-reorder in `_restore_redacted_secrets`** (H-3) — the exact case that
  cross-wires credentials.
- **No test for the traversal sinks** (C-1/C-2) — the receive path's `validate_uid` tests do not cover the
  reports tree at all.
- **No test asserts the boot-time bind invariant survives `PUT /config`** (H-1), and no test covers
  the reports wiring (Phase 1 C1) or rule-engine unification (Phase 1 C2).
- **No frontend test job in CI** — 65 vitest tests including the new a11y scan never run; the a11y stubs
  never render the interactive `<tr>` row markup, so the ARIA paths most likely to break are unscanned.
- **The audit chain is never verified in production** (Phase 1 H-7) — `verify_anchor_signatures` has no
  production caller.

**Documentation gaps:**
- **C-3 is the documentation crisis:** the shipped artifact is rc1, so every fix documented against `src/`
  describes code the installer does not run. Release provenance (SBOM, build-from-tag) is missing.
- `main.py:127` and `admin-guide.md:58` instruct operators to use a wizard step that does not exist (H-2).
- ADR-0005 has drifted (reports exist but are unwired; HL7 placeholder raises `NotImplementedError`);
  ADR-0007's load-bearing claim is not preserved at runtime.
- The config model label, admin guide, and `systemd/gateway.env.example` all claim "bcrypt" while the default
  install has no bcrypt (H-4).
- `verify_password`'s docstring describes a fallback that actually fails closed into a lockout (H-4).
- The middleware-ordering comment is factually inverted (L-8) — a comment that misdescribes a security
  control is worse than no comment.
