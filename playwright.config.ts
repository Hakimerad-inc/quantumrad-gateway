import { defineConfig } from "@playwright/test";

/**
 * E2E configuration (frontend-testing-best-practices: prefer-e2e-tests).
 *
 * One seeded gateway (e2e/seed.py) is started per worker run via
 * globalSetup/webServer-style wiring in utils.ts — started once by the first
 * spec process through `startGateway()`, reused across spec files, and torn
 * down by the last one (workers=1 keeps the lifecycle deterministic).
 *
 * Ports/spool are fully isolated (18299/18113) so the dev box's real gateway
 * stack (8080/11114/11115) is never touched; see e2e/README.md.
 */
export default defineConfig({
  testDir: "./e2e/tests",
  globalSetup: "./global-setup.ts",
  globalTeardown: "./global-teardown.ts",
  timeout: 30_000,
  expect: { timeout: 5_000 },
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://127.0.0.1:18299",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
});
