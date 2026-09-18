# Phase 4-4A — Framework & Language Idioms Review

**Scope:** dicom-gateway — FastAPI/Pydantic backend (`src/mercure_gateway`), React 18/TS SPA (`web/src`), Tauri 2/Rust shell (`src-tauri`).
**Base:** `aab94b5` + uncommitted WIP in `web/` (incl. untracked `web/public/axe.js`, `src/mercure_gateway/web/static/axe.js`, `web/src/test/a11y-scan.test.tsx`).
**Verified against:** installed tree (`.venv`, `web/node_modules`, `src-tauri/target`), live `uv.lock` / `package-lock.json` / `Cargo.lock`, `npm audit`, and runtime inspection of the built FastAPI app.

**Findings: 1 Critical · 7 High · 9 Medium · 9 Low = 26**

---

## 1. What is done well (verified, do not "fix")

These are current best practice and were confirmed by reading the code and running it:

- **Handlers are plain `def`, not `async def`** (`web/routes.py:15-17`). Blocking SQLite/filesystem work runs on Starlette's threadpool — the correct FastAPI pattern. The one `async def` (`import_config`, `routes.py:835`) is async only because it needs `await request.form()`.
- **`_SecurityMiddleware` is a hand-written pure-ASGI middleware** (`web/__init__.py:60`), not `BaseHTTPMiddleware`. This is the currently-recommended form (no anyio task-group/streaming quirks, no exception-swallowing).
- **Pydantic v2 used idiomatically**: `Annotated[... | ..., Field(discriminator="type")]` for the destination union (`config/__init__.py:213`), `model_validator(mode="after")`, `Field(pattern=...)`, `TypeAdapter` for env coercion. No `.dict()`/`.json()`/`.construct()` legacy calls anywhere in `src/`.
- **Python 3.12 idioms throughout**: `from __future__ import annotations`, `X | None`, `StrEnum` (`spool/__init__.py:81`, `led.py:41`), `datetime.UTC`, `Mapping`/`Protocol` from `collections.abc`/`typing`.
- **No `@app.on_event`** (deprecated since FastAPI 0.93) anywhere.
- **TS is genuinely strict**: `strict`, `noUnusedLocals`, `noUnusedParameters`, project-reference `tsc -b` build; only 3 escape hatches in the whole SPA (`DestinationsView.tsx:203`, `PipelineView.tsx:206-209`), each documented. `types/api.ts` derives the 5 payload types that *have* server schemas from generated `api-schema.ts`.
- **Rust release profile is properly tuned** (`Cargo.toml`): `lto = true`, `codegen-units = 1`, `opt-level = "s"`, `panic = "abort"`, `strip = true`.
- **Python dependency tree is remarkably current**: fastapi 0.141.1, starlette 1.6.0, pydantic 2.13.5, cryptography 50.0.1, uvicorn 0.52.4, numpy 2.5.2, pytest 9.1.1, mypy 2.3.1, ruff 0.16.5 — all ahead of the `pyproject.toml` floors. Zero yanked distributions in `uv.lock`. No deprecated entries in any Python manifest.

---

## 2. Critical

### C1 — Pydantic models silently discard unknown keys (`extra="ignore"`), which is the structural root cause of the wizard data-loss bug

**Files:** `src/mercure_gateway/config/__init__.py` (all models, e.g. `GeneralConfig:69`, `ReceiverConfig:76`), `web/src/pages/SetupWizard.tsx:79`

No model in the config tree sets `model_config`, so Pydantic v2's default `extra="ignore"` applies to all of them. The wizard's Receiver step therefore writes its validated `ae_title`/`port` into the wrong section and the schema eats them without a peep:

```ts
// web/src/pages/SetupWizard.tsx:79
current.general = { ...(current.general as object || {}), ...data.receiver };
```

`GeneralConfig` has only `appliance_name`, `locale`, `log_level`. The operator completes "Receiver Settings", the wizard reports success, `PUT /config` returns 200, and the receiver keeps its default AE title/port until someone notices. The backend validator (`web/wizard.py:87-92`) validates those fields correctly — the loss is purely a schema-permissiveness + wrong-target bug, and it is invisible at every layer because nothing ever errors.

This is the framework-level generalization of an established finding: **any** future key that the SPA mis-targets, or that a typo'd import/export carries, is dropped the same way. For a config schema that drives a medical-device appliance, unknown keys are always a mistake, never a feature.

**Recommended pattern** — forbid extras on the config schema (this is the fix that removes the whole class, not just this instance):

```python
from pydantic import BaseModel, ConfigDict

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")

class GeneralConfig(_Strict):
    appliance_name: str = "Gateway-CLI-01"
    locale: str = "en"
    log_level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
```

Apply to the root and every nested model. `PUT /config` already returns validation errors as 400 (`routes.py:781`), so an unknown key becomes a visible "Invalid config: extra inputs not permitted" instead of silent data loss. For a rolling rollout, start with `GatewayConfig` + `GeneralConfig` + `ReceiverConfig` (the surfaces the wizard and import touch) and gate the rest behind `config_version = "1.1"`.

Then fix the SPA to write to the right section:

```ts
current.receiver = { ...current.receiver, ...data.receiver };
```

**Migration note:** `apply_env_overrides` (`config/__init__.py:745`) walks `model_fields` and skips unknown env keys already, so it is unaffected. The tests in `tests/` that build configs from dicts will surface any legitimately-expected-but-unnamed keys — run the suite when enabling.

---

## 3. High

### H1 — The codegen pipeline that was built to type the API covers almost nothing: only 5 of 21 exported SPA types derive from the schema

**Files:** `src/mercure_gateway/web/routes.py` (39 operations, 34 without `response_model`), `web/src/types/api.ts:22-26`, `web/src/pages/DestinationsView.tsx:203`

Verified: `app.openapi()` reports 38 paths / 39 operations; `types/api.ts` derives exactly 5 types (`StudySummary`, `StudyPage`, `QueueStats`, `SystemStatus`, `DiskStatus`) from the 62 KB generated `api-schema.ts`, leaving 16 hand-written interfaces. The highest-traffic untyped surface is config:

```ts
// api.ts — untyped by design, because GET /config has no response_model
export function fetchConfig(): Promise<Record<string, unknown>> { ... }
```

Which forces every consumer into casts that the compiler cannot check:

```ts
// DestinationsView.tsx:199-203
setDestinations(withStableIds((cfg.destinations as Destination[]) ?? []));
setForwardingRules(Array.isArray(cfg.forwarding_rules) ? (cfg.forwarding_rules as unknown[]) : []);
```

**Recommended pattern** — add `response_model` to the config endpoints and let the generator replace the hand-written interfaces:

```python
class ConfigView(BaseModel):
    """GET /config — the full config, credentials redacted."""
    config_version: str
    general: GeneralConfig
    receiver: ReceiverConfig
    destinations: list[Destination]
    # ... mirror GatewayConfig; redaction is a serialization concern
    model_config = ConfigDict(extra="forbid")

@router.get("/config", response_model=ConfigView)
def get_config(request: Request) -> ConfigView:
    cfg = _config(request)
    return ConfigView.model_validate(redact_config(cfg.model_dump(mode="json")))
```

Then `fetchConfig(): Promise<components["schemas"]["ConfigView"]>` and the `as Destination[]` casts become real type errors when the backend changes. `scripts/export_openapi.py` + `just gen-api` (see L9) already exist for exactly this — the pipeline is built, it is just under-fed.

### H2 — Vendored 1.3 MB `axe.js` is copied into the production build and shipped to every installed desktop app, referenced by nothing

**Files:** `web/public/axe.js` (1,305,279 B, untracked WIP), `src/mercure_gateway/web/static/axe.js` (build output copy), `web/src/test/a11y-scan.test.tsx:2`

Verified by grep across `web/index.html` and `web/src/`: **zero references to `axe.js`**. The a11y test imports the real package:

```ts
import axe from "axe-core";   // from node_modules — the vendored file is redundant
```

`axe-core` is already a `devDependency`. But anything in `web/public/` is copied verbatim into `outDir` by Vite's `publicDir` behaviour, so this test-only dependency lands in `src/mercure_gateway/web/static/` — the directory the FastAPI app mounts as the production SPA — and therefore inside every Tauri installer and PyInstaller-freeze of the panel. It also appears **twice** (source + built copy).

**Recommended pattern** — delete the vendored file; if a jsdom test ever needs a browser bundle of axe, import from `axe-core` (as the test already does) or reference it via a Vitest `publicDir` scoped to the test config:

```bash
rm web/public/axe.js src/mercure_gateway/web/static/axe.js
```

Add a guard so this cannot regress:

```ts
// web/src/test/build-assets.test.ts
import { describe, it, expect } from "vitest";
describe("production public dir", () => {
  it("does not ship test-only assets", async () => {
    await expect(import("../public/axe.js")).rejects.toThrow(); // or assert the file is absent
  });
});
```

### H3 — 65 vitest tests exist but CI never runs them

**Files:** `.github/workflows/ci.yml` (jobs: `quality`, `dependency-audit`, `test`, `e2e`, `release-build`, …)

Verified: the web jobs run `npm ci`, `npm run build`, and `npm run lint`. **No job runs `npm test`.** The suite that the memory note "green vitest hid a broken bundle" refers to is exactly the suite that is not gated — the `tsc -b` gate caught the broken bundle, but a vitest-only regression (a component that compiles and renders wrong) has no CI signal at all.

**Recommended pattern** — add a job mirroring the pytest one:

```yaml
  web-test:
    name: SPA unit tests (vitest)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version-file: web/.nvmrc
          cache: npm
          cache-dependency-path: web/package-lock.json
      - working-directory: web
        run: npm ci
      - working-directory: web
        run: npm test            # vitest run
      - working-directory: web
        if: always()
        run: npm run build       # keeps the tsc -b gate in the same job
```

Optionally add `--coverage --coverage-reporter=text` with a modest `coverage.thresholds.lines` in `vitest.config.ts`. Run `npm test` in the same job as `npm run build` so a broken bundle fails fast and visibly.

### H4 — Dev-dependency advisories: vite 5.4.21 / vitest 2.1.9 / esbuild 0.21.5 are all behind, and the audit gate deliberately cannot see them

**Files:** `web/package.json:48-49`, `web/package-lock.json`, `.github/workflows/ci.yml:51-54`

`npm audit` in this environment reported (advisory content as returned by the registry):

| Package | Severity | Advisory | Fixed in |
|---|---|---|---|
| `vite` 5.4.21 | **high** | GHSA-4w7w-66w2-5vf9 — path traversal / info disclosure in optimized-deps `.map` handling (`<=6.4.1`); GHSA-v6wh-96g9-6wx3 — `launch-editor` NTLMv2 hash disclosure via UNC paths on Windows | vite **8.3.0** |
| `esbuild` 0.21.5 | moderate | GHSA-67mh-4wv8-2f99 — dev server sends arbitrary requests and reads the response (`<=0.24.2`) | via vite 8 |
| `@vitest/mocker` (⇐ vitest 2.1.9) | moderate | GHSA-82fw-gwwq-j7x9 — path traversal / arbitrary file read via redirect mock (`>=2.1.0 <4.1.11`) | vitest **5.0.1** |

All three are `devDependencies`, which is the correct assessment — none of these reach the built SPA, and the CI comment says so explicitly. Two things still matter:

1. **The NTLMv2/launch-editor advisory is Windows-relevant**, and this product's primary deployment surface is a Windows desktop app where developers run `npm run dev`. It is not purely hypothetical here.
2. **The audit gate is structurally blind to it**: `npm audit --omit=dev --audit-level=high` can never fail on these, and there is no separate dev audit. Combined with H3 (no vitest in CI), the entire frontend dev toolchain is unmonitored.

> **Caveat on the data:** the registry in this environment returned the full advisory set on one call and an empty report (`total: 0`) on two subsequent calls, with an implausible dependency count (`prod: 1, dev: 6`). The advisory IDs, ranges, and `fixAvailable` versions above came from the one complete response and are internally consistent, but **re-run `npm audit` in a trusted registry before acting on severity specifics.** The version-gap conclusion (vite 5 → 8, vitest 2 → 5) does not depend on the advisory data — the majors are simply that far behind.

**Recommended pattern** — two majors at once is the cleanest single jump, and both are dev-only so there is no production blast radius:

```jsonc
// package.json
"devDependencies": {
  "vite": "^8.3.0",
  "vitest": "^5.0.1",
  "@vitejs/plugin-react": "^4.7.0",   // verify 4.7 supports vite 8; bump if a 5.x is published
  // esbuild is transitive — it resolves to a patched version with vite 8
}
```

Then, because both majors break config: `vitest.config.ts` must add `test: { environment: "jsdom" }` is unchanged, but Vite 6+ removed the implicit `server`/`build` defaults that 5.x had; re-verify the `outDir: "../src/mercure_gateway/web/static"` cross-tree build (it writes outside `web/`, which newer Vite warns about — `build.emptyOutDir` is already set, which silences it). Run `npm run build && npm test` after the bump; the `tsc -b` gate will catch any type-level fallout from the newer `rollup`/`esbuild` typings.

Add a dev audit so the toolchain is monitored at all:

```yaml
      - name: Scan SPA dev toolchain (advisories, non-blocking information)
        working-directory: web
        run: npm audit --audit-level=moderate || true   # fail-open initially; tighten once H4 is closed
```

### H5 — Hand-rolled data fetching has a request-ordering race: a slow page-1 response can overwrite a fresh page-2 render

**Files:** `web/src/pages/QueueView.tsx:32-43`, same shape in `AuditView.tsx:12`, `ReportsView.tsx:18`, `LogsView.tsx:11`, `DestinationsView.tsx:199`

The pattern is correct-looking but has no cancellation:

```ts
const load = useCallback(async () => {
  setLoading(true);
  const result = await fetchStudies(page, PAGE_SIZE);   // no AbortController
  setData(result);                                       // unguarded write
}, [page]);
```

If the operator clicks page 2 while page 1's request is still in flight, both complete in arrival order and `setData` from the *older* request wins — the pagination controls say "Page 2" while the table shows page 1. Same hazard in every polling page.

**Recommended pattern** (minimal, no new dependency):

```ts
useEffect(() => {
  const ctrl = new AbortController();
  setLoading(true);
  fetchStudies(page, PAGE_SIZE, { signal: ctrl.signal })
    .then((r) => setData(r))
    .catch((e) => { if (e.name !== "AbortError") setError(String(e)); })
    .finally(() => setLoading(false));
  return () => ctrl.abort();
}, [page]);
```

This requires threading `signal` through `api.ts`'s `apiFetch` (a 3-line change). Alternatively — and this is the framework-level recommendation for a 9-page SPA that repeats this shape in 6 files — adopt **TanStack Query**, which gives cancellation, dedup, stale-while-revalidate, and retry for free, and deletes ~120 lines of duplicated `useState`+`useEffect`+`useCallback` boilerplate:

```ts
const { data, isLoading, error } = useQuery({
  queryKey: ["studies", page],
  queryFn: ({ signal }) => fetchStudies(page, PAGE_SIZE, { signal }),
});
```

For a behind-loopback admin panel with no offline requirement, Query is optional; the AbortController fix is not.

### H6 — No route-level code splitting: the entire SPA is one 200 KB chunk, and there is no `manualChunks`

**Files:** `web/vite.config.ts` (no `build.rollupOptions`), `web/src/App.tsx` (static `import` of all 9 pages), `src/mercure_gateway/web/static/assets/index-BFov4agi.js` (200,853 B)

Verified: no `React.lazy` anywhere in `web/src/`; all 9 pages are eagerly imported in `App.tsx`. The built output is a single `index-*.js` plus a handful of ~1 KB chunks.

For a Tauri desktop shell loading from a local `tauri://` origin this is *mostly* a non-issue — there is no network to wait on, so bundle size buys little UX. Two reasons it still matters here:

- The same bundle is served over HTTP from the FastAPI app for the non-Tauri (headless/service) deployment, where a 200 KB first paint is real.
- The vendored `axe.js` (H2) shows what happens without chunk boundaries: an unrelated asset has no place to live but the main graph.

**Recommended pattern** — lazy-load the page graph behind the existing `ErrorBoundary`, which already handles per-page failure:

```tsx
const QueueView = lazy(() => import("./pages/QueueView"));
const DestinationsView = lazy(() => import("./pages/DestinationsView"));
// ...etc.

<ErrorBoundary key={page}>
  <Suspense fallback={<div className="loading">Loading…</div>}>
    {page === "queue" && <QueueView />}
    ...
  </Suspense>
</ErrorBoundary>
```

Add an explicit vendor split so React itself is cacheable separately:

```ts
// vite.config.ts
build: {
  outDir: "../src/mercure_gateway/web/static",
  emptyOutDir: true,
  rollupOptions: {
    output: {
      manualChunks: { react: ["react", "react-dom"] },
    },
  },
},
```

Keep `Dashboard` eager (it is the landing page) and lazy-load the rest. Measure before/after with `vite build --report` (or `rollup-plugin-visualizer`); if the desktop-only path shows no gain, keep the eager build and just fix H2.

### H7 — `create_app` hardcodes OpenAPI `version="0.1.0"`, and the version-sync script that exists to prevent drift does not cover it

**Files:** `src/mercure_gateway/web/__init__.py:172`, `scripts/sync_version.py:30-36`

Verified at runtime: `app.openapi()["info"]["version"] == "0.1.0"` while the product is `1.1.0-rc3`. `sync_version.py` rewrites `pyproject.toml`, `web/package.json`, `tauri.conf.json`, `Cargo.toml`, and `Cargo.lock` from `__version__` — the FastAPI app is the sixth location and the one an API consumer actually sees. It is also the input to the codegen pipeline (H1), so generated TypeScript carries a `0.1.0` version field that matches nothing.

**Recommended pattern** — derive it, and add the app to the sync set:

```python
from mercure_gateway import __version__

app = FastAPI(
    title="QuantumRAD Gateway API",
    version=__version__,
    description="REST API for the QuantumRAD Gateway web admin panel",
)
```

Then extend `scripts/sync_version.py:_expected()`:

```python
WI = REPO / "src" / "mercure_gateway" / "web" / "__init__.py"
expected[WI] = re.sub(
    r'(?m)^(\s*version = )"[^"]*"',
    rf'\g<1>"{version}"',
    WI.read_text(encoding="utf-8"),
    count=1,
)
```

The `--check` mode (already used by the release guard test) will then keep all six in lockstep.

---

## 4. Medium

### M1 — FastAPI's dependency injection is bypassed: shared state is read by hand off `request.app.state`, typed as `Any`

**Files:** `src/mercure_gateway/web/routes.py:64-74`, used by ~30 handlers

`_spool` and `_config` are the right idea wired the wrong way — they are plain functions called inside handlers rather than declared as dependencies, so FastAPI cannot cache them per request, cannot document them, and cannot type them:

```python
def _spool(request: Request) -> Spool:
    sp: Spool = request.app.state.spool
    return sp

@router.get("/queue/stats", response_model=QueueStats)
def queue_stats(request: Request) -> QueueStats:
    counts = _spool(request).count_states()   # request threaded by hand everywhere
```

**Recommended pattern** — make them real dependencies; FastAPI injects `Request` and caches the result per request automatically:

```python
def get_spool(request: Request) -> Spool:
    return request.app.state.spool

def get_config(request: Request) -> GatewayConfig:
    return request.app.state.config

@router.get("/queue/stats", response_model=QueueStats)
def queue_stats(spool: Spool = Depends(get_spool)) -> QueueStats:
    counts = spool.count_states()
```

Because `create_app` stores the mutable config on `app.state.config` and refreshes it on save (`routes.py:790`), per-request resolution is *required* for correctness — a cached config dependency would serve the startup config forever. The `Depends` form makes that guarantee explicit instead of relying on every handler remembering to call the helper.

Optional next step: `app.state` is untyped (`request.app.state.config` is `Any` under mypy --strict), so a `TypedDict`/dataclass state or `request.state` accessor keeps the type information the models already carry.

### M2 — The destination health monitor's lifecycle lives in the composition root, not in the app — use a `lifespan`

**Files:** `src/mercure_gateway/main.py:382-404`, `src/mercure_gateway/web/__init__.py:160`

`DestinationHealthMonitor` is constructed, started, and stopped by `_run_web_admin` around the `uvicorn.run()` call. It works, but the thread's lifecycle is coupled to a caller that a test or an alternative server (e.g. `uvicorn --factory` or the future `fastapi run`) would not replicate. Nothing in `create_app` knows the monitor exists, so `TestClient`-based tests never start or stop it (they currently get `None` from `app.state.health_monitor` and degrade — which is why the routes all `getattr(..., None)` defensively).

**Recommended pattern**:

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    monitor = DestinationHealthMonitor(app.state.config)
    monitor.start()
    app.state.health_monitor = monitor
    try:
        yield
    finally:
        monitor.stop()

app = FastAPI(title=..., version=__version__, lifespan=lifespan)
```

Then `_run_web_admin` shrinks to `uvicorn.run(app, ...)` and the component-refs (`receiver`, `forwarder`, …) can move to `app.state` assignment before `lifespan` runs. This also removes the defensive `getattr` ladder — a missing state entry becomes a startup error rather than a silent 503.

### M3 — `json.loads(cfg.model_dump_json())` round-trips serialize-then-parse in 5 places where Pydantic v2 has a direct call

**Files:** `web/routes.py:701, 723, 824, 1195`; `audit/__init__.py:345`

```python
data: dict[str, Any] = json.loads(cfg.model_dump_json())   # serialize to str, parse it back
```

This is the pre-v2 idiom for getting JSON-safe primitives out of a model. `model_dump(mode="json")` does exactly that in one pass, without allocating an intermediate string or re-parsing it:

```python
data: dict[str, Any] = cfg.model_dump(mode="json")
```

Behaviour is identical here (custom serializers run in both paths); the change is mechanical and grep-able. `web/__init__.py:177` (`startup_config_json`) and `routes.py:187` (the restart-pending comparison) legitimately want the *string* form and should stay on `model_dump_json()`.

### M4 — Middleware is added in the opposite order from what the code comment claims, so security headers are absent from CORS preflight responses

**Files:** `src/mercure_gateway/web/__init__.py:155-166`, `starlette.applications.Starlette.add_middleware`

The comment says "Security middleware FIRST (runs outermost): headers on every response, CSRF origin check before the CORS handling." Starlette's `add_middleware` does `user_middleware.insert(0, ...)`, so the **last** added is outermost. Verified at runtime:

```
user_middleware order (index 0 = OUTERMOST):
  0: CORSMiddleware
  1: _SecurityMiddleware
```

and the observable consequence — a preflight short-circuits inside CORS and never reaches the headers layer:

```
preflight OPTIONS 200
  content-security-policy: None
  x-frame-options: None
  strict-transport-security: None
GET /api/system/health 200  csp present: True
```

Practical impact is low (a preflight response has no body and no frame to protect), but the CSRF check is *also* not running before CORS handling as intended, and the comment is a load-bearing claim about a security control that is false.

**Recommended pattern** — swap the order so the security layer is truly outermost, and make the comment match:

```python
# Outermost LAST: add_middleware inserts at index 0, so the security layer
# must be added after CORS to wrap it.
app.add_middleware(CORSMiddleware, ...)
app.add_middleware(_SecurityMiddleware, allowed_origins=_ALLOWED_ORIGINS)
```

Add a regression test asserting `type(app.user_middleware[0].cls) is _SecurityMiddleware`, and extend the existing header test to assert CSP on an `OPTIONS` preflight.

### M5 — `fetchConfig` is untyped end-to-end, so every config-editing page casts by hand

**Files:** `web/src/api.ts:245`, `web/src/pages/SetupWizard.tsx:79`, `web/src/pages/DestinationsView.tsx:203`, `web/src/pages/ConfigView.tsx`

This is the frontend face of H1. Because `GET /config` returns `dict[str, Any]`, the generated schema has nothing to say and the SPA degrades to `Record<string, unknown>` + `as` casts at every boundary. Fixing the `response_model` (H1) is the one change that removes all of:

- `(await fetchConfig()) as Record<string, unknown>`
- `cfg.destinations as Destination[]`
- `cfg.forwarding_rules as unknown[]`
- `current.general as object || {}`

and lets `SetupWizard.tsx`'s `data.receiver` merge be checked against a real `ReceiverConfig` type — which would have surfaced the C1 wrong-section bug at compile time.

### M6 — Rust: two majors of `png` and two majors of `reqwest` compile because the manifest lags Tauri's crates

**Files:** `src-tauri/Cargo.toml` (`png = "0.17"`, `reqwest = "0.12"`), `src-tauri/Cargo.lock`

Verified from the lockfile's dependency sections:

| Direct dep | Resolved | Also in tree (pulled by Tauri) |
|---|---|---|
| `png` | **0.17.16** (mercure-gateway, `ico 0.5.0`, `tauri-codegen 2.6.3`) | 0.18.1 (`muda 0.19.3`, `tray-icon 0.24.2`) |
| `reqwest` | **0.12.28** (mercure-gateway) | 0.13.4 (`tauri 2.11.5`, `tauri-plugin-updater 2.11.0`) |

`load_icon` (`src/lib.rs:43`) is the only `png` consumer and its API surface (`Decoder::new`, `read_info`, `next_frame`, `output_buffer_size`) is unchanged between the two majors.

**Recommended pattern**:

```toml
[dependencies]
png = "0.18"      # matches muda/tray-icon — drops one of two compiled copies
reqwest = "0.13"  # matches tauri + tauri-plugin-updater — fully dedupes
```

`reqwest 0.13` keeps `blocking` + `json` features; verify `Client::builder().timeout(...)` semantics are unchanged (they are). `png 0.17` remains transitive via `ico`/`tauri-codegen`, so the tree does not go to a single copy — but the crate's own contribution and one duplicate go away, which matters in a binary that is distributed as an installer.

### M7 — `import_config` is `async def` and then does blocking validation + file write on the event loop

**Files:** `src/mercure_gateway/web/routes.py:835-890`

The `async` is correct and required (`await request.form()`), but everything after it — `GatewayConfig.model_validate(restored)` and `save_config(...)` writing to disk synchronously — runs on the event loop it just told FastAPI it would not block. The file write is the worst offender: a slow disk (a USB dongle, which is this product's deployment target) stalls every endpoint in the process.

**Recommended pattern** — offload the blocking tail, keeping only the async parts in the coroutine:

```python
from fastapi.concurrency import run_in_threadpool

@router.post("/config/import")
async def import_config(request: Request) -> dict[str, Any]:
    form = await request.form()
    file = form.get("file")
    ...
    content = await file.read()
    payload = json.loads(content.decode("utf-8"))
    ...
    restored = _restore_redacted_secrets(payload, _config(request))
    return await run_in_threadpool(_persist_imported_config, request, restored)
```

where `_persist_imported_config` does the `model_validate` + `save_config` + `app.state` update and returns the response dict. Note this route also has no `response_model` — fold it into the H1 fix.

While there: the local `from starlette.datastructures import UploadFile as StarletteUploadFile` (`routes.py:842`) is imported and never used as a type (the code duck-types with `isinstance(file, str)` / `hasattr(file, "read")`). It is a dead import; delete it or use it as the real annotation.

---

## 5. Low

### L1 — `AuthContext` recreates its value object and all three closures on every render

**Files:** `web/src/context/AuthContext.tsx:74-90`

```tsx
const checkAuth = async () => { ... };          // new identity every render
const login = async (password) => { ... };
const logout = async () => { ... };
return <AuthContext.Provider value={{ isAuthenticated, isLoading, backendUnreachable, login, logout, checkAuth }}>
```

Every `AuthProvider` re-render hands consumers a fresh object, so **all** consumers re-render regardless of what changed. Today the provider's state changes rarely, so this costs little — but the pattern is the one that bites as a context grows.

```tsx
const login = useCallback(async (password: string): Promise<LoginResult> => { ... }, []);
const logout = useCallback(async () => { ... }, []);
const checkAuth = useCallback(async () => { ... }, []);
const value = useMemo(
  () => ({ isAuthenticated, isLoading, backendUnreachable, login, logout, checkAuth }),
  [isAuthenticated, isLoading, backendUnreachable, login, logout, checkAuth],
);
```

The `useCallback` deps are empty because all three only read state via the setters, which are stable.

### L2 — `setInterval` polling can overlap and land out of order

**Files:** `web/src/pages/LogsView.tsx:31-37` (also `web/src/ui/ServiceCard.tsx:23`)

```tsx
const id = setInterval(load, 5000);
```

If a fetch takes >5 s (a large operations log over a slow loopback), requests stack and resolve in arrival order, so an older snapshot can overwrite a newer one. Guard with an in-flight flag or a self-rescheduling `setTimeout`:

```tsx
useEffect(() => {
  if (!auto) return;
  let cancelled = false;
  const tick = async () => {
    await load();
    if (!cancelled) timer = setTimeout(tick, 5000);
  };
  let timer = setTimeout(tick, 5000);
  return () => { cancelled = true; clearTimeout(timer); };
}, [auto, load]);
```

This also turns "5 s between starts" into "5 s between completions", which is the intended cadence under load.

### L3 — `numpy` is declared as a direct runtime dependency but is never imported by project code

**Files:** `pyproject.toml:12`, `uv.lock`

Repo-wide grep (`src/`, `tests/`, `scripts/`, `e2e/`) finds `numpy` only in `pyproject.toml`. `uv.lock` shows it is required by `pylibjpeg-rle 2.2.0`, which pydicom's pixel-data path uses for RLE decompression — so it must be *installed*, but it need not be *declared*: uv resolves it transitively.

Two side notes: the floor `numpy>=1.26` reads as "we target the 1.x line" (1.26 is EOL) while the lock correctly resolves 2.5.2; and `pylibjpeg-rle>=2.2.0` is only referenced by `scripts/package_backend.py:48` (the PyInstaller hidden-imports list), never imported either — that one is legitimately needed as a pydicom plugin and should stay.

```toml
dependencies = [
    "pydantic>=2.7",
    "pynetdicom>=3.0.4",
    # ... numpy removed — pylibjpeg-rle brings it in for RLE decompression
]
```

If you would rather keep it explicit as documentation that pydicom's `pixel_array` path needs numpy, bump the floor to `>=2.0` so the constraint stops implying the EOL line.

### L4 — Rust: the `log` crate is a declared dependency that the shell never uses

**Files:** `src-tauri/Cargo.toml` (`log = "0.4"`), `src-tauri/src/lib.rs`

Grep for `log::` / `info!` / `warn!` / `error!` in `src-tauri/src/`: **zero matches.** All diagnostics are `eprintln!` (`lib.rs:262, 268`) or `let _ =` discards. `log` is 0.4.34 in the tree (pulled by Tauri anyway, so the manifest entry is redundant) and there is no logger initialized, so even wiring `log::error!` today would print nothing.

Either remove it from the manifest, or — better for a tray app that runs a background poller — actually use it:

```rust
use log::{error, info};

Err(e) => error!("mercure-gateway backend failed to start: {e}"),
```

plus `tauri::Builder::default().plugin(tauri_plugin_log::Builder::new().build())` to get log → OS logging (useful on Windows, where `eprintln` on a GUI app goes nowhere).

### L5 — Rust: tray state is `u8` constants + a write-only `AtomicU8`; model it as an enum

**Files:** `src-tauri/src/lib.rs:14-16, 169-173, 226-236`

```rust
const TRAY_IDLE: u8 = 0;
const TRAY_SENDING: u8 = 1;
const TRAY_ERROR: u8 = 2;
```

The idiomatic form is a `#[repr(u8)]` enum, which makes `state_label`/`apply_tray_state` exhaustive matches instead of `_ =>` catch-alls:

```rust
#[repr(u8)]
enum TrayState { Idle = 0, Sending = 1, Error = 2 }

let state = Arc::new(AtomicU8::new(TrayState::Idle as u8));
// ...
state.store(TrayState::Sending as u8, Ordering::Relaxed);
match TrayState::try_from(atomic.load(Ordering::Relaxed)).unwrap_or(TrayState::Idle) { ... }
```

Separately: `let state = Arc::new(...)` is created, cloned into the poll thread, written every 5 s — and **never read** (`state` itself is unused after the clone). Either expose the state to the SPA via a Tauri command (which would let the webview show "attention needed" without polling), or delete the shared atomics and keep the value local to the poll thread. Clippy would flag the needless clone.

### L6 — `queue_stats` maps state names with string literals while a `StrEnum` exists for them

**Files:** `src/mercure_gateway/web/routes.py:483-490`, `src/mercure_gateway/spool/__init__.py:81`

```python
counts = _spool(request).count_states()
queued=counts.get("QUEUED", 0),   # literal; StudyState.QUEUED exists
```

Because `StudyState` is a `StrEnum`, its members *are* `str` for hashing and equality, so the fix is zero-cost and type-checked:

```python
queued=counts.get(StudyState.QUEUED, 0),
sending=counts.get(StudyState.SENDING, 0),
```

If a state is ever renamed, `mypy`/ruff catch it instead of the API silently returning 0 for a category that has studies in it.

### L7 — Rust manifest targets edition 2021 / MSRV 1.77.2

**Files:** `src-tauri/Cargo.toml:6-7`

Edition 2024 (stabilized in Rust 1.85) is available and Tauri 2.11 compiles under it. Gains relevant to this crate: `let-else` and `let-chains` in conditions (both usable on 2021 with a newer compiler, but idiomatic in 2024), and `unsafe` attribute syntax. The MSRV floor of 1.77.2 is also old enough that it predates several borrow-checker improvements `find_backend`'s iterator chains would benefit from.

```toml
edition = "2024"
rust-version = "1.85"
```

Low value, no urgency — do it alongside M6 since both touch the manifest and require a full rebuild anyway. Verify the Tauri MSRV requirement (2.11 needs ≥1.77; edition 2024 needs 1.85 for the *build*, which is fine on CI's `rust-toolchain`).

### L8 — `eslint` 9.39.5 is marked deprecated in the lockfile; `globals` is a major behind

**Files:** `web/package-lock.json` (`node_modules/eslint` → `"deprecated": "This version is no longer supported"`; also `whatwg-encoding` 3.1.1, a transitive of jsdom)

These are the only two deprecated entries in the npm tree. `globals ^15.15.0` while 16.x ships (16 is the line that matches eslint 10's environments). Neither breaks anything today.

```jsonc
"eslint": "^10.0.0",       // verify the flat-config story is unchanged; eslint 10 keeps tseslint compatible
"globals": "^16.0.0",
```

Run after H4's vite/vitest bump so the frontend toolchain moves once.

### L9 — `just gen-api` cannot work as written: the schema goes to stdout while the next line reads a file the script never produces

**Files:** `justfile:95-97`, `scripts/export_openapi.py:39-44`

```make
gen-api:
	uv run python scripts/export_openapi.py      # writes JSON to stdout — discarded
	cd web && npx openapi-typescript ../mercure-gateway.openapi.json -o src/types/api-schema.ts
```

`export_openapi.py` ends with `json.dump(app.openapi(), sys.stdout, indent=2)`. Its own docstring documents the correct invocation (`> /tmp/openapi.json`). The justfile's first line never redirects, so line 2 reads a nonexistent file. The pipeline this task sequence was built for (H1) is broken at its entry point.

```make
gen-api:
	uv run python scripts/export_openapi.py > mercure-gateway.openapi.json
	cd web && npx openapi-typescript ../mercure-gateway.openapi.json -o src/types/api-schema.ts
```

Better: have the script write the file itself (with an `--out` argument, defaulting to stdout for scripting), and add `git diff --exit-code src/types/api-schema.ts` as a CI guard so a backend change that is not regenerated fails the build — that is what turns H1's typing promise into an enforced contract.

---

## 6. Cross-cutting recommendation: the config surface is one fix, four findings

C1 (silent drop), H1 (untyped API), M5 (SPA casts), and H7 (version drift) are all symptoms of one root choice: **the config schema is the product's contract, but nothing in the stack enforces it.** The ordered fix:

1. `extra="forbid"` on the config models (C1) — makes invalid input visible.
2. `response_model` on `GET`/`PUT /config` and `/config/warnings` (H1) — makes the contract machine-readable.
3. Regenerate `api-schema.ts` via a fixed `just gen-api` (L9) + a CI drift guard — makes the contract enforced.
4. Delete the hand-written interfaces in `types/api.ts` as the generator replaces them; the casts in the SPA disappear with them (M5).
5. `version=__version__` in `create_app` (H7) + the sync script — makes the contract versioned.

Steps 1-2 are the substantive work; 3-5 are an afternoon each and compound.

---

## 7. Summary table

| # | Sev | Finding | Primary file:line |
|---|---|---|---|
| C1 | Critical | `extra="ignore"` silently drops unknown config keys (wizard writes to wrong section) | `config/__init__.py:69`; `SetupWizard.tsx:79` |
| H1 | High | Codegen covers 5/21 types; 34/39 ops lack `response_model` | `web/routes.py`; `types/api.ts:22` |
| H2 | High | 1.3 MB vendored `axe.js` shipped in production build, unreferenced | `web/public/axe.js` |
| H3 | High | 65 vitest tests, zero CI execution | `.github/workflows/ci.yml` |
| H4 | High | vite 5 / vitest 2 / esbuild 0.21 advisories; audit gate is `--omit=dev` | `web/package.json:48` |
| H5 | High | Data-fetch race: stale response can overwrite fresh render | `QueueView.tsx:32` |
| H6 | High | No `React.lazy`/`manualChunks`; one 200 KB chunk | `App.tsx`; `vite.config.ts` |
| H7 | High | OpenAPI `version="0.1.0"`; sync script misses it | `web/__init__.py:172` |
| M1 | Medium | DI bypassed; `request.app.state` read by hand, untyped | `web/routes.py:64` |
| M2 | Medium | Health monitor lifecycle outside the app; no `lifespan` | `main.py:382` |
| M3 | Medium | `json.loads(model_dump_json())` instead of `model_dump(mode="json")` | `web/routes.py:701` |
| M4 | Medium | Middleware order inverted vs comment; no headers on preflight | `web/__init__.py:155` |
| M5 | Medium | `fetchConfig` untyped → casts across the SPA | `api.ts:245` |
| M6 | Medium | Duplicate `png`/`reqwest` majors in the Rust tree | `src-tauri/Cargo.toml` |
| M7 | Medium | `async` handler does blocking validate + file write on the loop | `web/routes.py:835` |
| L1 | Low | `AuthContext` value not memoized | `AuthContext.tsx:74` |
| L2 | Low | `setInterval` polling can overlap/out-of-order | `LogsView.tsx:31` |
| L3 | Low | `numpy` direct dep unused by project code; EOL-looking floor | `pyproject.toml:12` |
| L4 | Low | `log` crate declared, never used; `eprintln` only | `src-tauri/Cargo.toml` |
| L5 | Low | Tray state as u8 consts; write-only `AtomicU8` | `src-tauri/src/lib.rs:14` |
| L6 | Low | State-name literals where a `StrEnum` exists | `web/routes.py:483` |
| L7 | Low | Edition 2021 / MSRV 1.77.2 | `src-tauri/Cargo.toml:6` |
| L8 | Low | `eslint` 9.39.5 deprecated in lockfile; `globals` a major behind | `web/package-lock.json` |
| L9 | Low | `just gen-api` discards the schema it then tries to read | `justfile:95` |
