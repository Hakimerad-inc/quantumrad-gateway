import { test, expect } from "@playwright/test";
import { login } from "./utils";

// Pipeline flow view: SVG canvas renders the live topology from
// /api/pipeline (receiver/spool/forwarder nodes; zero destinations on the
// seeded gateway), plus the auto-refresh toggle.

test.describe("Pipeline", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
    await page.getByRole("link", { name: "Pipeline" }).click();
    await expect(page.getByRole("heading", { name: "Pipeline" })).toBeVisible();
  });

  test("renders the flow diagram with core nodes", async ({ page }) => {
    const svg = page.locator("svg.pipe-canvas");
    await expect(svg).toBeVisible();
    // exact: the node titles also emit <title> tooltips ("Receiver: listening").
    await expect(svg.getByText("Receiver", { exact: true })).toBeVisible();
    await expect(svg.getByText("Spool", { exact: true })).toBeVisible();
    await expect(svg.getByText("Forwarder", { exact: true })).toBeVisible();
  });

  test("live polling populates the updated-at hint", async ({ page }) => {
    // generated_at arrives from the real snapshot; the toolbar hint shows it.
    await expect(page.locator(".hint")).toContainText(/updated|loading/, { timeout: 10_000 });
  });

  test("reports view reflects the queue-requested reports (US-06)", async ({ page }) => {
    // The queue spec clicked "Report" on a seeded study (workers:1 keeps spec
    // order). With no report query source configured, the retriever
    // eventually marks them failed — the visible end-state of requesting a
    // report on a gateway that cannot reach a report PACS.
    await page.getByRole("link", { name: "Reports" }).click();
    await expect(page.getByRole("heading", { name: "Reports" })).toBeVisible();
    await expect(page.locator("tbody tr")).not.toHaveCount(0, { timeout: 10_000 });
    await expect(page.locator(".badge", { hasText: /pending|failed|retrieving/ }).first()).toBeVisible();
  });
});
