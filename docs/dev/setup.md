# Development environment setup

Getting a fresh clone to a passing test suite. This is the doc the README's
quick start points at when you need more than three commands.

## Prerequisites

| Tool | Version | Why |
| --- | --- | --- |
| [uv](https://docs.astral.sh/uv/) | any recent | Python env + lockfile. `.python-version` pins 3.12. |
| Node.js | **24** | Pinned in `web/.nvmrc`; CI reads the same file. `nvm use` in `web/`. |
| Rust + Cargo | stable | Only for the Tauri shell (`src-tauri/`). Skippable for backend/web work. |
| [just](https://github.com/casey/just) | any recent | Task runner. Optional — every recipe below is a plain command. |

The Node pin is not decorative: CI resolves it from `web/.nvmrc` via
`node-version-file`, so a local Node 18 or 20 diverges from what CI actually
runs. If `nvm` is your manager, `cd web && nvm use` picks it up.

## One-time setup

```bash
just setup          # or: uv sync --all-extras && cd web && npm ci
just precommit-install   # installs the local gates into .git/hooks
```

`uv sync` installs from the **committed `uv.lock`** — exact versions, not
resolver output. It is committed on purpose (see the comment in
`.gitignore`); after changing `pyproject.toml`, run `just update-locks`.

## The daily loop

```bash
just test           # backend pytest + frontend vitest
just lint           # ruff, mypy, eslint, tsc -b
just build-web      # SPA into src/mercure_gateway/web/static/
```

## Two traps worth knowing

**Frontend tooling runs from `web/`, always.** Invoking vitest or tsc from the
repo root fails in a misleading way: vitest resolves its jsdom environment
relative to the config file, so a root invocation silently drops the DOM and
every test dies with `document is not defined` — a convincing-looking total
failure that is really just a wrong working directory. The `just` recipes `cd`
for you; if you run the commands by hand, do the same.

**Typecheck with `tsc -b`, never `tsc --noEmit`.** The root `tsconfig.json`
has `files: []` and only project references, so a bare `--noEmit` checks
nothing and reports success on a bundle that will not build. A green vitest
run hides this too — vitest transpiles through esbuild, which skips
type-checking entirely. This exact combination once shipped a broken
production bundle behind passing tests.

## Regenerating API types

After any change to a pydantic model or a route signature:

```bash
just gen-api
```

The SPA's `web/src/types/api-schema.ts` is derived from the backend's OpenAPI
schema. Forgetting this step leaves the frontend compiling against a backend
shape that no longer exists.

## Gates, local and in CI

The local pre-commit hooks mirror the CI `quality` job (`ruff`, `mypy`) plus
gitleaks and mechanical checks. CI additionally runs:

- **coverage** — `pytest --cov-fail-under=80` (`just test-backend-cov` locally)
- **dependency audit** — pip-audit on Python, `npm audit` on the SPA's
  production deps, `rustsec` on Cargo
- **build verification** — `npm run build` then asserts `index.html` landed in
  the served static dir
- **perf gates** — `scripts/check_perf_gates.py` (§5.6, K8)

## What lives where

```
src/mercure_gateway/   backend (FastAPI + pynetdicom)
web/                   SPA (React + TS + Vite)
src-tauri/             desktop shell; freezes the backend via PyInstaller
tests/                 backend pytest suite
e2e/                   Playwright, against a seeded gateway
scripts/               packaging, perf gates, release tooling
docs/                  ADRs, QA evidence, runbooks
```
