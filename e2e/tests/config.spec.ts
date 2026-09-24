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
    // c383f3d: the lint runs on every keystroke, so the parse error appears
    // under the editor immediately — not only after a save attempt.
    await expect(page.locator(".lint-error")).toContainText("Invalid JSON");
    // And the document is never sent: `canSave` excludes parse errors, so the
    // gateway cannot be handed a body that would fail server-side.
    await expect(page.getByRole("button", { name: "Save" })).toBeDisabled();
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
    // The app-wide banner appears without a reload: the shell subscribes to the
    // module-scope restart signal.
    await expect(page.getByText("Configuration saved but not yet active")).toBeVisible();
    // Reload: the saved rename is the config on disk now (GET reflects it).
    await page.reload();
    await expect(page.getByLabel("Configuration JSON")).toHaveValue(/"appliance_name":\s*"E2E-Renamed"/, { timeout: 10_000 });
    // b4a4f00: the banner is server-sourced now (`config_pending_restart` —
    // the saved config differs from the one the running components hold), so it
    // survives the reload. The old client-only flag cleared on reload and let an
    // operator believe a saved change was already live.
    await expect(page.getByText("Configuration saved but not yet active")).toBeVisible();
  });

  test("service card is hidden on non-Windows (available=false contract)", async ({ page }) => {
    // Seeded gateway runs on Linux: /api/service returns available:false and
    // ServiceCard renders nothing rather than a dead card.
    await expect(page.getByText("Windows Service")).toHaveCount(0);
  });
});
