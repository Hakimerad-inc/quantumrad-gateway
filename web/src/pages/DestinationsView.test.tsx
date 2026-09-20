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
  preview?: unknown;
  previewStatus?: number;
} = {}) {
  const {
    config = CONFIG_WITH_SECRET,
    warnings = [],
    saveResult = { status: 'ok', restart_required: true },
    echoStatus = 'ok',
    preview = null,
    previewStatus = 200,
  } = opts;
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    if (url.endsWith('/api/config') && (!init || init.method === undefined)) return json(config);
    if (url.endsWith('/api/config/warnings')) return json({ warnings, config_version: '1.0' });
    if (url.endsWith('/api/config') && init?.method === 'PUT') return json(saveResult);
    if (url.endsWith('/api/echo')) return json({ status: echoStatus });
    if (url.endsWith('/api/rules/preview')) return json(preview, previewStatus);
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

  it('exposes every field a destination type requires — XNAT was unusable before', async () => {
    // The form once showed only the URL for XNAT, but the model requires
    // username/password/project; every XNAT add ended in a 400 naming fields
    // the operator never saw. If a required field goes missing from
    // TYPE_FIELDS again, this test names it.
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const fetchStub = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.endsWith('/api/config') && (!init || init.method === undefined)) {
        return json({ config_version: '1.0', destinations: [] });
      }
      if (url.endsWith('/api/config/warnings')) return json({ warnings: [], config_version: '1.0' });
      if (url.endsWith('/api/config') && init?.method === 'PUT') {
        return json({ status: 'ok', restart_required: true });
      }
      return json({});
    });
    vi.stubGlobal('fetch', fetchStub);
    render(<DestinationsView />);
    await screen.findByText('No destinations configured');

    await user.click(screen.getByText('Add Destination'));
    const typeSelect = screen.getByLabelText('Destination type') as HTMLSelectElement;
    await user.selectOptions(typeSelect, 'xnat');

    for (const label of ['URL', 'Username', 'Password', 'Project']) {
      expect(screen.getByLabelText(`destination ${label}`)).toBeInTheDocument();
    }
  });

  it('switching a destination type drops stale fields instead of 400ing', async () => {
    // The old handler set only `type`, so a dicom→s3 switch kept host/port and
    // omitted bucket — the save 400'd on a required field the operator never
    // filled. The rebuild keeps name/type/enabled only.
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const fetchStub = stubFetch();
    vi.stubGlobal('fetch', fetchStub);
    render(<DestinationsView />);
    await screen.findByDisplayValue('pacs-a');

    const typeSelect = screen.getAllByLabelText('Destination type')[0] as HTMLSelectElement;
    await user.selectOptions(typeSelect, 's3');

    // The new type's required field is now present…
    expect(screen.getByLabelText('pacs-a Bucket')).toBeInTheDocument();
    // …and the stale dicom fields are gone from the saved payload.
    expect(screen.queryByLabelText('pacs-a Host')).not.toBeInTheDocument();
  });

  it('blocks duplicate and empty names inline, not after a save', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const fetchStub = stubFetch();
    vi.stubGlobal('fetch', fetchStub);
    render(<DestinationsView />);
    await screen.findByDisplayValue('pacs-a');

    // Duplicate the existing name.
    await user.click(screen.getByText('Add Destination'));
    const names = screen.getAllByLabelText('Destination name');
    await user.type(names[names.length - 1], 'pacs-a');

    // Both cards sharing the name carry the error.
    expect(await screen.findAllByText('Name must be unique.')).toHaveLength(2);
    expect(screen.getByText('Save Changes')).toBeDisabled();

    // Fixing it re-enables the save.
    await user.clear(names[names.length - 1]);
    await user.type(names[names.length - 1], 'pacs-c');
    expect(screen.getByText('Save Changes')).toBeEnabled();
  });

  it('never sends the client-only stable id to the backend', async () => {
    // The uid is a React key, not config; the model would reject the extra
    // field. It must be stripped on the wire even after a type switch.
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const fetchStub = stubFetch();
    vi.stubGlobal('fetch', fetchStub);
    render(<DestinationsView />);
    await screen.findByDisplayValue('pacs-a');

    await user.click(screen.getByText('Add Destination'));
    const names = screen.getAllByLabelText('Destination name');
    await user.type(names[names.length - 1], 'pacs-b');
    await user.click(screen.getByText('Save Changes'));

    await waitFor(() => {
      const puts = fetchStub.mock.calls.filter(([, init]) => init?.method === 'PUT');
      expect(puts).toHaveLength(1);
      const saved = JSON.parse(puts[0]![1]!.body as string);
      // No destination object carries the internal id.
      expect(saved.destinations.every((d: Record<string, unknown>) => !('_uid' in d))).toBe(true);
    });
  });

  it('reports an expired admin session as such, not as a dead destination', async () => {
    // A 401 means the operator's session lapsed, not that the PACS is down.
    // Reporting "error" puts an unreachable badge on a healthy destination.
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.endsWith('/api/echo')) return json({ detail: 'Not authenticated' }, 401);
        return stubFetch()(url, init);
      }),
    );
    render(<DestinationsView />);
    const name = await screen.findByDisplayValue('pacs-a');
    await user.click(name.closest('.card')!.querySelector('button')!); // Echo
    expect(await screen.findByText('session expired')).toBeInTheDocument();
  });

  it('drops a stale Echo badge when the destination is edited afterwards', async () => {
    // The badge was keyed by the destination's live host:port, so it survived
    // an edit and vouched for an endpoint it had never touched. It must be
    // keyed by what was actually probed.
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    vi.stubGlobal('fetch', stubFetch({ echoStatus: 'refused' }));
    render(<DestinationsView />);
    const name = await screen.findByDisplayValue('pacs-a');
    const card = name.closest('.card')!;
    await user.click(card.querySelector('button')!); // Echo
    expect(await screen.findByText('refused')).toBeInTheDocument();

    // Change where the destination points: the old result no longer describes it.
    await user.clear(screen.getByLabelText('pacs-a Host'));
    await user.type(screen.getByLabelText('pacs-a Host'), '10.0.0.99');

    expect(screen.queryByText('refused')).not.toBeInTheDocument();
  });

  it('offers the one remedy for a stale routing rule that this page can perform', async () => {
    // The lint warning advises renaming the target or adding the destination —
    // but no forwarding-rules UI exists, so "rename" sends the operator to raw
    // JSON. Adding the destination is what this page is for; the warning must
    // carry the fix, not just the complaint.
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const fetchStub = stubFetch({
      config: {
        config_version: '1.0',
        destinations: CONFIG_WITH_SECRET.destinations,
        forwarding_rules: [
          { rule: 'StudyDescription ~ ^MRI', targets: ['pacs-a', 'orthanc-2'], priority: 'normal' },
        ],
      },
      warnings: [
        {
          path: 'forwarding_rules[0].targets',
          message: 'Rule targets unknown destination(s) orthanc-2. Rename or remove the target, or add the destination.',
          severity: 'warning',
        },
      ],
    });
    vi.stubGlobal('fetch', fetchStub);
    render(<DestinationsView />);
    const btn = await screen.findByText(/Add destination orthanc-2/);
    expect(btn).toBeInTheDocument();

    await user.click(btn);
    expect(await screen.findByDisplayValue('orthanc-2')).toBeInTheDocument();
  });

  it('offers the Echo probe only where the answer can be trusted (plaintext dicom)', async () => {    // The endpoint probes in plaintext; a TLS-only PACS would report
    // "refused" while healthy. The button must not appear for dicom_tls.
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    vi.stubGlobal('fetch', stubFetch());
    render(<DestinationsView />);
    await screen.findByDisplayValue('pacs-a');

    const typeSelect = screen.getAllByLabelText('Destination type')[0] as HTMLSelectElement;
    await user.selectOptions(typeSelect, 'dicom_tls');
    expect(screen.queryByText('Echo')).not.toBeInTheDocument();
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

  it('previews which destinations a modality routes to, by configured rule', async () => {
    // The preview is the operator-facing half of the unified rule engine
    // (review P0-9): before it, the only way to ask "where would this go" was
    // to let a real study arrive and check the queue.
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const fetchStub = stubFetch({
      preview: { targets: ['pacs-a'], matched_any: true },
    });
    vi.stubGlobal('fetch', fetchStub);
    render(<DestinationsView />);
    await screen.findByDisplayValue('pacs-a');

    await user.type(screen.getByLabelText('Modality to preview'), 'CT');
    await user.click(screen.getByRole('button', { name: /Preview/ }));

    expect(await screen.findByText(/A forwarding rule matched/)).toBeInTheDocument();
    expect(screen.getByText(/pacs-a/)).toBeInTheDocument();
    // The tag the operator typed is what the backend is asked about.
    const posts = fetchStub.mock.calls.filter(([, init]) => init?.method === 'POST');
    expect(JSON.parse(posts[0]![1]!.body as string)).toEqual({ tags: { Modality: 'CT' }, rules: null });
  });

  it('distinguishes a rule match from the default route in the preview', async () => {
    // The target list alone cannot tell the two apart — "pacs-a" because a
    // rule said so and "pacs-a" because no rule said anything are different
    // statements about the operator's config.
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    vi.stubGlobal(
      'fetch',
      stubFetch({
        preview: { targets: ['pacs-a'], matched_any: false },
      }),
    );
    render(<DestinationsView />);
    await screen.findByDisplayValue('pacs-a');

    await user.type(screen.getByLabelText('Modality to preview'), 'US');
    await user.click(screen.getByRole('button', { name: /Preview/ }));

    expect(await screen.findByText(/No rule matched this modality/)).toBeInTheDocument();
  });

  it('reports a rule the preview cannot parse instead of a silent default', async () => {
    // A malformed configured rule makes the endpoint 400; the live router
    // fails open and over-delivers, so without this the operator's preview
    // would look clean while every study went everywhere.
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    vi.stubGlobal(
      'fetch',
      stubFetch({
        preview: { detail: 'forwarding_rules[1]: rule has empty tag or value' },
        previewStatus: 400,
      }),
    );
    render(<DestinationsView />);
    await screen.findByDisplayValue('pacs-a');

    await user.type(screen.getByLabelText('Modality to preview'), 'CT');
    await user.click(screen.getByRole('button', { name: /Preview/ }));

    expect(await screen.findByText(/could not be parsed/)).toBeInTheDocument();
    // The failed preview never renders a target list that could be mistaken
    // for an answer.
    expect(screen.queryByText(/A forwarding rule matched/)).not.toBeInTheDocument();
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
    // The inline name gate blocks an empty-named save, so the new card needs
    // a name before this can reach the PUT.
    const names = screen.getAllByLabelText('Destination name');
    await user.type(names[names.length - 1], 'pacs-b');
    await user.click(screen.getByText('Save Changes'));
    expect(await screen.findByText(/Input should be greater than 0/)).toBeInTheDocument();
  });

  it('a rename carrying an untouched secret is refused with a message, not saved', async () => {
    // Rename is a first-class action, but a secret the operator never touched
    // is still the '***' sentinel — and a sentinel under a name nothing is
    // stored under cannot be restored. Position used to cover this; position is
    // not identity once the list is sorted or filtered, so the server now 400s
    // naming the destination and the page must show that detail (review P1-2).
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.endsWith('/api/config') && init?.method === 'PUT') {
          return json(
            {
              detail:
                "Destination 'sftp-lab-2' has a redacted password but no destination named 'sftp-lab-2' exists — re-enter the credential.",
            },
            400,
          );
        }
        return json(CONFIG_WITH_SECRET);
      }),
    );
    render(<DestinationsView />);
    const name = await screen.findByDisplayValue('sftp-lab');
    await user.clear(name);
    await user.type(name, 'sftp-lab-2');
    await user.click(screen.getByText('Save Changes'));
    expect(await screen.findByText(/re-enter the credential/)).toBeInTheDocument();
  });
});
