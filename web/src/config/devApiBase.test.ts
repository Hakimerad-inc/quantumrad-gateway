/**
 * The dev proxy must target the backend the operator actually started, not a
 * hardcoded port.
 *
 * The proxy defaulted to 8080 while the headless systemd deployment documents
 * 8081, and MERCURE_BACKEND_PORT can put a packaged install anywhere — so
 * `npm run dev` silently proxied /api to a port nothing answered on in every
 * setup that wasn't the desktop default. VITE_API_BASE_URL overrides it.
 */
import { describe, it, expect } from 'vitest';
import { resolveDevApiBase, DEFAULT_DEV_API_BASE } from './devApiBase';

describe('resolveDevApiBase', () => {
  it('defaults to 8080 — the WebUIConfig and Tauri sidecar default', () => {
    expect(resolveDevApiBase({})).toBe(DEFAULT_DEV_API_BASE);
    expect(DEFAULT_DEV_API_BASE).toBe('http://127.0.0.1:8080');
  });

  it('ignores an empty or whitespace override and keeps the default', () => {
    // Vite exposes an unset var as the empty string in some setups; treating
    // that as an override would proxy to a malformed target.
    expect(resolveDevApiBase({ VITE_API_BASE_URL: '' })).toBe(DEFAULT_DEV_API_BASE);
  });

  it('follows VITE_API_BASE_URL so dev matches a non-default backend port', () => {
    // 8081 is the documented headless port (systemd/gateway.env.example).
    expect(resolveDevApiBase({ VITE_API_BASE_URL: 'http://127.0.0.1:8081' })).toBe(
      'http://127.0.0.1:8081',
    );
  });
});
