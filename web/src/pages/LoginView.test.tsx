/** S06-T8 (RED): Login page component tests. */

import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import LoginView from './LoginView';
import { AuthProvider } from '../context/AuthContext';

// Mock fetch
const mockFetch = vi.fn();
global.fetch = mockFetch;

/** Response the stub returns for the next POST /api/login. */
let loginResponse: { ok: boolean; status: number; body: unknown };
/** When set, POST /api/login awaits this promise instead of resolving. */
let loginGate: Promise<unknown> | null = null;

function renderLogin(onLogin = vi.fn()) {
  return render(
    // LoginView delegates to AuthContext, so it must be inside the provider.
    <AuthProvider>
      <LoginView onLogin={onLogin} />
    </AuthProvider>,
  );
}

describe('LoginView', () => {
  beforeEach(() => {
    loginResponse = { ok: true, status: 200, body: { status: 'ok' } };
    loginGate = null;
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      // AuthProvider probes /api/system/status on mount.
      if (String(url).includes('/api/system/status')) {
        return { ok: false, status: 401, json: async () => ({}) };
      }
      if (String(url).includes('/api/login')) {
        if (loginGate) return loginGate;
        return {
          ok: loginResponse.ok,
          status: loginResponse.status,
          json: async () => loginResponse.body,
        };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    });
  });

  it('renders login form with username/password fields and submit button', async () => {
    renderLogin();
    // findBy* flushes the AuthProvider mount probe before asserting.
    expect(await screen.findByLabelText(/password/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /sign in/i })).toBeInTheDocument();
  });

  it('shows error message on failed login (401)', async () => {
    loginResponse = { ok: false, status: 401, body: { detail: 'invalid credentials' } };

    renderLogin();
    const passwordInput = screen.getByLabelText(/password/i);
    const submitButton = screen.getByRole('button', { name: /sign in/i });

    await userEvent.type(passwordInput, 'wrong');
    await userEvent.click(submitButton);

    await waitFor(() => {
      expect(screen.getByText(/invalid credentials/i)).toBeInTheDocument();
    });
  });

  it('calls onLogin callback on successful login', async () => {
    const onLogin = vi.fn();

    renderLogin(onLogin);
    const passwordInput = screen.getByLabelText(/password/i);
    const submitButton = screen.getByRole('button', { name: /sign in/i });

    await userEvent.type(passwordInput, 'correct');
    await userEvent.click(submitButton);

    await waitFor(() => {
      expect(onLogin).toHaveBeenCalledTimes(1);
    });
  });

  it('disables submit button while loading', async () => {
    let resolveFetch: (value: unknown) => void;
    loginGate = new Promise((resolve) => {
      resolveFetch = resolve;
    });

    renderLogin();
    const passwordInput = screen.getByLabelText(/password/i);
    const submitButton = screen.getByRole('button', { name: /sign in/i });

    await userEvent.type(passwordInput, 'password');
    await userEvent.click(submitButton);

    expect(submitButton).toBeDisabled();
    expect(submitButton).toHaveTextContent(/signing in/i);

    await act(async () => {
      resolveFetch!({ ok: true, status: 200, json: async () => ({ status: 'ok' }) });
    });
    await waitFor(() => expect(submitButton).not.toBeDisabled());
  });
});
