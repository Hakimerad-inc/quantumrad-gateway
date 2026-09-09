/** UpdaterBanner tests (ADR-0006 opt-in update slice). */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// The Tauri plugin modules don't exist in jsdom — mock the module imports.
vi.mock('@tauri-apps/plugin-updater', () => ({
  check: vi.fn(),
}));
vi.mock('@tauri-apps/plugin-process', () => ({
  relaunch: vi.fn(),
}));

import { check } from '@tauri-apps/plugin-updater';
import { relaunch } from '@tauri-apps/plugin-process';
import UpdaterBanner from './UpdaterBanner';

// The plugin's real Update type carries many more fields; the banner only
// touches version + downloadAndInstall, so a partial cast is honest here.
type CheckResult = Awaited<ReturnType<typeof check>>;

function fakeUpdate(
  version: string,
  downloadAndInstall: () => Promise<void>,
): NonNullable<CheckResult> {
  return { version, downloadAndInstall } as unknown as NonNullable<CheckResult>;
}

function setTauri(on: boolean) {
  Object.defineProperty(window, '__TAURI_INTERNALS__', {
    value: on ? {} : undefined,
    configurable: true,
    writable: true,
  });
}

beforeEach(() => {
  vi.mocked(check).mockReset();
  vi.mocked(relaunch).mockReset();
  setTauri(true);
});

afterEach(() => {
  cleanup();
  setTauri(false);
});

describe('UpdaterBanner', () => {
  it('renders nothing when no update is available', async () => {
    vi.mocked(check).mockResolvedValue(null);
    const { container } = render(<UpdaterBanner />);
    await waitFor(() => expect(check).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing outside the Tauri shell', () => {
    setTauri(false);
    const { container } = render(<UpdaterBanner />);
    expect(container).toBeEmptyDOMElement();
    expect(check).not.toHaveBeenCalled();
  });

  it('offers an opt-in restart when an update is available', async () => {
    vi.mocked(check).mockResolvedValue(
      fakeUpdate('1.1.0', vi.fn().mockResolvedValue(undefined)),
    );
    render(<UpdaterBanner />);
    expect(await screen.findByText(/1\.1\.0/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /dismiss/i })).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /restart to update/i }));
    await waitFor(() => expect(relaunch).toHaveBeenCalledTimes(1));
  });

  it('hides the banner when dismissed', async () => {
    vi.mocked(check).mockResolvedValue(fakeUpdate('1.1.0', vi.fn()));
    render(<UpdaterBanner />);
    await userEvent.click(await screen.findByRole('button', { name: /dismiss/i }));
    expect(screen.queryByText(/1\.1\.0/)).not.toBeInTheDocument();
  });
});
