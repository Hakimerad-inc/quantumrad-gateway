/**
 * DestinationsView tests — managing delivery targets without editing JSON
 * (refinement 2026-09-17).
 *
 * The page exists because the Queue view's Enqueue error pointed at a
 * "Destinations page" that did not exist, leaving the operator no way to add or
 * re-enable a PACS short of hand-editing the config JSON.
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import DestinationsView from './DestinationsView';
import type { ConfigWarning } from '../api';

// A representative running config as GET /api/config returns it: the secret
// field carries the redaction sentinel, never the real value.
const CONFIG_WITH_SECRET = {
  config_version: '1.0',
  destinations: [
    {
      name: 'pacs-a',
      type: 'dicom',
      enabled: true,
      host: '10.0.0.5',
      port: 104,
      aet_target: 'ORTHANC',
      aet_source: 'GATEWAY',
    },
    {
      name: 'sftp-lab',
      type: 'sftp',
      enabled: false,
      host: 'lab.local',
      port: 22,
      username: 'dicom',
      password: '***', // redacted by the backend
    },
  ],
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

// Routes the stubbed fetch by URL/method so one stub serves the whole page.
function stubFetch(opts: {
  config?: unknown;
  warnings?: ConfigWarning[];
  saveResult?: unknown;
  echoStatus?: string;
} = {}) {
  const { config = CONFIG_WITH_SECRET, warnings = [], saveResult = { status: 'ok', restart_required: true }, echoStatus = 'ok' } = opts;
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    if (url.endsWith('/api/config') && (!init || init.method === undefined)) return json(config);
    if (url.endsWith('/api/config/warnings')) return json({ warnings, config_version: '1.0' });
    if (url.endsWith('/api/config') && init?.method === 'PUT') return json(saveResult);
    if (url.endsWith('/api/echo')) return json({ status: echoStatus });
    return json({});
  });
}

describe('DestinationsView', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    cleanup();
  });

  it('lists destinations from the running config', async () => {
    vi.stubGlobal('fetch', stubFetch());
    render(<DestinationsView />);
    // Names render as input *values*, not text nodes.
    expect(await screen.findByDisplayValue('pacs-a')).toBeInTheDocument();
    expect(screen.getByDisplayValue('sftp-lab')).toBeInTheDocument();
  });

  it('shows the empty state when no destination is configured', async () => {
    vi.stubGlobal('fetch', stubFetch({ config: { config_version: '1.0', destinations: [] } }));
    render(<DestinationsView />);
    expect(await screen.findByText('No destinations configured')).toBeInTheDocument();
  });

  it('adds a destination and saves the section', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const fetchStub = stubFetch();
    vi.stubGlobal('fetch', fetchStub);
    render(<DestinationsView />);
    await screen.findByDisplayValue('pacs-a');

    await user.click(screen.getByText('Add Destination'));
    // New destinations are appended to the end of the list.
    const nameInputs = screen.getAllByLabelText('Destination name');
    await user.type(nameInputs[nameInputs.length - 1], 'pacs-b');
    await user.click(screen.getByText('Save Changes'));

    await waitFor(() => {
      const puts = fetchStub.mock.calls.filter(([, init]) => init?.method === 'PUT');
      expect(puts).toHaveLength(1);
      const saved = JSON.parse(puts[0]![1]!.body as string);
      // The destinations section carries the new entry; the rest round-trips.
      expect(saved.destinations).toHaveLength(3);
      expect(saved.destinations[2].name).toBe('pacs-b');
      // Untouched existing secrets must keep the sentinel, not be blanked.
      expect(saved.destinations.find((d: { name: string }) => d.name === 'sftp-lab').password).toBe('***');
    });
  });

  it('typing into a masked secret replaces the mask, never appends to it', async () => {
    // A loaded secret renders as an empty field with a bullet placeholder.
    // Seeding the mask as the *value* meant one keystroke saved
    // "••••••••x" as the real password; the field must replace cleanly.
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const fetchStub = stubFetch();
    vi.stubGlobal('fetch', fetchStub);
    render(<DestinationsView />);
    await screen.findByDisplayValue('sftp-lab');

    const pw = screen.getByLabelText('sftp-lab Password') as HTMLInputElement;
    expect(pw.value).toBe('');
    expect(pw.type).toBe('password');
    await user.type(pw, 'new-secret');

    await user.click(screen.getByText('Save Changes'));
    await waitFor(() => {
      const puts = fetchStub.mock.calls.filter(([, init]) => init?.method === 'PUT');
      const saved = JSON.parse(puts[0]![1]!.body as string);
      expect(saved.destinations.find((d: { name: string }) => d.name === 'sftp-lab').password).toBe(
        'new-secret',
      );
    });
  });

  it('toggles enable/disable and preserves the secret sentinel', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const fetchStub = stubFetch();
    vi.stubGlobal('fetch', fetchStub);
    render(<DestinationsView />);
    const sftpName = await screen.findByDisplayValue('sftp-lab');
    const sftpCard = sftpName.closest('.card')!;
    const checkbox = sftpCard.querySelector('input[type="checkbox"]') as HTMLInputElement;
    expect(checkbox.checked).toBe(false);

    await user.click(checkbox);
    await user.click(screen.getByText('Save Changes'));

    await waitFor(() => {
      const puts = fetchStub.mock.calls.filter(([, init]) => init?.method === 'PUT');
      const saved = JSON.parse(puts[0]![1]!.body as string);
      expect(saved.destinations.find((d: { name: string }) => d.name === 'sftp-lab').enabled).toBe(true);
      expect(saved.destinations.find((d: { name: string }) => d.name === 'sftp-lab').password).toBe('***');
    });
  });

  it('runs an Echo probe and reports the status', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    vi.stubGlobal('fetch', stubFetch({ echoStatus: 'refused' }));
    render(<DestinationsView />);
    const name = await screen.findByDisplayValue('pacs-a');
    const card = name.closest('.card')!;
    await user.click(card.querySelector('button')!); // first button in the header is Echo
    expect(await screen.findByText('refused')).toBeInTheDocument();
  });

  it('renders lint warnings from the running config', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({
        warnings: [
          { path: 'destinations', message: 'Every destination is disabled.', severity: 'warning' },
        ],
      }),
    );
    render(<DestinationsView />);
    expect(await screen.findByText(/Every destination is disabled/)).toBeInTheDocument();
  });

  it('surfaces a save rejection with the server detail, not a bare failure', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.endsWith('/api/config') && init?.method === 'PUT') {
          return json({ detail: "Invalid config: port: Input should be greater than 0" }, 400);
        }
        return json(CONFIG_WITH_SECRET);
      }),
    );
    render(<DestinationsView />);
    await screen.findByDisplayValue('pacs-a');
    await user.click(screen.getByText('Add Destination'));
    await user.click(screen.getByText('Save Changes'));
    expect(await screen.findByText(/Input should be greater than 0/)).toBeInTheDocument();
  });
});
