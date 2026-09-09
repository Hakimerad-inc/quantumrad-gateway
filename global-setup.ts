/** Playwright globalSetup: seed the isolated gateway and boot it.
 *
 * Prints the seed env into the process env (worker processes inherit it via
 * `process.env` mutation — Playwright forwards the globalSetup process env).
 * globalTeardown (teardown.ts) SIGTERMs the process. */

import { startGateway } from "./e2e/tests/utils";

async function globalSetup() {
  // The seed has already run (repo CI / the npm script pipes it in); we only
  // boot the gateway here. If E2E_CONFIG is missing, seed now via python.
  if (!process.env.E2E_CONFIG) {
    const { execSync } = await import("child_process");
    const out = execSync("uv run python e2e/seed.py", { encoding: "utf8", cwd: __dirname });
    for (const line of out.trim().split("\n")) {
      const [key, value] = line.split("=", 2);
      if (key && value !== undefined) process.env[key] = value;
    }
  }
  await startGateway();
}

export default globalSetup;
