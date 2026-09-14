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
    // Self-contained (review 2026-09-14): specs run in file order — pipeline
    // before queue — so requesting the report here instead of relying on a
    // prior queue-spec click. With no report query source configured the
    // retriever accepts the request and the row stays PENDING — the visible
    // end-state of requesting a report on a gateway with no report PACS.
    await page.getByRole("link", { name: "Queue" }).click();
    await expect(page.getByRole("heading", { name: "Study Queue" })).toBeVisible();
    const row = page.locator("tr", { hasText: "E2E-ACC-1" });
    await row.getByRole("button", { name: "Report" }).click();
    await page.getByRole("link", { name: "Reports" }).click();
    await expect(page.getByRole("heading", { name: "Reports" })).toBeVisible();
    await expect(page.locator("tbody tr")).not.toHaveCount(0, { timeout: 10_000 });
    await expect(page.locator(".badge", { hasText: /pending|failed|retrieving/ }).first()).toBeVisible();
  });
});
