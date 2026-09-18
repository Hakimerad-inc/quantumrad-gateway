# Phase 3-3B — Documentation Accuracy & Completeness Review

**Repo:** `/home/dev/Documents/mercurie/dicom-gateway` · **Base commit:** `aab94b5` (plus uncommitted WIP in `web/`)
**Scope:** `README.md`, `docs/**` (incl. ADRs, QA records, sprint boards), `PRODUCT_BRIEF.md`, `mercure-gateway-PRD.md`, `product-refinement-spec.md`, `usb-dongle-gateway-spec.md`, `mercure-gateway.spec`, inline docstrings across `src/`, `web/`, `src-tauri/`, generated OpenAPI output, `justfile`/`systemd/` operator instructions.
**Method:** every factual claim in prose and docstrings was checked against the code it names. Severity reflects patient-safety exposure of a *confident wrong* document on a PHI-handling device.

**Severity scale**
- **Critical** — documentation asserts a control or clinical feature that the code does not provide; the doc is the only reason an operator would believe it works.
- **High** — documentation contradicts implementation in a way that misleads operators, reviewers, or the build pipeline.
- **Medium** — drift between decision records / plans and shipped reality; broken or stale instructions.
- **Low** — cosmetic staleness or imprecision with bounded blast radius.

---

## Verdict

The documentation set is unusually thorough and, in the QA records (`docs/qa/`), admirably self-critical. But the two documents an operator or regulator would trust most — the config model's own field labels and the module docstrings in the composition root — contain outright falsehoods that hide total feature failures. Three Critical findings each describe a feature that is documented as working and does not work at all in production: report retrieval (transports never wired), web-UI authentication enablement (no code path exists to set a password), and the bundled backend snapshot (rc1 code inside an rc3-labelled installer).

For a medical device the ranking is unambiguous: an accurate-but-missing doc is a gap; a confident wrong doc is a hazard. Findings C1–C3 are the hazard class.

**Finding counts:** 3 Critical · 7 High · 16 Medium · 12 Low · 4 structural gaps · 8 verified-accurate positives.

### ADR conformance at a glance

| ADR | Subject | Status vs implementation |
|---|---|---|
| ADR-0001 | Receiver transport abstraction | **Conforms.** pynetdicom SCP behind the transport contract; `AllTransferSyntaxes` + `decompress_common`; `max_associations` default 25; `demo/fake_modality.py` as the mirror SCU — all as documented. |
| ADR-0002 | Desktop shell architecture | **Drifted (M2, M2b, M5b).** Decision says Method 1 (separate process, webview loads `http://127.0.0.1:8080`); the shipped shell is Method 2 (bundled PyInstaller sidecar spawned from Rust, webview loads the bundled SPA). Credits Tauri with notifications/auto-start it does not provide. Tray row lists four states, the shell ships three. No amendment. |
| ADR-0003 | License and hub contract | **Conforms, deliberately partial.** MIT accepted; Q7 explicitly deferred and proven against the `test-rig/bookkeeper` stub — the ADR says so in its own status line. |
| ADR-0004 | At-rest encryption | **Conforms, and is the model for the others.** The 2026-09-09 amendment formally declines SQLCipher and records the reasoning; `db.py`/`audit/__init__.py` mark the exact swap points. (The *PRD and product brief* still claim SQLCipher — H7.) |
| ADR-0005 | Report retrieval strategy | **Does not conform (C1, M1).** Decision says C-FIND/C-MOVE only, DICOMweb "deferred to Sprint 08"; sprint-08 marks the DICOMweb transport ✅ done. Neither transport is wired in the composition root, so the ADR's Consequences describe a pipeline that cannot run. |
| ADR-0006 | Auto-update | **Mostly conforms; one claim false (M10, L11).** Signature enforcement, custody, and endpoint match. "Operators can still disable the updater via configuration" is false for the desktop shell — the SPA banner checks unconditionally; the `update.enabled` flag gates only the headless Python path. |
| ADR-0007 | Web admin transport posture | **Substantially conforms; one over-claim (H5).** The refusal, the TLS pair validation, and the HSTS-only-over-TLS change are all implemented and tested as described. The refusal boundary is boot-only and defeatable at runtime via `PUT /config`, which the Consequences present as a continuous guarantee. |

---

# CRITICAL

## C1 — Report retrieval is documented as a working feature; no transport is ever wired, so every report request fails

**(a) The claim, in the code's own docstring** — `src/mercure_gateway/reports/__init__.py:7-10`:

> The transports are injected as `finder` (C-FIND SCU) and `mover` (C-MOVE SCU) callables so the state machine can be unit-tested against fakes; the production wiring in `main.py` supplies the real `ReportFinder` and `ReportRetrieve` implementations.

**(b) The contradicting code:**
- `src/mercure_gateway/main.py:523-526` — the composition root constructs `ReportRetriever(config.reports, database, spool, audit=audit)` with **no `finder`/`mover` argument**. `ReportRetriever.__init__` sets `self.finder = None` / `self.mover = None` (`reports/__init__.py:76-77`).
- `reports/__init__.py:230-231` — `_do_retrieve` raises `RuntimeError("report transports (finder/mover) not configured")`.
- `reports/__init__.py:215-226` — that exception is caught by `retrieve()` and converted to `ReportStatus.FAILED` with a `REPORT_RETRIEVAL_FAILED` audit event.
- A repo-wide grep for `ReportFinder(` / `ReportRetrieve(` finds **zero production construction sites** — only `tests/`.

**Consequence:** in any real run, `POST /api/studies/{id}/reports`, `POST /api/reports/{id}/refresh` and the poller walk every PENDING report straight to `failed`. The `finder`/`mover` arguments exist solely to make the unit tests pass against fakes — the docstring describes a wiring that has never existed.

**The "pluggable transport" layer is the same gap one level down.** `src/mercure_gateway/reports/transport.py:10-14` presents the registry as the composition path:

> Registration is keyed by `query_source.type` so the composition root can build the transport directly from configuration:
> `transport = transport_for_query_source(config.reports.query_source)`

That call appears only in `transport.py`'s own docstring and in `tests/` — `main.py` never invokes it. And `_register_defaults()` (`transport.py:147-154`) calls `register_transport` for `dicom` / `dicomweb` / `fhir` but never `register_factory`, so for `query_source.type == "dicom"` there is no factory and `transport_for_query_source` falls through to `cls(source)`, which constructs `DICOMReportTransport` requiring a `finder` and a `retriever` (`transport.py:122-128`) — i.e. the documented one-liner would raise `TypeError`. `reports/hl7_fhir.py:33` repeats the claim ("can be built uniformly by `transport_for_query_source`") for a transport whose `find`/`retrieve` raise `NotImplementedError` (`hl7_fhir.py:46-53`). The registry is test-only scaffolding described as the composition root.

**Documentation that propagates the falsehood:**
- `docs/adr/ADR-0005-report-retrieval-strategy.md:43-50` — Consequences assert "`reports/find.py` and `reports/move.py` implement C-FIND/C-MOVE Study Root Q/R only" and "Sprint 08 will add a DICOMweb transport implementing the same `ReportFinder`/`ReportMover` protocols". Neither transport is reachable from the running gateway.
- `docs/guides/user-guide.md:64-68` — "The **Reports** tab lists retrieved report objects… Click **Request report** on a study to pull its report on demand."
- `docs/guides/admin-guide.md:36` — the `reports` config row; `:92` implies report SLA behaviour.
- `docs/sprints/sprint-05.md` and `docs/sprints/sprint-08.md:17` — S08-T4 "DICOMweb reports green" marked ✅; sprint-05 marks the C-FIND/C-MOVE pipeline ✅.
- `docs/qa/security-review-package.md:17` — lists "report retrieval (DICOM SR/PDF, DICOMweb, experimental HL7/FHIR)" as an in-place capability for an external security review.
- `README.md:31` — the architecture diagram shows `ReportRetriever` in the data flow.

**Recommendation:** either wire the transports (`main.py` should construct `ReportFinder`/`ReportRetrieve` from `config.reports` and inject them) or mark the feature unimplemented in every doc that claims it. The `reports/__init__.py:9-10` docstring must be rewritten to say the transports are test-injected and that the composition root does not currently supply them. If the feature is genuinely deferred, `docs/adr/ADR-0005` needs an amendment in the style of ADR-0004's, and `user-guide.md` §Reports must carry a "not enabled in this release" banner. Add a composition-root test asserting `retriever.finder is not None` so the gap cannot survive a green suite again.

## C2 — The documented way to enable web-UI authentication does not exist

**(a) The claim, in six places:**
- `src/mercure_gateway/config/__init__.py:401-404` — field description: `"Bcrypt hash of the web UI password. Set via the setup wizard."`
- `src/mercure_gateway/main.py:127-129` — the refusal message operators see at boot: `"Enable web_ui auth (wizard → Setup) or set web_ui.host to 127.0.0.1."`
- `docs/guides/admin-guide.md:58` — "Enable `web_ui.auth_enabled` and set a password hash via the **Setup** wizard."
- `docs/guides/site-deployment.md:92-93` — "enable `web_ui.auth_enabled` (+ a strong password hash via the wizard)"
- `docs/guides/secrets-and-env-overrides.md:60-61` — "Web admin login password hash (bcrypt/sha256$salt$hash). Generate with the setup wizard, or: `htpasswd -bnBC 10 "" 'password' | cut -d: -f2`"
- `mercure-gateway-PRD.md:387` and `product-refinement-spec.md:348` — `"auth_password_hash": ""  // bcrypt hash; set via setup wizard`

**(b) The contradicting code:**
- `src/mercure_gateway/web/wizard.py:20` — `_STEPS = ["receiver", "destinations", "reports", "summary"]`; no auth step exists.
- `web/src/pages/SetupWizard.tsx:5` — `const STEPS = ["receiver", "destinations", "reports", "summary"];` — the SPA matches.
- A grep of all of `web/src` and `src/` for `auth_password_hash` returns **only readers** (`web/auth.py:85,117`, `redact.py:49-50`, `routes.py:758-759`, `config/encryption.py:127-134,172`) and **no writer that creates a hash**. Nothing anywhere hashes a password — no CLI, no endpoint, no UI field.
- `pyproject.toml` — `bcrypt` is **not a declared dependency** (it is present only as a transitive of `paramiko`, and happens to be bundled in the frozen snapshot). The field label's "Bcrypt hash" prescribes an algorithm the project does not depend on.
- `src/mercure_gateway/web/auth.py:61-80` — `verify_password` *verifies* only. Its docstring ("Supports bcrypt … when `bcrypt` is installed and falls back to salted SHA-256 … otherwise") is misleading in one direction that matters: for a `$2b$` hash with bcrypt absent it returns `False` and there is no fallback at all — a permanent lockout with no recovery path documented.
- `docs/sprints/sprint-09.md:25` — S06-T8 "Web UI auth … bcrypt/sha256 password hash … 401 enforcement" is marked ✅, treating the verify half as the whole feature.

**Consequence:** `auth_enabled=true` with an empty `auth_password_hash` locks the panel with no in-product remedy. The only real path is the out-of-band `htppasswd`/env-var workaround in `secrets-and-env-overrides.md:60-63`, which the doc presents as an *alternative* to a wizard step that does not exist. On a PHI device this is the documented path to the only authentication control.

**Recommendation:** delete "Set via the setup wizard" from `config/__init__.py:403` and `main.py:127-129`; add an authoritative "Enabling web UI authentication" section to `admin-guide.md` §Authentication that gives the working recipe (`MERCURE_GATEWAY_WEB_UI_AUTH_ENABLED=true` + a hash generated with `htpasswd -bnBC 10`, or a `sha256$salt$hex` value plus a documented generation command — none exists today). Either add the auth step to the wizard or add a `POST /api/webui/credentials` endpoint that hashes a chosen password. Until one of those lands, `secrets-and-env-overrides.md:61` should read "the wizard cannot set this; generate it with…". Change the config label to "Password hash for the web UI (`$2b$…` bcrypt or `sha256$salt$hex`); the gateway never generates it — see admin guide."

## C3 — The bundled backend snapshot is rc1 code in an rc3-labelled installer, and no provenance or guard catches it

**(a) What the docs assert:**
- `docs/dev/packaging.md:26-31` — "Freeze the backend (PyInstaller onedir → dist/, copy → src-tauri/binaries/)" then "`cargo tauri build` (bundles the binaries/ dir as a resource)".
- `docs/dev/release-runbook.md:61` — "The tag should be the tip commit of a green main"; `:79-82` — "per-OS **build** jobs freeze the PyInstaller sidecar… so the built sources match the published version."
- `docs/qa/rc1-checklist.md` (rc3 row) — asserts the rc3 tag and its signed artifacts.

**(b) The contradicting state:**
- `src-tauri/tauri.conf.json:31` — `"resources": ["binaries/mercure-gateway/"]` bundles whatever sits in that directory.
- The on-disk snapshot at `src-tauri/binaries/mercure-gateway/_internal/mercure_gateway/__init__.py:4` declares `__version__ = "1.1.0-rc1"`, while `tauri.conf.json:4`, `src-tauri/Cargo.toml:3`, `pyproject.toml:3` and `src/mercure_gateway/__init__.py:4` all declare `1.1.0-rc3`.
- The snapshot's `main.py` (`src-tauri/binaries/mercure-gateway/_internal/mercure_gateway/main.py:91,411`) defines only `_warn_insecure` and calls it at boot. The `_enforce_bind_security` **`SystemExit`** that `src/mercure_gateway/web/auth.py:6-7` says its no-op-when-auth-off design "rests on", that ADR-0007:52 calls "the hard boundary", and that `docs/guides/site-deployment.md:78` tells operators to verify at the site — **does not exist in the code the local build would run**.
- `src-tauri/binaries/` is gitignored (`.gitignore:80`; `git ls-files src-tauri/binaries` → 0 files), so nothing in version control pins which tag produced the snapshot, and no SBOM, manifest, commit ref, or version record is written next to it by `scripts/package_backend.py` (`build()` `:82-116`, `copy_to_tauri()` `:119-130` — it runs PyInstaller, `copytree`s, prints the size, writes nothing else).
- No check anywhere asserts the frozen backend's `__version__` equals the canonical version. The rc1 smoke test in `docs/qa/rc1-checklist.md` recorded `{"status":"ok","version":"1.1.0-rc1"}` and did not flag it as drift; `docs/qa/b3-tray-leg.md` records the live dev node still reporting `1.1.0-rc1` on `:8081` while rc2/rc3 work proceeded.

**Scope correction to carry forward:** the *published* CI releases re-freeze the sidecar from source (`release.yml` runs `scripts/package_backend.py` before bundling), so published rc2/rc3 artifacts are not rc1. The exposure is (i) any local `cargo tauri build` from this working tree, which bundles the stale rc1 snapshot into an installer that *reports* 1.1.0-rc3, and (ii) the absence of any guard that would catch a stale snapshot at tag time.

**Recommendation:** add a version assertion to `scripts/package_backend.py` (parse `__version__` from the frozen tree and fail if it != `read_canonical_version()`), record it in the release-runbook §1 pre-tag sequence, and emit a manifest (`_internal/PROVENANCE.json` with tag, commit SHA, pyinstaller version, dependency set) that `verify_release_sig.py`-style tooling can read. Until then, `docs/dev/packaging.md` must state that `cargo tauri build` bundles whatever `src-tauri/binaries/` currently holds and that `just build-backend` must be re-run after every version bump. Note that `site-deployment.md:81-83`'s instruction to "verify the refusal at the site (proves the build is post-D3b)" is the one control that would have caught this — keep it, and reference it from the runbook.

---

# HIGH

## H1 — The wizard's Receiver step is silently discarded on save, then the UI claims success

**(a) Implied by** `web/src/pages/SetupWizard.tsx:151-152` ("Receiver Settings" card) and `docs/guides/user-guide.md:24-28` ("**Receiver** — the AE title and port the gateway listens on…").

**(b) Contradicting code:**
- `web/src/pages/SetupWizard.tsx:79` — `current.general = { ...(current.general as object || {}), ...data.receiver };` — `data.receiver` is `{ae_title, port}` (`:14-15`).
- `src/mercure_gateway/config/__init__.py:67-72` — `GeneralConfig` has only `appliance_name` / `locale` / `log_level`. There is no `model_config`/`extra=` override, so Pydantic's default `extra="ignore"` **drops both keys silently**. AE title and port belong under `config.receiver` (`ReceiverConfig`, `:75-84`).
- `SetupWizard.tsx:87` — `aet_source: "GATEWAY"` is hardcoded for every created destination instead of using the AE title the operator just typed.
- `SetupWizard.tsx:131-141` — the success screen reads "Configuration saved… The gateway is configured and running. You can now receive and forward DICOM studies." — false for the receiver settings, which were validated server-side (`POST /api/wizard/validate/receiver` passes) and then thrown away.

**Documentation impact:** `docs/guides/site-deployment.md:60-62` makes the wizard the K4 evidence path ("identity → receiver → destination(s) → test → done" — note also that "identity" and "test" are not actual wizard steps, see M9); `docs/guides/user-guide.md:44-48` tells operators to point their modality at the AE title they entered.

**Recommendation:** write to `current.receiver`, not `current.general`; use the entered AE title for `aet_source`; add a backend assertion in `PUT /api/config` (or a wizard-specific route) that the receiver block actually changed. Change the summary step to echo the *saved* receiver config read back from the API rather than the local form state.

## H2 — CSP omits `frame-src`, so the PDF report iframe — the only PDF rendering path — is refused

**(a) Nothing documents the constraint.** The CSP rationale comment at `src/mercure_gateway/web/__init__.py:64-67` explains `script-src`, `style-src`, `img-src`, `connect-src`, `object-src`, `base-uri` and `frame-ancestors` and says nothing about `frame-src`. `docs/guides/admin-guide.md:83-86` presents the header story as complete: "Security headers: CSP, `X-Content-Type-Options: nosniff`, and `X-Frame-Options: DENY` on every response…".

**(b) Contradicting code:**
- `src/mercure_gateway/web/__init__.py:68-77` — the CSP is `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'` — **no `frame-src`**.
- `web/src/pages/ReportsView.tsx:107-113` — `<iframe title={…} src="data:application/pdf;base64,${selected.content}" … />`, gated on `selected.mime === "application/pdf"`; `src/mercure_gateway/web/routes.py:966-1000` returns base64 PDF bytes for exactly that purpose. The iframe is the only PDF rendering path in the SPA.
- `object-src 'none'` additionally blocks any `<object>`/`<embed>` fallback.
- Consequence: `frame-src` falls back to `default-src 'self'`; a `data:` URL is not `'self'`, so the iframe load is refused and PDF reports render blank. (`X-Frame-Options: DENY` at `web/__init__.py:63` is not the blocker — it attaches to no response for a `data:` URL.)
- The gap is invisible to CI: `tests/test_web_security.py:42` asserts only that a `content-security-policy` header is present, never its directive set.

**Recommendation:** add `frame-src 'self' data:` (or restructure PDF display to a `blob:` URL plus an explicit `frame-src`), and add a test asserting the directive exists when the reports content endpoint can return `application/pdf`. If PDF display is intentionally deferred, say so in `user-guide.md` §Reports and `admin-guide.md` §Security headers.

## H3 — The OpenAPI document advertises version 0.1.0 while the product ships 1.1.0-rc3

**(a) `src/mercure_gateway/web/__init__.py:161-165`:**

```python
app = FastAPI(
    title="QuantumRAD Gateway API",
    version="0.1.0",
    ...
)
```

**(b) Contradicting code:** canonical version is `1.1.0-rc3` (`src/mercure_gateway/__init__.py:4`, `pyproject.toml:3`, `src-tauri/tauri.conf.json:4`, `src-tauri/Cargo.toml:3`, `web/package.json`). The version-sync guard that is supposed to catch this — `scripts/sync_version.py` + `tests/test_version_sync.py` — does not read the FastAPI `version` string at all; `_expected()` (`sync_version.py:33-61`) rewrites five mirror files, none of them `web/__init__.py`. The generated client (`web/src/types/api-schema.ts`, checked in) therefore carries the wrong version into the SPA.

**Recommendation:** set `version=__version__` (import from `mercure_gateway`) and add `web/__init__.py`'s FastAPI version to `sync_version.py`'s `_KINDS`/`_expected()` so the guard covers it. Any external reviewer or integrator reading `/openapi.json` today concludes the API is a 0.1.0 dev artifact.

## H4 — The middleware-ordering comment is factually inverted, and the inversion defeats a documented CSRF fallback

**(a) `src/mercure_gateway/web/__init__.py:179-181`:**

> `# Security middleware FIRST (runs outermost): headers on every response,`
> `# CSRF origin check before the CORS handling.`

**(b) Contradicting code:** Starlette's `add_middleware` does `self.user_middleware.insert(0, …)` and `build_middleware_stack` iterates `reversed(...)`, so the **last** added middleware is outermost. `web/__init__.py:181` adds `_SecurityMiddleware`, then `:185-191` adds `CORSMiddleware` — CORS is outermost, the security middleware is inner. The comment is backwards.

Real consequence, not just words: `web/__init__.py:40-42` documents that the CSRF check accepts "any port on loopback hosts… tests bind isolated web ports, e.g. E2E". But a cross-origin browser request from `http://127.0.0.1:<other-port>` is answered by the outer CORS middleware (whose `allow_origins` is the fixed four-entry list at `:43-48`) before the inner permissive loopback fallback is ever reached — so the documented fallback does not do what its comment says for the browser case it names.

**Recommendation:** reverse the comment to describe reality ("CORS outermost; the CSRF origin check runs inside it"), and either move `_SecurityMiddleware` after CORS or extend `CORSMiddleware`'s allow-list so the loopback-any-port behaviour the code comments rely on actually holds. Add a test that a cross-origin POST from a non-default loopback port is accepted/rejected per the documented intent.

## H5 — ADR-0007's refusal boundary is boot-only and is defeatable at runtime; the ADR does not say so

**(a) `docs/adr/ADR-0007-web-admin-transport-posture.md:52-57`** describes `_enforce_bind_security` as "the hard boundary", and `:71-74` (Consequences) asserts "the refusal boundary still applies (auth must be on, or TLS+auth, or the explicit hatch), so PHI never crosses an unauthenticated cleartext socket by accident."

**(b) Contradicting code:** `src/mercure_gateway/web/routes.py:786-792` — `PUT /api/config` validates the payload and then does `request.app.state.config = updated`, replacing the **live** config object. `require_auth` (`web/auth.py:94-96`) reads `request.app.state.config.web_ui.auth_enabled` on every request, and `_enforce_bind_security` is called **only** from `main()` at boot (`main.py:456`). So an authenticated operator on a non-loopback, TLS-protected panel can `PUT` `web_ui.auth_enabled: false` and the unauthenticated API is immediately live on the network — no restart, no refusal, no warning. The running uvicorn socket keeps its bind, so the boot check never re-fires.

**Recommendation:** either re-run the bind check inside `PUT /config` (reject a saved config that would leave a non-loopback bind unauthenticated), or add an ADR-0007 amendment stating the boundary is boot-time only and that runtime auth disable on a network-bound panel is an audit finding. The current text reads as a continuous guarantee.

## H6 — Two documented developer workflows in the `justfile` cannot run as written

**(a) `justfile:92-97` (`gen-api`):**

```make
gen-api:
    uv run python scripts/export_openapi.py
    cd web && npx openapi-typescript ../mercure-gateway.openapi.json -o src/types/api-schema.ts
```

**(b) Contradicting code:** `scripts/export_openapi.py:58-61` writes the schema to **stdout** (`json.dump(app.openapi(), sys.stdout, …)`) and never creates `mercure-gateway.openapi.json`; that file does not exist in the working tree and is not gitignored. The recipe's second line reads a file the first line never produced, and the script's own docstring (`:5-7`) documents a *third*, different invocation (`> /tmp/openapi.json`). `docs/dev/setup.md:62-67` points developers at `just gen-api` as the only regeneration path — so the checked-in `web/src/types/api-schema.ts` has no working regeneration procedure.

**(c) `justfile:51-55` (`e2e`):** `cd web && npx playwright test -c playwright.config.ts` — but `playwright.config.ts` lives at the **repo root** with `testDir: "./e2e/tests"` and `globalSetup: "./global-setup.ts"`, all root-relative. From `web/` none of those paths resolve. `e2e/README.md` gives the correct root-relative command; the two documents disagree.

**Recommendation:** fix both recipes (`uv run python scripts/export_openapi.py > mercure-gateway.openapi.json`, and drop the `cd web` / use `-c ../playwright.config.ts`), and reconcile `export_openapi.py`'s docstring with the recipe. Add a CI or pre-commit check that the checked-in `api-schema.ts` matches a fresh generation, since drift is currently undetectable.

## H7 — The PRD and product brief claim SQLCipher AES-256 at rest; ADR-0004 formally declined it

**(a) The claims:**
- `mercure-gateway-PRD.md:200` — "SQLite (SQLCipher/encrypted), filesystem storage of DICOM".
- `mercure-gateway-PRD.md:421` — "At rest: SQLite encrypted with SQLCipher (AES-256); DICOM files stored on local disk with restrictive ACLs…".
- `PRODUCT_BRIEF.md:35` — "Encrypted Local Spool | SQLite (SQLCipher) + filesystem; persist-before-ack"; `:75` "SQLCipher"; `:139` "Encryption: SQLCipher spool…".

**(b) Contradicting code and decision record:**
- `src/mercure_gateway/spool/db.py:20` — "Encryption-at-rest (SQLCipher) is a documented stub only: SQLite connections…".
- `src/mercure_gateway/main.py:462-465` — "…open_database derives an HMAC verifier from it… True SQLite *encryption* still requires SQLCipher — documented as a follow-up."
- `docs/adr/ADR-0004-at-rest-encryption.md` Amendment (2026-09-09) — "v1.1 SQLCipher upgrade path formally **declined**", relying on OS full-disk encryption plus the key-required HMAC verifier.

The canonical product documents still assert a database encryption control that was evaluated and rejected. `PRODUCT_BRIEF.md` also states "encryption enabled by default" as a US-07 acceptance criterion that is not met as written.

**Recommendation:** update PRD §5.1/§6.1 with an "Amendment (see ADR-0004)" note replacing the SQLCipher claim with the actual posture (plain SQLite + HMAC key verifier + OS-level FDE expectation), and refresh `PRODUCT_BRIEF.md` §3/§8. `docs/qa/security-review-package.md:57-60` already states this correctly — use its wording.

---

# MEDIUM

## M1 — ADR-0005 is stale against its own sprint board

`docs/adr/ADR-0005:24-25` decides "DICOMweb / WADO-RS retrieval is deferred to Sprint 08"; `docs/sprints/sprint-08.md:17` marks S08-T4 (DICOMweb QIDO/WADO) ✅ done and `:18` marks the HL7/FHIR transport ✅. The ADR was never amended, and its Consequences (`:43-52`) describe a wiring that does not exist (see C1 — no transport is wired, including C-FIND/C-MOVE). ADR-0005's decision text is *operationally* correct ("the gateway retrieves SR and PDF via C-FIND/C-MOVE only") but for the wrong reason. **Recommendation:** add an amendment recording that the transports landed as modules, that they are unwired in the composition root, and that `reports/hl7_fhir.py`'s `find`/`retrieve` raise `NotImplementedError` — and mark the HL7/FHIR row in sprint-08 as "module only, not wired".

## M2 — ADR-0002 describes Method 1; the shipped shell is Method 2

`docs/adr/ADR-0002-desktop-shell-architecture.md:31` diagrams `Webview → http://127.0.0.1:8080`; `:55` "FastAPI runs as a separate process on localhost:8080. Tauri webview loads this URL."; `:93` "Method 2 can be adopted later". In the shipped architecture the Rust shell **bundles** the PyInstaller onedir and spawns it by resolved path (`docs/dev/packaging.md:16-19`, `src-tauri/src/lib.rs`), the webview loads the bundled SPA from the Tauri app origin (`WebviewUrl::App("index.html")`, `frontendDist: ../src/mercure_gateway/web/static`), and the API port is injected by the shell via `window.__MERCURE_PORT__` — that *is* Method 2 ("FastAPI is bundled as a Tauri sidecar binary"). The same ADR's tray row (`:45`) lists four states `idle / sending / error / safe-to-remove` while the Rust copy implements three (see M5). Also `:21-23` credits Tauri with "system notifications, auto-start" it does not provide (no notification or autostart plugin in `src-tauri/Cargo.toml`; auto-start is a separate Windows scheduled-task script, `scripts/install_windows_autolaunch.ps1`).

The ADR's Method 2 row (`:66`) also promises lifecycle management the shell never performs: `src-tauri/src/lib.rs` spawns the sidecar once, holds the child in one async task, and on failure only logs — no retry, no health check, no re-spawn path exists anywhere in the crate. So even where the ADR's Consequences (`:113`) treat the shipped design as Method 1, the deployed shell is Method 2 *without* Method 2's stated benefit.

The same webview-load falsehood appears in `src/mercure_gateway/ui/__init__.py:16-17` and in `docs/sprints/sprint-06.md:45`; `src-tauri/src/lib.rs:247-250` correctly states the SPA is "loaded from the tauri://localhost origin", contradicting the ADR it implements.

**Recommendation:** add an amendment recording that Method 2 was adopted at S06/S09 (and that its lifecycle management is not yet implemented), correcting the webview-load claim, and trimming the tray state list — the ADR-0004 amendment is the template.

## M2b — The documented local build command fails on Windows for a reason CI explicitly works around

`docs/dev/packaging.md:29-30` instructs `cd src-tauri && cargo tauri build`, and `src-tauri/tauri.conf.json:20` sets `"targets": "all"`. `.github/workflows/release.yml:31-33` records that `targets: "all"` "would also emit MSI, and the MSI version field rejects semver pre-release identifiers ('1.1.0-rc1' → bundle error, first release run 34902860370)", so CI overrides with `--bundles nsis` (`release.yml:36`, `:73`). With the current `1.1.0-rc3` version, the documented local command on Windows fails for exactly the reason CI documents and works around — and nothing in `packaging.md` warns the operator. **Recommendation:** add the `--bundles nsis` note to `packaging.md` §Build chain, or set `"targets": ["nsis", "deb", "appimage"]` in `tauri.conf.json`.

## M3 — `PRODUCT_BRIEF.md` is stale on nearly every axis

- `:3` — "**Version:** 0.1.0-dev (Sprint 09/10 in progress; targeting v1.1-RC)"; actual `1.1.0-rc3`, three RCs published.
- `:4` / `:205` — "**Branch:** `docs/sprint-plan`"; the repo is on `main`.
- `:162-172` — the repository-structure map lists `config.py`, `hub/`, `rules/` as packages; the actual layout is `config/__init__.py`, `hub_client.py` + `hub_events.py`, `rules.py`.
- `:192` — "ADRs | `docs/adr/ADR-0001` … `ADR-0006`" — ADR-0007 exists.
- `:83-96` / `:100-117` — sprint statuses and "470+ tests… 84% coverage" (rc1-checklist records 733 passed / 85% at rc3).
- `:179` — `.github/workflows/ci.yml` exists, but `release.yml` (the pipeline the runbook documents) is unmentioned.

**Recommendation:** either bring it current or mark the header "**Historical** — see `docs/qa/rc1-checklist.md` for current state" and stop linking it from `README.md:24`. A stale product brief is the document most likely to be quoted in a procurement or regulatory context.

## M4 — The release runbook is an rc1 document for an rc3+ pipeline

`docs/dev/release-runbook.md:1` titles itself "v1.1.0-rc1 (and later tags)"; `:66` instructs `git tag -s v1.1.0-rc1`; `:90-94` gives a verification `curl` against the release URL. The repo is at rc3, and the rc1-checklist rc3 row notes the repo is private so the anonymous `curl` in runbook §3 404s — a known-broken instruction that the runbook still hands to the release captain. **Recommendation:** parameterise the version, replace the anonymous curl with `gh release download`, and add the pre-tag checks that actually matter now (frozen-snapshot version match per C3, `just gen-api` freshness per H6).

## M5 — The tray state machine is documented as four states and live as three; the fourth is unreachable end-to-end

- `src/mercure_gateway/tray.py:7-11` documents `idle / sending / error / removable` with priority `error > sending > removable > idle` (`:28-51`), and `:3-5` states "The Rust side only polls `GET /api/system/status` + `GET /api/queue/stats` and feeds the raw counts here".
- `SystemStatus` (`src/mercure_gateway/web/routes.py:126-133`) has **no `usb_mode` field**, so no API response can carry it; the Rust poller re-derives the state locally without importing `tray.py` at all. `tray.py`'s only caller is `tests/`.
- The Rust copy implements three states (`src-tauri/src/lib.rs:13-15`), `state_label` has no `removable` arm (`:140-146`), `TrayIcons` decodes three glyphs (`:168-173`), and `derive_state` never reads `usb_mode` (`:115-138`) — while `src-tauri/src/lib.rs:12` and `:113-114` claim the Rust side "mirrors the Python `derive_tray_state` output". `docs/qa/b3-tray-leg.md` confirms three glyphs (green idle / amber sending / red error).
- `tray.py:32-34` defaults missing component fields to `"running"` while both the Rust copy (`lib.rs:116-118`, `unwrap_or("stopped")`) and the API model (`routes.py:127-129`) default to `"stopped"` — the same payload yields different states in the two copies.
- The tests that exercise the four-state machine (`tests/test_tauri_integration.py`, whose module docstring asserts the derivation "lives in Python where it is RED-testable") cover the implementation that **does not ship**. The shipping `derive_state` in `lib.rs:115` has no test at all — the crate's `#[cfg(test)]` block contains four tests, all about path resolution and port parsing, corroborated by `docs/qa/rc1-checklist.md`'s "4 unit tests" row.

**Recommendation:** delete the `removable` state from `tray.py` (or plumb `usb_mode` into `SystemStatus` and make the shell use the Python derivation), fix the divergent component defaults, and add unit tests for `derive_state` so the tested code is the shipped code.

## M5b — Rust comments attribute the updater relaunch and the webview origin incorrectly

- `src-tauri/src/lib.rs:186-190` — "Auto-update plumbing (ADR-0006): the updater verifies Ed25519-signed artifacts against the pubkey in tauri.conf.json …; **the process plugin performs the relaunch after an update is installed.**" Neither the updater nor the process plugin is ever invoked from Rust (a grep for `relaunch|check_for_update|UpdaterExt|ProcessExt` returns only that comment line). The relaunch is performed by the SPA (`web/src/ui/UpdaterBanner.tsx:46`, `const { relaunch } = await import("@tauri-apps/plugin-process"); await relaunch();`); the update check is at `UpdaterBanner.tsx:24`.
- `src-tauri/capabilities/default.json:4` — "Minimal default capability: the webview loads the localhost SPA and opens the main window". In a packaged build the webview loads the **bundled** SPA from the Tauri app origin, not a localhost URL (`lib.rs:199-210`, `tauri.conf.json:10`); `127.0.0.1:8080` is only `devUrl`. The shell's own comment at `lib.rs:247-250` says so.
- `src-tauri/src/lib.rs:246-250` describes the backend as reachable "on 127.0.0.1:8080", but the CSP at `tauri.conf.json:15` is host-only (`default-src 'self' http://127.0.0.1`) with no port restriction — so the webview may talk to *any* localhost service, which is required for the `MERCURE_BACKEND_PORT` override (`lib.rs:17-35`) but is undocumented as a deliberate posture.

**Recommendation:** correct the relaunch attribution in `lib.rs:186-190` to the webview; correct the capability description to "loads the bundled SPA"; add a one-line comment recording that the CSP is deliberately port-permissive for the same reason.

## M5c — The Tauri dev path is documented as working while it collides with the developer's own backend

`docs/dev/packaging.md:21` — "The dev fallback candidate lets `cargo tauri dev` work from the manifest dir." In dev the webview loads `devUrl http://127.0.0.1:8080` (`tauri.conf.json:9`) — the same port the developer's FastAPI occupies — while `find_backend` (`lib.rs:93-95`) will still resolve and spawn the frozen backend on that port (`boot_port` default 8080, `lib.rs:182`). With a real bundle present in the tree (it currently is, see C3) the shell spawns a second listener against 8080. `scripts/tauri_placeholders.py:1-10` documents the dev stub as a "zero-byte marker — never shipped, never executed", which is the only reason this is usually silent. **Recommendation:** document the collision in `packaging.md` and have the dev path derive a non-default `boot_port`.

## M6 — `usb-quickstart.md` documents an LED mapping the code does not have

`docs/guides/usb-quickstart.md:33-39` states: "Solid green — gateway running, no issues; Blinking green — forwarding in progress; Solid red — disk full or critical error; Blinking red — shutdown in progress", and `:51` advises waiting for the LED to go "solid amber". Actual behaviour in `src/mercure_gateway/led.py:41-84`: six **solid** colours only — `off` (stopped), `blue` (idle, both components running), `green` (sending/queued), `yellow` (errors or disk over threshold), `red` (critical: forwarder down while receiver accepts, or disk-over-threshold with purge disarmed), `white` (safe to remove). There is no amber and no blinking anywhere in `led.py`. Every row of the quickstart table is wrong. Separately, `usb-dongle-gateway-spec.md:60-67` (§3.3) specifies slow/fast blink and pulse semantics that `led.py` does not implement — though `:58` honestly flags "The MVP can ship without LED; LED is v1.1 enhancement", the quickstart presents the LED table as current behaviour. **Recommendation:** replace the quickstart table with the real mapping, or state that the LED is unimplemented in this release.

## M7 — The "how many version sources" number disagrees across five documents

`scripts/sync_version.py:4` "four other files duplicate it"; `sync_version.py:check()` docstring "all five real files"; `tests/test_version_sync.py` test name `test_all_five_version_sources_are_in_sync` whose body lists four; `docs/dev/release-runbook.md:55` "all six sources agree"; `docs/qa/rc1-checklist.md` rc1 row "all five sources" vs rc3 row "all six sources". The truth is 1 canonical + 5 mirrors (pyproject, package.json, tauri.conf.json, Cargo.toml, Cargo.lock). **Recommendation:** standardise on "five mirrors plus the canonical `__version__`" and make the test name match.

## M8 — "CSRF tokens" is documented; the implementation is an origin check with no token

`docs/qa/security-review-package.md:42` and `docs/sprints/sprint-09.md:22` list "CSRF tokens" as a control. A grep of `src/mercure_gateway/` finds no token generation or verification anywhere — the control is the Origin allow-list in `_SecurityMiddleware` (`web/__init__.py:102-128`), which ADR-0007 and `admin-guide.md:85` correctly call "an origin check". **Recommendation:** correct both to "origin check (CSRF)".

## M9 — `wizard.py`'s module docstring describes an API and a state machine that do not exist

`src/mercure_gateway/web/wizard.py:8-11`:

> Each step has a **validation gate**… The wizard can only be saved (`is_complete()`) once every step is complete. This module holds the pure state-machine logic; the SPA drives it over `GET/POST /api/wizard`…

There is no `/api/wizard` route — only `POST /api/wizard/validate/{step}` (`routes.py:1131`), which constructs a throwaway `SetupWizard()` per request (`:1141`), so no completion state survives between calls. `complete_step`, `is_step_complete`, `is_complete` and `_validate_summary` are dead outside `tests/test_wizard.py`. The module is a stateless validator, not a state machine. **Recommendation:** rewrite the docstring to match: "stateless per-step validator, called over `POST /api/wizard/validate/{step}`; the SPA holds the state."

## M10 — The desktop auto-update banner is documented as a deferrable slice; it ships and runs unconditionally

`docs/dev/packaging.md:65-68` calls the SPA update banner "a deferrable slice — the Python-side log-only startup check already satisfies ADR-0006's opt-in minimum". `docs/adr/ADR-0006-auto-update.md:47-48` states "Operators can still disable the updater via configuration… update checks are gated behind a config flag." The banner (`web/src/ui/UpdaterBanner.tsx`) runs `check()` on mount whenever it is inside Tauri, reading no config flag; `update.enabled` gates only the Python headless path (`main.py:271-283`). So the config gate does not cover the desktop shell — the form factor the ADR is about. **Recommendation:** update both docs; either gate the banner on an exposed config value or state that the in-app check is unconditional within Tauri and version-pinned sites must block the endpoint instead.

## M11 — There is no `web/README.md`, and load-bearing SPA knowledge is undiscoverable from inside `web/`

Non-obvious facts that only live in the root `justfile` or scattered comments: the build output is written into the backend's static dir (`web/vite.config.ts` → `../src/mercure_gateway/web/static`); `tsc -b` not `--noEmit` because the root tsconfig has `files: []`; vitest must run from `web/`; `just gen-api` after model changes. Also `web/mercure-gateway.json` is an untracked, unreferenced stray file (no `.ts`/`.tsx`/`.html`/`.json` under `web/` reads it) next to three `.bak` copies at the repo root — confusing in a directory that also contains a `mercure-gateway.json`-shaped config fixture. **Recommendation:** add a short `web/README.md` capturing the four traps (the existing `docs/dev/setup.md` text can be lifted), and delete the stray JSON.

## M12 — The forwarder docstring names a `retry_max` knob that does not exist and misdescribes the requeue timing

`src/mercure_gateway/forwarder/__init__.py:5` — "applies retry with exponential backoff up to `retry_max` attempts, after which the study is flagged `FAILED`". There is no `retry_max` field anywhere in the codebase (grep confirms): the budget is `RetryPolicy.max_attempts`, hardcoded to `5` with a `5.0 s` base (`forwarder/__init__.py:59-64`), and `main.py:176` constructs the forwarder with the default policy — retry is **not configurable**, contradicting the implication that `retry_max` is a config knob. The same docstring's `:13` claim — "the task is re-claimed by id on the next poll" — is also wrong: `_dispatch` requeues and re-claims **inside the same loop iteration** (`forwarder/__init__.py:248-252`), it does not wait for a poll. Separately, `:240-243`'s comment that "while this worker sleeps, the route stays locked in `sending`" is backwards — `_fail_task` sets the route to `error` (`spool/db.py:835-845`) *before* the backoff sleep. **Recommendation:** rename the docstring's knob to `RetryPolicy.max_attempts`, state that the policy is currently hardcoded, and correct the two mechanism descriptions.

## M13 — The spool schema docstring lists four of seven tables

`src/mercure_gateway/spool/db.py:16-17` — "Schema per PRD §5.4 with `studies`, `task_routing`, `reports` and `audit_events` tables." `SCHEMA_SQL` creates seven — those four plus `db_meta` (`:132`), `instance_meta` (`:139`) and `hub_outbox` (`:158`), the last of which is load-bearing for the durable hub outbox that `docs/guides/backup-restore.md` and `systemd/README.md:6` both rely on. The "with" phrasing reads as an exhaustive enumeration. **Recommendation:** either list all seven or reword to "centred on".

---

# LOW

- **L1** `src/mercure_gateway/web/__init__.py:206` — the SPA-not-built fallback tells operators "Run 'npm run build' in `web/static/`". That directory does not exist; the SPA source is `web/` and the output lands in `src/mercure_gateway/web/static/`. An operator-facing error message pointing at a nonexistent path.
- **L2** `src/mercure_gateway/main.py:8-12` — the module docstring's construction/shutdown-order list omits the database (constructed at `:468`, before the receiver), the head anchorer and `spool.stop()`, and lists database last in construction. "Shutdown order is the reverse of construction" is not accurate as written.
- **L3** `web/eslint.config.js:14` — `ignores: ["dist", "coverage", "src/mercure_gateway", "node_modules"]`, but `web/package.json` runs `eslint src` from `web/` where `src/mercure_gateway` cannot exist (it is the repo-root Python layout). Harmless, but it implies an exclusion that does not apply.
- **L4** `web/src/types/api.ts:9-11` — claims the hand-written interfaces are "derived from that schema **where a response model exists**" and that they live "in `api.ts`". `ServiceStatus` (`:163-167`) is hand-written despite `ServiceStatusModel` existing in the generated client and being the `response_model` of `GET /api/service` (`routes.py:407`); only 5 of 39 operations have a `response_model` at all. The interfaces live in `types/api.ts`, not `api.ts`.
- **L5** `docs/dev/packaging.md:52` says the K6 gate is "≤ 250 MB" while `docs/dev/release-runbook.md:74` prints "K6 gate 500 MB" for the deb. `scripts/check_installer_size.py:6-7,21` confirms 250 MB is the default and 500 MB the deb-specific override — the runbook's phrasing invites the misreading that K6 itself is 500 MB.
- **L6** `web/src/index.css` has a comment "Protected route wrapper ensures content doesn't flash" naming a component that does not exist (the auth gate is an inline conditional in `App.tsx`).
- **L7** `web/src/pages/ConfigView.test.tsx:20` and `web/src/config/lint.test.ts:24` use `general: { ae_title: 'GATEWAY' }` as the "representative clean document". `GeneralConfig` has no `ae_title` (it belongs to `ReceiverConfig`), so these fixtures model a document `GET /api/config` can never return; Pydantic silently ignores the key, so the tests pass against a shape the backend never emits.
- **L8** `README.md` is accurate but is a developer front door only: no operator/deployment entry point (the admin/user/site-deployment guides are unlinked), no ADR index, and the architecture diagram omits the Tauri shell entirely despite it being the primary distribution form. For a device shipped as an installer, the README should route non-developers to `docs/guides/`.
- **L9** `src-tauri/tauri.conf.json:27-29` lists `icons/tray-idle.png`, `tray-sending.png`, `tray-error.png` under `bundle.icon` — the installer icon set. Those PNGs are actually consumed as embedded byte resources via `include_bytes!` in `src-tauri/src/lib.rs:216-218`, never as bundle/installer icons. Harmless, but the config implies an icon set it does not use.
- **L10** `src-tauri/capabilities/default.json:4` calls the permission set "Minimal", but `core:default` already expands to `core:window:default` + `core:webview:default` (`src-tauri/gen/schemas/acl-manifests.json`), so both are listed twice; `core:menu:default`/`core:tray:default` are granted to web content that never uses them (the tray and menu are built from Rust). There is also no `#[tauri::command]` anywhere in `src-tauri/src/`, so the allow-list guards no app commands — not a bug, but the "minimal" framing overstates it.
- **L11** `docs/adr/ADR-0006-auto-update.md:41-42` says `tauri.conf.json` will enable `plugins.updater` with "pubkey embedding the Ed25519 public key (S09-T4)". The committed config still holds the literal `"pubkey": "REPLACE_VIA_RELEASE_CONFIG"` (`tauri.conf.json:40`), substituted only in CI. `docs/dev/packaging.md:58-64` documents this correctly; the ADR reads as if the key is in the repo.
- **L12** `web/src/ui/flow.tsx:18` documents the `dot` prop as `green | gray | yellow | red | accent` and `web/src/index.css` defines `.pipe-dot.accent`, but no call site ever passes `dot="accent"` (`PipelineView.tsx` uses `gray`/`green`/`yellow`/`red` only). A dead token advertised in a docstring.

---

# Structural gaps (missing, not wrong)

## G1 — API documentation has no contract for most endpoints

- **34 of 39 operations have no `response_model`** (verified by parsing `src/mercure_gateway/web/routes.py`: 39 decorators, 5 with `response_model` — `/system/status`, `/system/disk`, `/service`, `/queue/stats`, `/studies`). The prior phase's "28 of 38" figure is optimistic. Without a response model the generated schema degrades to `unknown`, so `GET /config`, `GET /studies/{id}`, `GET /reports/{id}/content`, `GET /audit/verify`, `GET /pipeline`, `GET /diagnostics/export` — the endpoints an integrator most needs — are undocumented in the machine-readable contract that `web/src/types/api-schema.ts` is generated from.
- **No request/response examples anywhere**; no `summary`/`description` on many operations; error contracts (400/401/403/404/503 shapes) are undocumented — e.g. `/reports/{id}/content` can return five different shapes depending on state, none of them described in the schema.
- **No API versioning**: every route is hardcoded under `/api` with no version segment and no ADR on HTTP versioning. The runbook's own downgrade finding (`check_update()` treating an older signed archive as an update, rc1-checklist rc3 row) shows version reasoning is load-bearing elsewhere but absent from the API contract.
- **Recommendation:** add `response_model` (or `responses=`) to the remaining operations, add an ADR-0008 on API versioning before GA, and regenerate `api-schema.ts` via a *working* `gen-api` (H6). Given that the SPA already consumes this schema, the ROI is high.

## G2 — No changelog, no migration guide, no breaking-change record

There is no `CHANGELOG.md` and no migration document. Breaking changes are discoverable only by reading commit history or QA records: the rc1 burst-loss fix (`553b718`, documented in `docs/qa/e1-dryrun.md`), the `/enqueue` endpoint addition, the `config_version`-must-be-a-string gotcha (`docs/guides/site-deployment.md:65` — a boot-crashing trap for fleet templates, documented only in prose), and the updater downgrade fix on `main` that "rides the GA cut". **Recommendation:** start a `CHANGELOG.md` at GA and a `docs/guides/migration.md` for config-file changes; the `config_version` string requirement belongs in the config model's `description`, not only in a deployment footnote.

## G3 — No release provenance for the Python dependency set

Signed-installer provenance is excellent (Ed25519 `.sig` sidecars, custody record in `release-runbook.md` §0.1, `scripts/verify_release_sig.py`). The frozen Python sidecar has none: no SBOM (`.spdx`/`.cyclonedx`) anywhere in the repo outside vendored third-party artifacts, no pip-audit/npm-audit output attached to release assets (they run as CI gates only), and no record of which commit produced `src-tauri/binaries/`. See C3. **Recommendation:** emit `PROVENANCE.json` per freeze and attach an SBOM to the release; `cyclonedx-py` over the frozen tree is a one-line addition to `package_backend.py`.

## G4 — No operator-facing documentation for enabling web authentication at all

Following from C2: the config label, the admin guide, the deployment runbook and the two product specs all point at a wizard step that does not exist, and the only working recipe (env var + `htpasswd`) is framed as the alternative. There is also no documented password-recovery procedure for a panel locked by an empty or unverifiable hash, and no rate-limiting/lockout documentation — `auth.py`'s fail-closed bcrypt path (C2) can produce a permanent lockout with no documented remedy.

---

# Verified accurate (do not re-litigate)

Acknowledged as correct and, in places, exemplary:

1. **`docs/guides/admin-guide.md` config defaults** — `retention_delivered_days=3`, `max_spool_gb=20`, `disk_full_warning_pct=90`, `purge_on_disk_full=false`, and the "undelivered/FAILED are never purged" semantics all match `config/__init__.py:350-368`.
2. **ADR-0007's test claim** — "covered by tests in `tests/test_web_security.py`" is true (`tests/test_web_security.py:115-145` exercises `_enforce_bind_security` directly, including the IPv6 and escape-hatch cases).
3. **`docs/guides/user-guide.md` wizard steps** — the four steps and their validation-gate description exactly match `web/wizard.py:20` and `SetupWizard.tsx:5`. It is the one document that does not over-claim an auth step.
4. **`docs/qa/` evidence records** — honest and self-critical in the way mature QA records should be: `e1-dryrun.md`'s "rc1 must not ship to a clinical site", the burst-loss reproduction, the `config_version` caveat, `b3-tray-leg.md`'s isolation table proving the live node was untouched.
5. **`docs/dev/setup.md`** — the two traps ("frontend tooling runs from `web/`", "`tsc -b`, never `--noEmit`") are accurate, load-bearing, and explain the *why*.
6. **`docs/guides/backup-restore.md` and `systemd/README.md`** — cross-references are correct, commands are runnable, and the auth-on health-probe workaround is stated rather than hidden.
7. **ADR-0004's amendment** — explicitly declining the v1.1 SQLCipher upgrade path and recording the reasoning is exactly how ADR drift should be handled; it is the model for the amendments ADR-0002 and ADR-0005 now need (M2, M1).
8. **`docs/qa/security-review-package.md` §3** — states known gaps (no SQLCipher, single shared password, no log forwarder, operator-supplied TLS) with unusual directness.

---

# Priority order for remediation

1. **C1** — wire the report transports or document the feature as unavailable. Rewrite `reports/__init__.py:9-10`.
2. **C2** — remove every "set via the setup wizard" claim; document the working auth recipe.
3. **C3** — add the frozen-snapshot version assertion + provenance manifest to `package_backend.py`.
4. **H1** — fix the wizard's receiver save path; the K4 evidence trail depends on it.
5. **H2** — add `frame-src` so PDF reports render.
6. **H3, H6** — fix the version string and the two broken `just` recipes; both are one-line fixes with outsized documentation leverage.
7. **M1, M2** — amend ADR-0005 and ADR-0002 to match shipped reality.
8. **G1** — add `response_model` to the remaining 34 operations and adopt an API-versioning ADR before GA.
