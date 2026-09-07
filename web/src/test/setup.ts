import '@testing-library/jest-dom';
import { vi } from 'vitest';

// Mock window.location for hash-based routing
Object.defineProperty(window, 'location', {
  value: { hash: '', href: 'http://localhost:8080/' },
  writable: true,
});

// Mock fetch globally
global.fetch = vi.fn();
