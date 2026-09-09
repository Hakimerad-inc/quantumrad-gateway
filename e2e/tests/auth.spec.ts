import { test, expect } from "@playwright/test";
import { login } from "./utils";

// The auth flow (S06-T8 / review C1): single shared password, signed cookie,
// and no login-loop after a successful POST.

test.describe("Authentication", () => {
  test("wrong password shows the server error and stays on login", async ({ page }) => {
    await page.goto("/");
    await page.getByLabel("Password").fill("definitely-wrong");
    await page.getByRole("button", { name: "Sign In" }).click();
    await expect(page.getByRole("alert")).toContainText(/invalid credentials|login failed/i);
    await expect(page.getByRole("heading", { name: "QuantumRAD Gateway" })).toBeVisible();
    // The password field must still be usable (no deadlock state).
    await expect(page.getByLabel("Password")).toBeEditable();
  });

  test("correct password lands on the dashboard (no C1 login loop)", async ({ page }) => {
    await login(page);
    await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
    await expect(page.getByText("System Status")).toBeVisible();
  });

  test("protected API is rejected without a session cookie", async ({ request }) => {
    const res = await request.get("/api/studies");
    expect(res.status()).toBe(401);
  });

  test("logout returns to the login screen and the session stops working", async ({ page }) => {
    await login(page);
    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(page.getByRole("heading", { name: "QuantumRAD Gateway" })).toBeVisible();
    // The cookie is gone server-side: reloading shows login, not the app.
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "QuantumRAD Gateway" })).toBeVisible();
  });
});
