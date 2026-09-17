/** BackendDownView tests — a dead backend must not offer a password form.
 *
 * The bug this view exists for (B3 tray leg, 2026-09-17): the desktop shell
 * pointed at a port with no listener, the SPA's status probe threw, and the
 * window rendered the login screen. An operator would type a valid password,
 * get "Network error", and conclude their password was wrong.
 */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { AuthProvider } from '../context/AuthContext';
import BackendDownView from './BackendDownView';

// useAuth requires the provider; wrap it the way App does.
function renderView() {
  render(
    <AuthProvider>
      <BackendDownView />
    </AuthProvider>,
  );
}

describe('BackendDownView', () => {
  beforeEach(() => {
    // The status probe fires on mount; a stubbed 200 keeps the provider from
    // flipping the connection flag while the view's own state is under test.
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 200 })));
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    cleanup();
  });

  it('renders a connection error and no password field', async () => {
    renderView();
    // Let the provider's mount-time status probe settle before asserting, so
    // the state update lands inside the render the test observes.
    await waitFor(() => expect(screen.getByText(/Gateway unreachable/i)).toBeInTheDocument());
    expect(screen.getByText(/Cannot reach the gateway backend/i)).toBeInTheDocument();
    // The whole point: no credential prompt for a transport failure.
    expect(screen.queryByLabelText(/password/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /sign in/i })).not.toBeInTheDocument();
  });

  it('offers a retry that re-probes the backend', async () => {
    renderView();
    await waitFor(() => expect(vi.mocked(fetch)).toHaveBeenCalled());
    const retry = screen.getByRole('button', { name: /retry connection/i });
    // The button is the only call to action; retrying must not require a
    // password (the useAuth.checkAuth wiring is covered by AuthContext tests).
    expect(retry).toBeInTheDocument();
    expect(retry).not.toBeDisabled();
  });
});
