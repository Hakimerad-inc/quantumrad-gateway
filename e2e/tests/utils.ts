// E2E utilities: shared gateway handle + login/logout helpers.
//
// One gateway process serves the whole run (Playwright `reuseExistingServer`
// also covers the watch-mode case). Specs log in through the real UI via
// `login()`; `resetPasswordGate()` clears the session cookie between specs.

import { spawn } from "child_process";
import { chromium, type Page } from "@playwright/test";

export const BASE_URL = process.env.E2E_BASE_URL ?? "http://127.0.0.1:18299";
export const PASSWORD = process.env.E2E_PASSWORD ?? "e2e-password";

let gatewayProc: ReturnType<typeof spawn> | null = null;
let gatewayLogs: string[] = [];

export function gatewayLogTail(): string {
  return gatewayLogs.slice(-40).join("\n");
}

/** Spawn the real backend (`uv run mercure-gateway --web`) and wait for health. */
export async function startGateway(): Promise<void> {
  const config = process.env.E2E_CONFIG;
  const port = new URL(BASE_URL).port;
  if (!config) throw new Error("E2E_CONFIG is not set — run the seed step first");

  const proc = spawn("uv", ["run", "mercure-gateway", "--config", config, "--web", "--port", port], {
    cwd: process.env.E2E_REPO_ROOT ?? process.cwd(),
    env: process.env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  gatewayProc = proc;
  const collect = (chunk: Buffer) => {
    gatewayLogs.push(chunk.toString());
    if (gatewayLogs.length > 400) gatewayLogs.splice(0, gatewayLogs.length - 400);
  };
  proc.stdout.on("data", collect);
  proc.stderr.on("data", collect);
  proc.on("exit", (code: number | null) => {
    gatewayLogs.push(`[gateway exited with ${code}]`);
  });

  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    try {
      // /api/system/health sits on the authed admin router; a 401 still
      // proves uvicorn is up and serving.
      const res = await fetch(`${BASE_URL}/api/system/health`);
      if (res.status === 401 || res.ok) return;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 300));
  }
  throw new Error(`gateway did not become healthy in 30s\n${gatewayLogTail()}`);
}

/** SIGTERM the backend; main() installs a graceful-shutdown handler. */
export async function stopGateway(): Promise<void> {
  const proc = gatewayProc;
  gatewayProc = null;
  if (!proc || proc.exitCode !== null) return;
  await new Promise<void>((resolve) => {
    proc.once("exit", () => resolve());
    proc.kill("SIGTERM");
    setTimeout(() => {
      try {
        proc.kill("SIGKILL");
      } catch {
        /* already gone */
      }
      resolve();
    }, 5000);
  });
}

/** Log in through the real login page (the C1 flow) and land on the dashboard. */
export async function login(page: Page, password: string = PASSWORD): Promise<void> {
  await page.goto(BASE_URL);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign In" }).click();
  await page.getByRole("heading", { name: "Dashboard" }).waitFor();
}

export { chromium };
