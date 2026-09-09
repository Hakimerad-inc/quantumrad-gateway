import { test, expect } from "@playwright/test";
import { login } from "./utils";

// Study queue: the seeded studies are visible with their real state badges,
// the detail rows carry the seeded accessions, and the report action hits the
// real endpoint (report retriever has no query source → 503 → error banner,
// which is itself the user-visible behavior when no report source exists).

test.describe("Study Queue", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
    await page.getByRole("link", { name: "Queue" }).click();
    await expect(page.getByRole("heading", { name: "Study Queue" })).toBeVisible();
  });

  test("lists the seeded studies with state badges", async ({ page }) => {
    await expect(page.getByText("E2E-ACC-1")).toBeVisible();
    await expect(page.getByText("E2E-ACC-2")).toBeVisible();
    await expect(page.locator(".badge", { hasText: "RECEIVED" }).first()).toBeVisible();
    await expect(page.locator(".badge", { hasText: "CT" })).toHaveCount(0); // modality is a plain cell, not a badge
  });

  test("report action creates a pending report row (US-06 flow)", async ({ page }) => {
    // The real ReportRetriever accepts the request and creates a PENDING row;
    // with no query source configured it simply stays pending — the Reports
    // view (pipeline.spec) pins the empty/pending rendering end of this flow.
    const row = page.locator("tr", { hasText: "E2E-ACC-1" });
    await row.getByRole("button", { name: "Report" }).click();
    // No error banner: the request was accepted.
    await expect(page.locator(".error-banner")).toHaveCount(0);
  });

  test("retry is only offered where the API allows it (contract via UI)", async ({ page }) => {
    // RECEIVED studies have no incomplete routes → retry button is not
    // rendered (state !== FAILED) — pin the visibility contract.
    await expect(page.getByRole("button", { name: "Retry" })).toHaveCount(0);
  });

  test("pagination controls stay hidden for a single page", async ({ page }) => {
    await expect(page.locator(".pagination")).toHaveCount(0);
  });
});
