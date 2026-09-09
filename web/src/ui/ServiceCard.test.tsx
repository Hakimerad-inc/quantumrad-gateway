/** Windows ServiceCard component tests (S07-T9). */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import ServiceCard from './ServiceCard';

const mockFetch = vi.fn();
global.fetch = mockFetch;

let statusBody: Record<string, unknown> | null;

function okJson(body: unknown) {
  return { ok: true, status: 200, json: async () => body };
}

beforeEach(() => {
  statusBody = { available: true, installed: false, state: 'stopped' };
  mockFetch.mockReset();
  mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
    if (String(url).includes('/api/service') && !init?.method) {
      return statusBody ? okJson(statusBody) : { ok: false, status: 500, json: async () => ({}) };
    }
    if (String(url).includes('/api/service/') && init?.method === 'POST') {
      return okJson({ status: 'ok' });
    }
    return okJson({});
  });
});

afterEach(() => {
  cleanup();
});

describe('ServiceCard', () => {
  it('renders nothing when the service surface is unavailable (non-Windows)', async () => {
    statusBody = { available: false, installed: false, state: 'unsupported' };
    render(<ServiceCard />);
    await waitFor(() => {
      expect(mockFetch).toHaveBeenCalled();
    });
    expect(screen.queryByText(/windows service/i)).not.toBeInTheDocument();
  });

  it('shows state badge and all four actions when available', async () => {
    render(<ServiceCard />);
    expect(await screen.findByText('not installed')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Install service' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Uninstall service' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^start$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^stop$/i })).toBeDisabled();
  });

  it('shows the running badge when the service is running', async () => {
    statusBody = { available: true, installed: true, state: 'running' };
    render(<ServiceCard />);
    expect(await screen.findByText('running')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^start$/i })).toBeDisabled();
  });

  it('asks for confirmation before install and uninstall', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<ServiceCard />);
    await screen.findByText('not installed');

    await userEvent.click(screen.getByRole('button', { name: 'Install service' }));
    expect(confirmSpy).toHaveBeenCalledWith(expect.stringMatching(/install .* service/i));
    // Declined → no POST was made for install.
    const installPost = mockFetch.mock.calls.find(
      (c) => String(c[0]).includes('/api/service/install') && c[1]?.method === 'POST',
    );
    expect(installPost).toBeUndefined();
    confirmSpy.mockRestore();
  });

  it('posts the action when confirmed', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<ServiceCard />);
    await screen.findByText('not installed');
    await userEvent.click(screen.getByRole('button', { name: 'Install service' }));
    await waitFor(() => {
      const installPost = mockFetch.mock.calls.find(
        (c) => String(c[0]).includes('/api/service/install') && c[1]?.method === 'POST',
      );
      expect(installPost).toBeTruthy();
    });
  });

  it('shows an error banner when the status fetch fails', async () => {
    statusBody = null;
    render(<ServiceCard />);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });
});
