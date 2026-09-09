import { test, expect } from "@playwright/test";
import { login } from "./utils";

// Setup wizard: real server-side validation and C-ECHO probe contract.
// The seeded gateway has an active DICOM receiver on port 18113 — a valid
// receiver step passes against it; the destinations step rejects malformed
// hosts before anything is saved.
//
// Note: the receiver inputs are wrapped in <label> without htmlFor/id, so
// Playwright's getByLabel does not match them; fill by placeholder-adjacent
// role selectors instead (accessible-selector rule: role > label > text).

test.describe("Setup Wizard", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
    await page.getByRole("link", { name: "Setup" }).click();
    await expect(page.getByRole("heading", { name: "Setup Wizard" })).toBeVisible();
  });

  async function fillReceiver(page: import("@playwright/test").Page, aet: string, port: string) {
    await page.locator("label", { hasText: "AE Title" }).getByRole("textbox").fill(aet);
    await page.locator("label", { hasText: "Port" }).getByRole("spinbutton").fill(port);
  }

  test("rejects an out-of-range receiver port with server-side errors", async ({ page }) => {
    await fillReceiver(page, "E2EGW", "70000");
    await page.getByRole("button", { name: "Next" }).click();
    // The wizard step banner shows the POST /wizard/validate errors (review M8).
    await expect(page.locator(".error-banner")).toBeVisible();
    // Still on the receiver step (step chip stays "current").
    await expect(page.locator(".step.current", { hasText: "Receiver Settings" })).toBeVisible();
  });

  test("valid receiver settings advance to destinations", async ({ page }) => {
    await fillReceiver(page, "E2EGW", "18113");
    await page.getByRole("button", { name: "Next" }).click();
    await expect(page.locator(".step.current", { hasText: "Destinations" })).toBeVisible();
  });

  test("destination echo probe surfaces a failure badge for a dead host", async ({ page }) => {
    await fillReceiver(page, "E2EGW", "18113");
    await page.getByRole("button", { name: "Next" }).click();
    await expect(page.locator(".step.current", { hasText: "Destinations" })).toBeVisible();
    // Start on the empty destinations list.
    await page.getByRole("button", { name: "+ Add Destination" }).click();
    await page.getByPlaceholder("Name").fill("Dead PACS");
    await page.getByPlaceholder("Host").fill("127.0.0.1");
    await page.getByPlaceholder("Port").fill("1"); // nothing listens here
    await page.getByPlaceholder("AET").fill("NOPE");
    await page.getByRole("button", { name: "Echo" }).click();
    // Port 1 on loopback → ConnectionRefused → status "refused" → red badge.
    await expect(page.locator(".badge.red", { hasText: /refused|error|timeout/ })).toBeVisible({ timeout: 15_000 });
  });

  test("back navigation preserves entered receiver values", async ({ page }) => {
    await fillReceiver(page, "KEEPME", "18113");
    await page.getByRole("button", { name: "Next" }).click();
    await expect(page.locator(".step.current", { hasText: "Destinations" })).toBeVisible();
    await page.getByRole("button", { name: "Back" }).click();
    await expect(page.locator("label", { hasText: "AE Title" }).getByRole("textbox")).toHaveValue("KEEPME");
  });
});
