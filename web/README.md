# web/ — the admin panel SPA

React 18 + Vite 5 + Vitest. Everything here runs through the `just` recipes in
the repo root, which `cd web/` for you; the raw commands are below each one.

This file exists because four things in this directory fail *silently* — each
one has caused a convincing-looking green check on a broken result in this repo.
Setup and the daily loop live in [docs/dev/setup.md](../docs/dev/setup.md);
read that first. What follows is the part you cannot infer from the config
without already knowing it.

## The four traps

**1. The build does not write a `dist/` next to this README.**

`vite.config.ts` sets `build.outDir` to `../src/mercure_gateway/web/static` —
inside the Python package, outside this directory. That directory is served two
ways: the FastAPI web layer mounts it at `/`, and the Tauri shell bundles it as
its embedded frontend (`build.frontendDist` in `src-tauri/tauri.conf.json`; see
[ADR-0002](../docs/adr/ADR-0002-desktop-shell-architecture.md)).

There is no `web/dist/`, and Vite does not create one. If you "fix" the path to
a local `dist/`, or point a server at one, the build succeeds and ships a stale
SPA: the commit you are looking at and the page the operator loads are two
different bundles, and nothing errors. CI catches this only because the
`build-spa` job asserts `src/mercure_gateway/web/static/index.html` exists after
the build.

```bash
just build-web        # == cd web && npm run build   (== tsc -b && vite build)
```

**2. `tsc --noEmit` checks nothing here. `tsc -b` is the command.**

`tsconfig.json` is references-only — `files: []` plus references to
`tsconfig.app.json` (includes `src`) and `tsconfig.node.json` (includes
`vite.config.ts`). A bare `tsc --noEmit` against that root config has no files
to check, so it exits 0 on a tree that will not build.

This is invisible during development because Vitest transpiles through esbuild,
which skips type-checking entirely: tests go green while the bundle is broken.
The two `.tsbuildinfo` files in this directory are `tsc -b` artifacts — leave
them alone, they are the incremental state that makes the command fast.

```bash
just typecheck-web    # == cd web && npx tsc -b
```

**3. Vitest must run from `web/`, or jsdom is dropped.**

`vitest.config.ts` sets `environment: 'jsdom'` and loads `src/test/setup.ts`.
Vitest resolves its config from the working directory. There is no
`vitest.config.*` at the repo root and no root `package.json`, so invoking it
from there picks up no config at all: the environment falls back to Node, the
setup file never loads, and every test fails with `document is not defined`.
That looks like a catastrophic regression; it is a wrong working directory.

```bash
just test-web         # == cd web && npm run test   (== vitest run)
just test             # backend pytest + this suite, in that order
```

**4. `just gen-api` after any backend response model or route signature change.**

`src/types/api-schema.ts` is generated from the backend's OpenAPI export — the
header says so, and it is ~3.9k lines. Editing it by hand is reverted by the
next regeneration. Forgetting to regenerate after a pydantic model changes
leaves the SPA compiling happily against a backend shape that no longer exists;
the last time that happened a response type collapsed to
`Record<string, unknown>` and hid a real bug.

Two drift guards catch it if you forget:

- `just gen-api-check` — regenerates to `/tmp` and diffs against the committed
  file.
- The CI `api-schema.ts drift check` job — the same check, on every push.

```bash
just gen-api          # exports ../mercure-gateway.openapi.json, then regenerates
                      # src/types/api-schema.ts from it
```

## Commands

| Task | Command | Notes |
| --- | --- | --- |
| Install | `cd web && npm ci` | Node 24, pinned in `web/.nvmrc`; CI reads the same file |
| Dev server | `cd web && npm run dev` | Port 5173, proxies `/api` to the backend |
| Typecheck | `cd web && npx tsc -b` | Trap 2 — never `tsc --noEmit` |
| Lint | `cd web && npm run lint` | eslint over `src` |
| Tests | `cd web && npm run test` | Trap 3 — must run from here |
| Build | `cd web && npm run build` | Trap 1 — emits to `../src/mercure_gateway/web/static` |
| API types | `just gen-api` | Trap 4 — after backend model/route changes |

One dev-server detail worth knowing: the `/api` proxy target defaults to
`http://127.0.0.1:8080` (the desktop default, resolved in
`src/config/devApiBase.ts`). If the backend is on another port — the headless
systemd deployment documents 8081, and `MERCURE_BACKEND_PORT` moves a packaged
install — set `VITE_API_BASE_URL` before `npm run dev`, or the proxy points at
a port nothing answers on and every request fails with no useful diagnostic.

## Known deprecated transitive dependencies

`npm install` reports a deprecation for `whatwg-encoding@3.1.1`, a transitive
dev-only dependency reached only through `jsdom` (directly, and via its
`html-encoding-sniffer` dependency). Nothing in `dependencies` reaches it, so
it never ships in the bundle or the Tauri binary, and the warning is cosmetic.

The version itself cannot be bumped — 3.1.1 is the latest published, and its
deprecation notice points at `@exodus/bytes`, a different implementation that is
not an API-compatible drop-in (adopting it would mean patching jsdom's
internals, not editing a version range).

The range *can* be bumped, and was tried: jsdom dropped `whatwg-encoding` in
27.4.0 (the last 27.x release — 27.3.0 still pins `^3.1.1`), so `jsdom ^24` →
`^28` removes it from the tree entirely. That bump was
reverted. jsdom 28's slower environment construction pushes
`SetupWizard.test.tsx > writes receiver fields into the receiver section, not
general` past its 5000ms timeout whenever the vitest cache is cold — which is
every CI run. The test sits at ~3100ms on jsdom 24, so the margin it lost was
already thin. Measured on the full suite, cold cache, no other changes:
jsdom 24 — 3/3 runs green; jsdom 28 — 3/3 runs red at the same assertion.

If you retry this, first raise that test's timeout or split it, and re-run the
full suite cold (`rm -rf node_modules/.vite`) several times — warm runs pass on
both versions and will hide the regression.
