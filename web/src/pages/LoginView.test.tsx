/** S06-T8 (RED): Login page component tests. */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import LoginView from './LoginView';

// Mock fetch
const mockFetch = vi.fn();
global.fetch = mockFetch;

describe('LoginView', () => {
  beforeEach(() => {
    mockFetch.mockReset();
  });

  it('renders login form with username/password fields and submit button', () => {
    render(<LoginView onLogin={vi.fn()} />);
    expect(screen.getByLabelText(/password/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /sign in/i })).toBeInTheDocument();
  });

  it('shows error message on failed login (401)', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 401,
      json: async () => ({ detail: 'invalid credentials' }),
    });

    render(<LoginView onLogin={vi.fn()} />);
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
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ status: 'ok' }),
    });

    render(<LoginView onLogin={onLogin} />);
    const passwordInput = screen.getByLabelText(/password/i);
    const submitButton = screen.getByRole('button', { name: /sign in/i });

    await userEvent.type(passwordInput, 'correct');
    await userEvent.click(submitButton);

    await waitFor(() => {
      expect(onLogin).toHaveBeenCalledTimes(1);
    });
  });

  it('disables submit button while loading', async () => {
    let resolveFetch: (value: any) => void;
    const fetchPromise = new Promise((resolve) => { resolveFetch = resolve; });
    mockFetch.mockReturnValueOnce(fetchPromise);

    render(<LoginView onLogin={vi.fn()} />);
    const passwordInput = screen.getByLabelText(/password/i);
    const submitButton = screen.getByRole('button', { name: /sign in/i });

    await userEvent.type(passwordInput, 'password');
    await userEvent.click(submitButton);

    expect(submitButton).toBeDisabled();
    expect(submitButton).toHaveTextContent(/signing in/i);

    resolveFetch!({ ok: true, status: 200, json: async () => ({ status: 'ok' }) });
    await fetchPromise;
  });
});
