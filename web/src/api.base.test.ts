import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// C2 regression: inside the packaged Tauri shell the SPA is loaded from a
// `tauri://localhost` origin, so API calls must target the backend on
// 127.0.0.1:8080 explicitly instead of using relative (same-origin) URLs.
describe("api base URL resolution (C2)", () => {
  beforeEach(() => {
    vi.resetModules();
    delete (window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("uses relative (same-origin) URLs by default", async () => {
    const api = await import("./api");
    expect(api.API_BASE).toBe("");
    expect(api.apiUrl("/api/status")).toBe("/api/status");
  });

  it("targets 127.0.0.1:8080 when running inside the Tauri shell", async () => {
    (window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    const api = await import("./api");
    expect(api.API_BASE).toBe("http://127.0.0.1:8080");
    expect(api.apiUrl("/api/status")).toBe("http://127.0.0.1:8080/api/status");
  });

  it("honors a VITE_API_BASE_URL build-time override", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "http://gw.example:9999");
    const api = await import("./api");
    expect(api.API_BASE).toBe("http://gw.example:9999");
    expect(api.apiUrl("/api/status")).toBe("http://gw.example:9999/api/status");
  });
});
