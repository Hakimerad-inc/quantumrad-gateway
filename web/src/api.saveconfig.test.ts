import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// H5 regression: a successful config save must (a) report restart_required from
// the backend, and (b) flip the global "restart required" signal so App can
// render a persistent banner. Running components hold their own config refs, so
// a saved change only applies after a restart (review H5).
describe("saveConfig surfaces restart-required (H5)", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("returns restart_required: true and flips the global signal on success", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        status: "ok",
        message: "Config update saved",
        restart_required: true,
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const api = await import("./api");
    expect(api.isRestartRequired()).toBe(false);

    // Subscribe before saving so we observe the notification fired on success.
    const seen: boolean[] = [];
    const unsub = api.onRestartRequired((v) => seen.push(v));

    const res = await api.saveConfig({ general: { appliance_name: "x" } });
    expect(res).not.toBeNull();
    expect(res?.restart_required).toBe(true);

    // The global signal is now set, and subscribers were notified.
    expect(api.isRestartRequired()).toBe(true);
    expect(seen).toContain(true);
    unsub();
  });

  it("returns null on a failed save and does not flip the signal", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 400 });
    vi.stubGlobal("fetch", fetchMock);

    const api = await import("./api");
    const res = await api.saveConfig({});
    expect(res).toBeNull();
    expect(api.isRestartRequired()).toBe(false);
  });
});
