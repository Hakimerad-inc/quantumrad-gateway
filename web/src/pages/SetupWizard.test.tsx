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

function stubFetch() {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    if (url.endsWith('/api/config') && init?.method === 'PUT') {
      return json({ status: 'ok', message: 'Config update saved', restart_required: true });
    }
    if (url.endsWith('/api/wizard/validate/receiver')) return json({ errors: [] });
    if (url.endsWith('/api/wizard/validate/destinations')) return json({ errors: [] });
    if (url.endsWith('/api/wizard/validate/reports')) return json({ errors: [] });
    if (url.endsWith('/api/config')) return json(RUNNING_CONFIG);
    return json({});
  });
}

// Walk the wizard to Summary and Save, returning the PUT body the page sent.
async function saveAndCollect(): Promise<Record<string, unknown>> {
  const user = userEvent.setup();
  const fetchStub = stubFetch();
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

  it('preserves the structured reports.query_source instead of the free-text string', async () => {
    const saved = await saveAndCollect();

    expect(saved.reports).toMatchObject({ enabled: true });
    // The wizard's "host:port" string must not overwrite the structured object.
    expect(saved.reports).toHaveProperty('query_source', REPORT_SOURCE);
  });
});
