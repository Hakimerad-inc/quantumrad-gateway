/** AuthContext tests — the transport-vs-auth distinction (B3 tray leg).
 *
 * Before this fix, a connection-refused error in `checkAuth` set
 * `isAuthenticated = false` and the app rendered the login screen: an
 * operator with a dead backend stared at a password prompt. These pin the
 * three-way split: reachable+authorized, reachable+401, and unreachable.
 */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { AuthProvider, useAuth } from './AuthContext';

// Minimal consumer: surfaces the context as text so the assertions read as
// the state machine, not as a rendered page.
function Probe() {
  const { isAuthenticated, isLoading, backendUnreachable } = useAuth();
  return (
    <div>
      <span>{isLoading ? 'loading' : 'loaded'}</span>
      <span>{isAuthenticated ? 'authenticated' : 'anonymous'}</span>
      <span>{backendUnreachable ? 'unreachable' : 'reachable'}</span>
    </div>
  );
}

function renderProbe() {
  render(
    <AuthProvider>
      <Probe />
    </AuthProvider>,
  );
}

describe('AuthContext transport-vs-auth distinction', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    cleanup();
  });

  it('authenticates when the status probe returns 200', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response('{}', { status: 200 }));
    renderProbe();
    await waitFor(() => {
      expect(screen.getByText('loaded')).toBeInTheDocument();
    });
    expect(screen.getByText('authenticated')).toBeInTheDocument();
    expect(screen.getByText('reachable')).toBeInTheDocument();
  });

  it('stays anonymous on a 401 but keeps the backend marked reachable', async () => {
    // A real auth failure: the gateway answered, the session is not valid.
    // The login screen is the correct response here.
    vi.mocked(fetch).mockResolvedValueOnce(new Response('{"detail":"unauthorized"}', { status: 401 }));
    renderProbe();
    await waitFor(() => {
      expect(screen.getByText('loaded')).toBeInTheDocument();
    });
    expect(screen.getByText('anonymous')).toBeInTheDocument();
    expect(screen.getByText('reachable')).toBeInTheDocument();
  });

  it('marks the backend unreachable when the connection is refused', async () => {
    // The regression: this used to set anonymous+reachable, rendering the
    // login screen for a backend that was never listening.
    vi.mocked(fetch).mockRejectedValueOnce(new TypeError('Failed to fetch'));
    renderProbe();
    await waitFor(() => {
      expect(screen.getByText('loaded')).toBeInTheDocument();
    });
    expect(screen.getByText('anonymous')).toBeInTheDocument();
    expect(screen.getByText('unreachable')).toBeInTheDocument();
  });

  it('probes /api/system/status with credentials', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response('{}', { status: 200 }));
    renderProbe();
    await waitFor(() => expect(vi.mocked(fetch)).toHaveBeenCalled());
    const [url, init] = vi.mocked(fetch).mock.calls[0];
    expect(url).toBe('/api/system/status');
    expect(init).toMatchObject({ credentials: 'include' });
  });
});
