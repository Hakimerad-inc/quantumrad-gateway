/** Review C1 (RED): the shell must be gated on auth state, not on a stale flag.
 *
 * Regression guard for the login loop: LoginView used to POST /api/login itself,
 * which set the session cookie while `isAuthenticated` stayed false, so AppContent
 * re-rendered the login screen forever.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import App from './App';

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>();
  return {
    ...actual,
    fetchSystemStatus: vi.fn().mockResolvedValue({
      receiver: 'running',
      forwarder: 'running',
      report_retriever: 'running',
      uptime_sec: 1,
      version: '0.1.0',
      hub_registered: false,
      hub_streaming: false,
    }),
    fetchQueueStats: vi
      .fn()
      .mockResolvedValue({ queued: 0, sending: 0, sent: 0, error: 0, failed: 0 }),
    fetchDiskStatus: vi.fn().mockRejectedValue(new Error('n/a')),
  };
});

const mockFetch = vi.fn();
global.fetch = mockFetch;

describe('App auth gating (review C1)', () => {
  beforeEach(() => {
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (String(url).includes('/api/system/status')) {
        return { ok: false, status: 401, json: async () => ({}) };
      }
      if (String(url).includes('/api/login')) {
        return { ok: true, status: 200, json: async () => ({ status: 'ok' }) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    });
  });

  it('shows only the login screen while unauthenticated', async () => {
    render(<App />);

    expect(await screen.findByRole('button', { name: /sign in/i })).toBeInTheDocument();
    expect(screen.queryByText('Sign out')).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Dashboard' })).not.toBeInTheDocument();
  });

  it('leaves the login screen after a successful login instead of looping back', async () => {
    render(<App />);

    const passwordInput = await screen.findByLabelText(/password/i);
    await userEvent.type(passwordInput, 'correct');
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }));

    await waitFor(() => {
      expect(screen.queryByRole('button', { name: /sign in/i })).not.toBeInTheDocument();
    });
    expect(await screen.findByRole('heading', { name: 'Dashboard' })).toBeInTheDocument();
    expect(screen.getByText('Sign out')).toBeInTheDocument();
  });

  it('returns to the login screen after logout', async () => {
    render(<App />);

    const passwordInput = await screen.findByLabelText(/password/i);
    await userEvent.type(passwordInput, 'correct');
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }));

    await screen.findByRole('heading', { name: 'Dashboard' });
    await userEvent.click(screen.getByText('Sign out'));

    expect(await screen.findByRole('button', { name: /sign in/i })).toBeInTheDocument();
  });
});
