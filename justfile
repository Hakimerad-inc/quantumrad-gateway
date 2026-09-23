# Task runner for the multi-toolchain dev loop.
# Install: https://github.com/casey/just  (cargo install just, or a package
# manager of your choice). just works on Windows natively — unlike make —
# which matters for a project whose operators run Windows.
#
# Everything runs through `uv run` / `npm run` so the commands you type here
# are the same ones CI executes in .github/workflows/ci.yml.

# Default: list the recipes. `just` with no argument prints this.
default:
    @just --list

# ── One-time setup ──────────────────────────────────────────────────────

# Install everything: Python deps from the committed uv.lock, then the SPA.
# CI runs `uv sync --all-extras` + `npm ci`; this mirrors it.
setup:
    uv sync --all-extras
    cd web && npm ci

# Update the lockfiles after a dependency change (pyproject.toml or
# package.json). uv.lock is committed on purpose — it pins exact versions.
update-locks:
    uv lock
    cd web && npm update --package-lock-only

# ── Tests ───────────────────────────────────────────────────────────────

# Backend test suite. `--no-file-parallelism` is NOT needed here — that flag
# only matters for the vitest worker crash under jsdom, not pytest.
test: test-backend test-web

test-backend:
    uv run pytest

# Same suite with the coverage gate CI enforces (>= 80%, pyproject.toml).
test-backend-cov:
    uv run pytest --cov=mercure_gateway --cov-fail-under=80

# The SPA test suite. MUST run from web/ — vitest resolves its jsdom
# environment relative to the config file, and invoking it from the repo
# root silently drops the DOM and fails every test with "document is not
# defined". The `cd` is load-bearing, not cosmetic.
test-web:
    cd web && npm run test

# Performance gates (§5.6, K8) — the perf-gates CI job runs this same script.
# Synthetic mode: the scripted handler does no I/O, so this measures the
# forwarder's queue/claim/route bookkeeping only (review P1-7). Fast and
# stable enough for every push — and it proves nothing about real throughput,
# which is exactly why the mode below exists.
perf:
    uv run python scripts/check_perf_gates.py

# The same batch delivered by the real DICOMHandler through an actual socket
# to a local C-STORE SCP, reading real .dcm files off a temp spool and
# counting instances that arrived on the wire. The synthetic number and the
# real one differ by ~300×, and only the real one means anything. This is the
# integration path — also run by CI's perf-gates-real job, which reports on
# every push but is only required on main because a socket-bound measurement
# flaps on a loaded shared runner (see the integration marker in the test
# suite).
perf-gates-real:
    uv run python scripts/check_perf_gates.py --real

# Playwright E2E against a seeded gateway. Requires the SPA build first.
e2e: build-web
    uv run python e2e/seed.py > /tmp/e2e-env.txt
    cd web && npx playwright install --with-deps chromium
    npx playwright test -c playwright.config.ts --reporter=line

# ── Lint & typecheck ────────────────────────────────────────────────────

# All four gates CI runs in its `quality` job, plus the SPA typecheck.
lint: lint-py lint-web typecheck-web

lint-py:
    uv run ruff check .
    uv run mypy .

lint-web:
    cd web && npm run lint

# `tsc -b`, not `tsc --noEmit`. The root tsconfig has `files: []` and only
# project references, so a bare --noEmit checks NOTHING and reports success
# on a bundle that will not build. A green vitest run hides this: vitest
# transpiles via esbuild, which skips type-checking entirely.
typecheck-web:
    cd web && npx tsc -b

# Apply ruff's formatter-safe fixes. NOT a gate — run it by hand.
fix:
    uv run ruff check --fix .
    cd web && npx eslint --fix src

# ── Build ───────────────────────────────────────────────────────────────

# Build the SPA into src/mercure_gateway/web/static/ (vite emits there; the
# backend serves that directory). CI verifies index.html lands in it.
build-web:
    cd web && npm run build

# Freeze the backend sidecar (PyInstaller onedir) for the Tauri bundle.
build-backend:
    uv run python scripts/package_backend.py

# Refresh a locally-frozen sidecar so it matches this tree. A stale snapshot
# is invisible from the outside — it boots, answers health checks, and runs
# an older backend. The committed one was 1.1.0-rc1 and predates
# _enforce_bind_security, so an installer built from it would have booted an
# unauthenticated admin panel on the LAN (review P0-2). Run this after
# `git pull` before `cargo tauri build`.
refresh-sidecar:
    uv run python scripts/package_backend.py --keep-dist
    @echo "frozen sidecar refreshed; PROVENANCE.json written beside it"

# Regenerate the SPA's API types from the backend OpenAPI schema, after any
# change to a pydantic model or a route signature. Forgetting this leaves the
# frontend compiling against a backend that no longer has that shape.
gen-api:
    uv run python scripts/export_openapi.py --output mercure-gateway.openapi.json
    cd web && npx openapi-typescript ../mercure-gateway.openapi.json -o src/types/api-schema.ts

# CI guard: fail if the committed api-schema.ts is not what the current
# backend exports. Catches a route or model change that never regenerated the
# SPA's types — the drift that let a Record<string, unknown> paper over a
# real mismatch (review P1-9).
gen-api-check:
    uv run python scripts/export_openapi.py --output /tmp/api-schema-check.json
    cd web && npx openapi-typescript /tmp/api-schema-check.json -o /tmp/api-schema-check.ts
    @diff -u src/types/api-schema.ts /tmp/api-schema-check.ts && echo "api-schema.ts is up to date"

# ── Pre-commit ──────────────────────────────────────────────────────────

# Install the hooks into .git/hooks/ (run once after cloning).
precommit-install:
    uv run pre-commit install

# Run the hooks against every file, as CI's pre-commit.ci equivalent would.
precommit:
    uv run pre-commit run --all-files
