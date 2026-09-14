import '@testing-library/jest-dom';
import { vi } from 'vitest';

// Mock window.location for hash-based routing
Object.defineProperty(window, 'location', {
  value: { hash: '', href: 'http://localhost:8080/' },
  writable: true,
});

// Mock fetch globally. vi.stubGlobal over bare `global`: the tsc build
// type-checks this file under the DOM lib only (no @types/node), where the
// bare Node `global` identifier does not exist (first CI run, build-SPA job).
vi.stubGlobal("fetch", vi.fn());
