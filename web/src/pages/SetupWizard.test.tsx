/**
 * SetupWizard tests — the wrong-section regression guard (review P0-4).
 *
 * The wizard used to merge its receiver step into `general`:
 *
 *     current.general = {...current.general, ...data.receiver}
 *
 * `GeneralConfig` has no `ae_title`/`port`, so Pydantic's default `extra=
 * "ignore"` discarded them. The operator validated, saved, saw a success
 * toast, and the receiver kept its defaults forever. This test pins the
 * payload shape: receiver fields reach `receiver`, and `general` still has
 * exactly its own three keys.
 */
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, afterEach } from 'vitest';
import SetupWizard from './SetupWizard';

const REPORT_SOURCE = { type: 'dicom', host: '10.0.0.9', port: 104, aet: 'ORTHANC' };

const RUNNING_CONFIG = {
  config_version: '1.0',
  general: { appliance_name: 'gateway', locale: 'en', log_level: 'INFO' },
  receiver: { ae_title: 'OLD-AET', port: 11112 },
  destinations: [{ name: 'pacs-a', type: 'dicom', host: '10.0.0.5', port: 104, aet_target: 'ORTHANC' }],
  reports: { enabled: false, query_source: REPORT_SOURCE },
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function stubFetch(running: unknown = RUNNING_CONFIG) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    if (url.endsWith('/api/config') && init?.method === 'PUT') {
      return json({ status: 'ok', message: 'Config update saved', restart_required: true });
    }
    if (url.endsWith('/api/wizard/validate/receiver')) return json({ errors: [] });
    if (url.endsWith('/api/wizard/validate/destinations')) return json({ errors: [] });
    if (url.endsWith('/api/wizard/validate/reports')) return json({ errors: [] });
    if (url.endsWith('/api/wizard/validate/admin')) return json({ errors: [] });
    if (url.endsWith('/api/web-ui/password')) return json({ status: 'ok' });
    if (url.endsWith('/api/config')) return json(running);
    return json({});
  });
}

// Walk the wizard to Summary and Save, returning the PUT body the page sent.
// `running` is the GET /api/config body the wizard starts from; a fresh
// appliance has reports.query_source null, which is the case that exposed the
// discard (the default stub already carries a structured object and masks it).
async function saveAndCollect(running: unknown = RUNNING_CONFIG): Promise<Record<string, unknown>> {
  const user = userEvent.setup();
  const fetchStub = stubFetch(running);
  vi.stubGlobal('fetch', fetchStub);
  render(<SetupWizard />);

  // Step 1 — change the AE title to something recognizable in the payload.
  const aeInput = screen.getByLabelText('AE Title') as HTMLInputElement;
  await user.clear(aeInput);
  await user.type(aeInput, 'NEW-AET');

  // handleNext is async (it awaits validation before advancing), so each Next
  // is followed by a findBy* on something only the *next* step renders — that
  // is the step-transition signal. Clicking blind races the validation and
  // leaves the wizard a step behind.
  await user.click(screen.getByText('Next'));
  await screen.findByText('+ Add Destination'); // destinations step

  await user.click(screen.getByText('Next'));
  // Reports step: enable retrieval and type a free-text query source — the old
  // merge wrote this string over the structured ReportQuerySource object.
  const enableReports = await screen.findByLabelText('Enable report retrieval');
  await user.click(enableReports);
  const qsInput = screen.getByLabelText('Query Source (host:port)') as HTMLInputElement;
  await user.type(qsInput, '10.0.0.9:104');

  await user.click(screen.getByText('Next'));
  // Admin step: type a password — it must reach /api/web-ui/password, never
  // the config PUT body (review P0-8).
  await screen.findByText('Admin Password');
  await user.type(screen.getByLabelText('Password'), 's3cret-s3cret');
  await user.type(screen.getByLabelText('Confirm'), 's3cret-s3cret');

  await user.click(screen.getByText('Next'));
  await screen.findByText('Configuration Summary'); // summary step
  await user.click(screen.getByText('Save Configuration'));

  // The success screen is the proof the save actually completed.
  expect(await screen.findByText('Configuration saved')).toBeInTheDocument();

  const puts = fetchStub.mock.calls.filter(([, init]) => init?.method === 'PUT');
  expect(puts).toHaveLength(1);
  return JSON.parse(puts[0]![1]!.body as string);
}

describe('SetupWizard', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    cleanup();
  });

  it('writes receiver fields into the receiver section, not general', async () => {
    const saved = await saveAndCollect();

    expect(saved.receiver).toMatchObject({ ae_title: 'NEW-AET', port: 11112 });
    // The bug: ae_title/port landed in `general` and were silently dropped.
    expect(saved.general).not.toHaveProperty('ae_title');
    expect(saved.general).not.toHaveProperty('port');
  });

  it('leaves the general section with exactly its own keys', async () => {
    const saved = await saveAndCollect();

    // If the wizard ever writes a key GeneralConfig does not have, the backend
    // `extra="forbid"` change (P0-4) turns it into a 400 here.
    expect(Object.keys(saved.general as object).sort()).toEqual(
      ['appliance_name', 'locale', 'log_level'].sort(),
    );
  });

  it('parses the free-text query source into a structured ReportQuerySource', async () => {
    const saved = await saveAndCollect();

    expect(saved.reports).toMatchObject({ enabled: true });
    // The wizard collects "host:port" but query_source on the wire is a
    // structured object. The wizard used to collect the string because the
    // backend gate requires it, then drop it — reports came up enabled with
    // query_source null and silently never retrieved anything.
    expect(saved.reports).toHaveProperty('query_source', {
      type: 'dicom',
      host: '10.0.0.9',
      port: 104,
      // The calling AE is the receiver title set in step 1, not a literal.
      aet: 'NEW-AET',
    });
    // The free-text string itself must not reach the wire.
    expect(saved.reports).not.toHaveProperty('query_source', '10.0.0.9:104');
  });

  it('writes a query source on a fresh appliance whose config has none', async () => {
    // The install case that actually shipped broken: GET /api/config on a
    // fresh appliance returns query_source null, the wizard collected a
    // required PACS address and threw it away, and main.py logged "reports
    // are enabled but reports.query_source is unset" with no UI remedy.
    const fresh = JSON.parse(JSON.stringify(RUNNING_CONFIG)) as Record<string, unknown>;
    (fresh.reports as Record<string, unknown>).query_source = null;

    const saved = await saveAndCollect(fresh);

    expect(saved.reports).toMatchObject({ enabled: true });
    const qs = (saved.reports as Record<string, unknown>).query_source;
    expect(qs).toEqual({ type: 'dicom', host: '10.0.0.9', port: 104, aet: 'NEW-AET' });
  });

  it('leaves query_source untouched when reports are disabled', async () => {
    // The wizard step is optional; leaving it off must not null out an
    // endpoint a later config edit established.
    const user = userEvent.setup();
    const fetchStub = stubFetch();
    vi.stubGlobal('fetch', fetchStub);
    render(<SetupWizard />);

    await user.type(screen.getByLabelText('AE Title'), 'NEW-AET');
    await user.click(screen.getByText('Next'));
    await screen.findByText('+ Add Destination');
    await user.click(screen.getByText('Next'));
    // Reports step, left disabled.
    await screen.findByLabelText('Enable report retrieval');
    await user.click(screen.getByText('Next'));
    await screen.findByText('Admin Password');
    await user.click(screen.getByText('Next'));
    await screen.findByText('Configuration Summary');
    await user.click(screen.getByText('Save Configuration'));
    expect(await screen.findByText('Configuration saved')).toBeInTheDocument();

    const puts = fetchStub.mock.calls.filter(([, init]) => init?.method === 'PUT');
    const saved = JSON.parse(puts[0]![1]!.body as string);
    expect(saved.reports).toHaveProperty('query_source', REPORT_SOURCE);
  });

  it('posts the admin password to the dedicated endpoint, never into the config', async () => {
    const user = userEvent.setup();
    const fetchStub = stubFetch();
    vi.stubGlobal('fetch', fetchStub);
    render(<SetupWizard />);

    await user.click(screen.getByText('Next'));
    await screen.findByText('+ Add Destination');
    await user.click(screen.getByText('Next'));
    await screen.findByLabelText('Enable report retrieval');
    await user.click(screen.getByText('Next'));
    await screen.findByText('Admin Password');
    await user.type(screen.getByLabelText('Password'), 's3cret-s3cret');
    await user.type(screen.getByLabelText('Confirm'), 's3cret-s3cret');
    await user.click(screen.getByText('Next'));

    // The summary must not render the plaintext — it is in the DOM.
    const summary = await screen.findByText('Configuration Summary');
    expect(summary.parentElement?.textContent).not.toContain('s3cret-s3cret');

    await user.click(screen.getByText('Save Configuration'));
    expect(await screen.findByText('Configuration saved')).toBeInTheDocument();

    // The password went to /api/web-ui/password as plaintext, once.
    const pwPosts = fetchStub.mock.calls.filter(
      ([url, init]) => url.toString().endsWith('/api/web-ui/password') && init?.method === 'POST',
    );
    expect(pwPosts).toHaveLength(1);
    expect(JSON.parse(pwPosts[0]![1]!.body as string)).toEqual({
      new_password: 's3cret-s3cret',
    });

    // …and never near the config body.
    const putBody = JSON.parse(
      fetchStub.mock.calls.find(([, init]) => init?.method === 'PUT')![1]!.body as string,
    );
    expect(JSON.stringify(putBody)).not.toContain('s3cret-s3cret');
  });

  it('skips the password post when the admin step is left blank', async () => {
    const user = userEvent.setup();
    const fetchStub = stubFetch();
    vi.stubGlobal('fetch', fetchStub);
    render(<SetupWizard />);

    await user.click(screen.getByText('Next'));
    await screen.findByText('+ Add Destination');
    await user.click(screen.getByText('Next'));
    await screen.findByLabelText('Enable report retrieval');
    await user.click(screen.getByText('Next'));
    await screen.findByText('Admin Password');
    await user.click(screen.getByText('Next'));
    await screen.findByText('Configuration Summary');
    await user.click(screen.getByText('Save Configuration'));
    expect(await screen.findByText('Configuration saved')).toBeInTheDocument();

    expect(
      fetchStub.mock.calls.some(([url]) => url.toString().endsWith('/api/web-ui/password')),
    ).toBe(false);
  });
});
