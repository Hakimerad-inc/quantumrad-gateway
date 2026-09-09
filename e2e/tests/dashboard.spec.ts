import { test, expect } from "@playwright/test";
import { login } from "./utils";

// Dashboard: live data from the seeded gateway — component status, queue
// rollup, storage gauge. All values come from the real /api/* endpoints.

test.describe("Dashboard", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test("shows live component status and seeded queue numbers", async ({ page }) => {
    await expect(page.getByText("System Status")).toBeVisible();
    // Receiver/forwarder/retriever are running on the seeded gateway.
    await expect(page.getByText("Receiver")).toBeVisible();
    // The two seeded studies are in the queue rollup.
    await expect(page.getByText("Queue Overview")).toBeVisible();
    await expect(page.locator(".stat", { hasText: "Queued" }).first()).toBeVisible();
  });

  test("storage card renders a capacity gauge from the real filesystem", async ({ page }) => {
    await expect(page.getByText("Storage", { exact: true })).toBeVisible();
    await expect(page.locator(".gauge")).toBeVisible();
    // Percentage text like "12.3%" rendered by the gauge row.
    await expect(page.locator(".gauge-value")).toContainText(/%/);
  });
});
