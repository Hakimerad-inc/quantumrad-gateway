import { test, expect } from "@playwright/test";
import { login } from "./utils";

// Config editor: round-trip load, JSON error surfacing, and the save flow
// including the persistent restart banner (review H5). The editor operates
// on the gateway's real config file (e2e-gateway.json in the temp dir).

test.describe("Config", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
    await page.getByRole("link", { name: "Config" }).click();
    await expect(page.getByRole("heading", { name: "Configuration" })).toBeVisible();
    await expect(page.getByLabel("Configuration JSON")).toHaveValue(/\{/, { timeout: 10_000 });
  });

  test("loads the real config with the appliance name", async ({ page }) => {
    await expect(page.getByLabel("Configuration JSON")).toHaveValue(/"appliance_name"/);
  });

  test("invalid JSON blocks the save and shows the parse error", async ({ page }) => {
    const editor = page.getByLabel("Configuration JSON");
    await editor.fill("{ not json");
    await page.getByRole("button", { name: "Save" }).click();
    await expect(page.locator(".error-banner")).toContainText("Invalid JSON");
  });

  test("saving a rename persists and arms the restart banner", async ({ page }) => {
    const editor = page.getByLabel("Configuration JSON");
    // Rename the appliance through the real PUT /api/config path. editor.fill
    // sets the value in one shot; a trailing change event marks dirty and
    // enables Save.
    const raw = await editor.inputValue();
    const renamed = raw.replace(/"appliance_name":\s*"[^"]*"/, '"appliance_name": "E2E-Renamed"');
    await editor.click();
    await page.keyboard.press("ControlOrMeta+a");
    await page.keyboard.type("{");
    // Dirty now — undo the typing, then type the full JSON so React sees real
    // input events (fill alone can leave dirty=false with empty delta).
    await page.keyboard.press("Backspace");
    await editor.fill(renamed);
    await expect(page.getByRole("button", { name: "Save" })).toBeEnabled({ timeout: 5_000 });
    await page.getByRole("button", { name: "Save" }).click();
    await expect(page.getByText("Config saved — restart the gateway")).toBeVisible();
    // The H5 banner appears app-wide without a reload.
    await expect(page.getByText("Configuration changed — restart the gateway")).toBeVisible();
    // Reload: the saved rename is the config on disk now (GET reflects it);
    // the restart banner is gone on the fresh page load because the signal is
    // module-scope (session-only).
    await page.reload();
    await expect(page.getByLabel("Configuration JSON")).toHaveValue(/"appliance_name":\s*"E2E-Renamed"/, { timeout: 10_000 });
  });

  test("service card is hidden on non-Windows (available=false contract)", async ({ page }) => {
    // Seeded gateway runs on Linux: /api/service returns available:false and
    // ServiceCard renders nothing rather than a dead card.
    await expect(page.getByText("Windows Service")).toHaveCount(0);
  });
});
