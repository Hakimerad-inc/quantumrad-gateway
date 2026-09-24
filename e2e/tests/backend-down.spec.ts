// b0e74d4 regression guard: a *transport* failure must render BackendDownView,
// not the login screen.
//
// Before the fix, `AuthContext.checkAuth` set `isAuthenticated = false` on a
// 401 and on a connection refusal alike, so an operator whose backend had
// crashed stared at a password prompt, typed a valid password, and read the
// resulting "Network error" as a wrong password. The B3 tray leg hit this in
// a real GNOME session: the shell pointed at a port with no listener.
//
// These specs pin the distinction from both sides — a refused connection is
// unreachable, a 401 is still a sign-in prompt — because either half can be
// broken independently. `page.route` is the only faithful way to produce a
// transport failure against the live gateway: the SPA is served same-origin,
// so a genuinely dead port never serves the bundle and React never mounts.

import { test, expect } from "@playwright/test";

test.describe("Backend unreachable", () => {
  test("a dead backend shows 'Gateway unreachable', not a password form", async ({ page }) => {
    // Abort before first navigation: the auth probe runs on mount.
    await page.route("**/api/system/status", (route) => route.abort());
    await page.goto("/");

    await expect(page.getByRole("heading", { name: "Gateway unreachable" })).toBeVisible();

    // The whole point of the view: no credential can fix a refused connection,
    // so it must not offer one.
    await expect(page.getByLabel("Password")).toBeHidden();
    await expect(page.getByRole("button", { name: "Retry connection" })).toBeVisible();

    // The diagnostic the operator actually needs: which endpoint was probed.
    await expect(page.locator("body")).toContainText("/api/system/status");
  });

  test("a 401 is still the login screen — the two states are distinct", async ({ page }) => {
    // The other half of the same fix: a genuine auth failure must keep
    // presenting the password field. If this regresses, the operator is back to
    // a "Gateway unreachable" page for a mere typo.
    await page.route("**/api/system/status", (route) =>
      route.fulfill({
        status: 401,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Not authenticated" }),
      }),
    );
    await page.goto("/");

    await expect(page.getByRole("heading", { name: "QuantumRAD Gateway" })).toBeVisible();
    await expect(page.getByLabel("Password")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Gateway unreachable" })).toBeHidden();
  });

  test("Retry reconnects once the backend answers again", async ({ page }) => {
    // The recovery path: the first probe fails, BackendDownView renders, and
    // the operator's Retry picks up a backend that has since come back.
    let dead = true;
    await page.route("**/api/system/status", (route) => {
      if (dead) return route.abort();
      return route.fulfill({
        status: 401,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Not authenticated" }),
      });
    });

    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Gateway unreachable" })).toBeVisible();

    dead = false;
    await page.getByRole("button", { name: "Retry connection" }).click();

    // backendUnreachable clears and the app falls through to sign-in.
    await expect(page.getByRole("heading", { name: "QuantumRAD Gateway" })).toBeVisible();
    await expect(page.getByLabel("Password")).toBeVisible();
  });
});
