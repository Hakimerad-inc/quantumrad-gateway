# E2E tests (Playwright)

End-to-end tests drive the **real** QuantumRAD Gateway: a seeded gateway
process (`uv run mercure-gateway --web`) is booted on an isolated port, and
specs exercise the SPA through a real browser — login, queue, wizard,
config, audit, logs, pipeline. No `fetch` mocks, no jsdom.

Per the frontend-testing-best-practices rules: component tests are the
exception, not the default. The surviving vitest files are documented
below.

## Run

```bash
# 1. Seed: generate an isolated config + spool in a temp dir
E2E_SEED_OUT=$(uv run python e2e/seed.py)   # prints KEY=value lines
export $(echo "$E2E_SEED_OUT" | xargs)

# 2. Run (the gateway is spawned by the first spec, torn down at the end)
npx playwright test -c playwright.config.ts
```

`E2E_BASE_URL` (default `http://127.0.0.1:18299`), `E2E_PASSWORD`
(default `e2e-password`) come from the seed output. Playwright itself is
resolved from the machine install (`~` node_modules); no npm dependency is
added to `web/`.

## Isolation

The dev box runs a real mercure stack (gateway on 8080/11115, Orthanc on
11114). The seed hard-codes isolated ports — web **18299**, DICOM receiver
**18113** — and a temp spool dir, so the E2E run never touches them. CI
uses the same defaults.

## Specs

| Spec | Covers |
|---|---|
| `auth.spec.ts` | login flow incl. C1 loop, 401 gating, logout |
| `backend-down.spec.ts` | `b0e74d4` guard: refused connection vs 401, Retry recovery |
| `dashboard.spec.ts` | live status/queue/storage from real endpoints |
| `queue.spec.ts` | seeded studies, state badges, report/retry contracts |
| `wizard.spec.ts` | server-side validation, echo probe, back-preserve |
| `config.spec.ts` | config round-trip, JSON errors, H5 restart banner, service-card hiding |
| `audit-logs.spec.ts` | chained-hash verify, log viewer |
| `pipeline.spec.ts` | SVG flow canvas, live poll hint, empty reports |

## Vitest exceptions (kept deliberately)

| File | Why it stays |
|---|---|
| `api.base.test.ts` | Pure functions (`apiUrl` resolution) — unit-test territory. |
| `api.saveconfig.test.ts` | Pure client logic (error-detail extraction, single fake fetch); the save *flow* is covered E2E. |
| `ui/UpdaterBanner.test.tsx` | Tauri-only plugin surface — the plugin modules only exist inside the Tauri shell; no browser/E2E can exercise the update path. Verified 2026-09-24: `UpdaterBanner` returns `null` outside `inTauri()` and the mount effect early-returns the same way, and there is no `/api/update` route in the web layer, so there is no browser-reachable surface to assert against. The version/channel/downgrade logic (`update.py`) is covered by `tests/test_updater.py`; the in-app banner half stays a Windows-UAT step. |
| `ui/ErrorBoundary.test.tsx` | Deliberate child crash — cannot be triggered safely through E2E without corrupting a real session. |

Everything else that used to be a jsdom component test (Login, App auth,
App disk, Queue, SetupWizard, Pipeline, ServiceCard) is covered — with
more realism — by the specs above, and was removed.

## Files

- `e2e/tests/utils.ts` — gateway process lifecycle + login helper
- `e2e/seed.py` — config/spool seeding via the repo's own `Spool`/`AuditLog` code
- `../playwright.config.ts` — Playwright config (root, `workers: 1`)
