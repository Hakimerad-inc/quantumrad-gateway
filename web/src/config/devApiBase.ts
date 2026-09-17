/**
 * The backend the Vite dev server proxies /api to.
 *
 * The default is 8080 — what `mercure-gateway --web` binds (WebUIConfig) and
 * what the Tauri sidecar expects. But the port is not always 8080:
 *
 *   - the headless systemd deployment documents 8081
 *     (systemd/gateway.env.example: MERCURE_GATEWAY_PORT=8081)
 *   - MERCURE_BACKEND_PORT moves a packaged install so it can coexist with
 *     another service on 8080 — openpacs held the port on 2026-09-14 and
 *     blocked the tray demo (src-tauri/src/lib.rs)
 *   - two gateways on one dev box, the second started with --port
 *
 * Hardcoding 8080 meant `npm run dev` silently proxied to a port nothing
 * answered on in every one of those setups. VITE_API_BASE_URL overrides it;
 * unset keeps the desktop default.
 *
 * Kept as a pure module (no vite import) so it can be unit-tested directly —
 * importing vite.config pulls in esbuild, which fails its own invariant check
 * under the repo's jsdom test environment.
 */

export const DEFAULT_DEV_API_BASE = 'http://127.0.0.1:8080';

/** Read from the environment, falling back to the desktop default. */
export function resolveDevApiBase(env: Record<string, string | undefined> = process.env): string {
  const override = env.VITE_API_BASE_URL;
  if (typeof override === 'string' && override.length > 0) return override;
  return DEFAULT_DEV_API_BASE;
}
