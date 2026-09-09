import { test, expect } from "@playwright/test";
import { login } from "./utils";

// Audit chain and operations log views: real chained-hash verification over
// the seeded events, plus the live log viewer backed by the running gateway.

test.describe("Audit", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
    await page.getByRole("link", { name: "Audit" }).click();
    await expect(page.getByRole("heading", { name: "Audit Log" })).toBeVisible();
  });

  test("shows the seeded audit events (STUDY_RECEIVED)", async ({ page }) => {
    await expect(page.locator(".badge", { hasText: "STUDY_RECEIVED" }).first()).toBeVisible();
  });

  test("chain verification passes on the untampered seeded log", async ({ page }) => {
    await page.getByRole("button", { name: "Verify Chain Integrity" }).click();
    await expect(page.getByText("Chain intact")).toBeVisible({ timeout: 10_000 });
  });
});

test.describe("Logs", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
    await page.getByRole("link", { name: "Logs" }).click();
    await expect(page.getByRole("heading", { name: "Operations Log" })).toBeVisible();
  });

  test("shows log lines from the running gateway", async ({ page }) => {
    await expect(page.locator("pre")).not.toBeEmpty({ timeout: 10_000 });
  });

  test("limit selector refetches with the chosen value", async ({ page }) => {
    await page.locator("select").selectOption("50");
    await expect(page.locator(".hint")).toContainText("showing last");
  });
});
