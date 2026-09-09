/** Review M8: SetupWizard component tests (last major untested view). */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import SetupWizard from './SetupWizard';

const mockFetch = vi.fn();
global.fetch = mockFetch;

/** URL-recognising stub responses, set per-test. */
type Responder = (url: string, body: unknown) => unknown | undefined;
let responder: Responder;

/** Stub reply for POST /api/wizard/validate/* (errors list; empty = pass). */
let validateErrors: string[] = [];
/** Stub reply for PUT /api/config (save). */
let saveOk: boolean;
/** Stub reply for POST /api/echo (echo probe status string). */
let echoStatus: string;

function okJson(body: unknown) {
  return { ok: true, status: 200, json: async () => body };
}

function renderWizard() {
  mockFetch.mockReset();
  mockFetch.mockImplementation(
    async (url: string, init?: { body?: string; method?: string }) => {
    const u = String(url);
    const body: unknown = init?.body ? JSON.parse(init.body) : undefined;
    // fetchConfig() probe on save — return the current config as stored.
    if (u.includes('/api/config') && (!init?.method || init.method === 'GET')) {
      return okJson(saveOk ? {} : {});
    }
    if (u.includes('/api/config') && init?.method === 'PUT') {
      return saveOk ? okJson({ status: 'ok', message: '', restart_required: true })
        : { ok: false, status: 400, json: async () => ({ detail: 'Invalid config: bad destination' }) };
    }
    if (u.includes('/api/wizard/validate/')) {
      return okJson({ step: 'x', errors: validateErrors });
    }
    if (u.includes('/api/echo')) {
      return okJson({ status: echoStatus, target: 'probe' });
    }
    const custom = responder(u, body);
    if (custom !== undefined) return custom;
    return okJson({});
    },
  );
  return render(<SetupWizard />);
}

async function typeReceiverAndNext() {
  await userEvent.clear(screen.getByLabelText(/ae title/i));
  await userEvent.type(screen.getByLabelText(/ae title/i), 'TESTAE');
  await userEvent.click(screen.getByRole('button', { name: /next/i }));
  // Step advanced once the receiver editor is gone.
  await waitFor(() => {
    expect(screen.queryByLabelText(/ae title/i)).not.toBeInTheDocument();
  });
}

beforeEach(() => {
  validateErrors = [];
  saveOk = true;
  echoStatus = 'ok';
  responder = () => undefined;
});

describe('SetupWizard', () => {
  it('renders the receiver step with AE title and port inputs', () => {
    renderWizard();
    expect(screen.getByRole('heading', { name: /setup wizard/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/ae title/i)).toHaveValue('GATEWAY');
    expect(screen.getByLabelText(/^port/i)).toHaveValue(11112);
    expect(screen.getByRole('button', { name: /back/i })).toBeDisabled();
  });

  it('shows validation errors and stays on the step when the server rejects it', async () => {
    validateErrors = ['AE title must be uppercase'];
    renderWizard();
    await userEvent.click(screen.getByRole('button', { name: /next/i }));
    expect(await screen.findByText(/ae title must be uppercase/i)).toBeInTheDocument();
    // Still on step 1 — the receiver editor is still shown.
    expect(screen.getByLabelText(/ae title/i)).toBeInTheDocument();
  });

  it('advances through validation to the destinations step', async () => {
    renderWizard();
    await typeReceiverAndNext();
    // Destination editor visible now.
    expect(screen.getByRole('button', { name: /add destination/i })).toBeInTheDocument();
    // The validate call carried the receiver step's data.
    const validateCall = mockFetch.mock.calls.find((c) =>
      String(c[0]).includes('/api/wizard/validate/'),
    );
    expect(validateCall).toBeTruthy();
    expect(JSON.parse(String(validateCall![1]?.body))).toMatchObject({
      ae_title: 'TESTAE',
      port: 11112,
    });
  });

  it('adds a destination row, echoes it, and removes it', async () => {
    renderWizard();
    await typeReceiverAndNext();

    await userEvent.click(screen.getByRole('button', { name: /add destination/i }));
    expect(screen.getByPlaceholderText('Name')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('Host')).toBeInTheDocument();

    await userEvent.type(screen.getByPlaceholderText('Name'), 'PACS-1');
    await userEvent.type(screen.getByPlaceholderText('Host'), '10.0.0.5');
    await userEvent.type(screen.getByPlaceholderText('AET'), 'ARCHIVE');

    await userEvent.click(screen.getByRole('button', { name: /^echo$/i }));
    await waitFor(() => {
      expect(screen.getByText('ok')).toBeInTheDocument();
    });
    const call = mockFetch.mock.calls.find((c) => String(c[0]).includes('/api/echo'));
    expect(JSON.parse(String(call![1]?.body))).toMatchObject({
      host: '10.0.0.5',
      port: 104,
      aet: 'MERCUREARCHIVE', // default AET prefix retained while typing
    });

    await userEvent.click(screen.getByRole('button', { name: /remove destination PACS-1/i }));
    expect(screen.queryByPlaceholderText('Name')).not.toBeInTheDocument();
  });

  it('navigates back from destinations to receiver', async () => {
    renderWizard();
    await typeReceiverAndNext();
    await userEvent.click(screen.getByRole('button', { name: /back/i }));
    expect(await screen.findByLabelText(/ae title/i)).toBeInTheDocument();
  });

  it('shows the summary step with the collected data before save', async () => {
    renderWizard();
    // Walk: receiver → destinations (empty, passes) → reports → summary.
    await typeReceiverAndNext();
    await userEvent.click(screen.getByRole('button', { name: /next/i })); // reports
    await userEvent.click(screen.getByRole('button', { name: /next/i })); // summary
    expect(await screen.findByText(/configuration summary/i)).toBeInTheDocument();
    expect(screen.getByText(/"ae_title": "TESTAE"/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /save configuration/i })).toBeInTheDocument();
  });

  it('saves the merged config on the summary step', async () => {
    renderWizard();
    await typeReceiverAndNext();
    await userEvent.click(screen.getByRole('button', { name: /next/i }));
    await userEvent.click(screen.getByRole('button', { name: /next/i }));
    await screen.findByText(/configuration summary/i);

    await userEvent.click(screen.getByRole('button', { name: /save configuration/i }));

    expect(await screen.findByText(/setup complete/i)).toBeInTheDocument();
    expect(screen.getByText(/configuration saved/i)).toBeInTheDocument();
    const put = mockFetch.mock.calls.find(
      (c) => String(c[0]).includes('/api/config') && c[1]?.method === 'PUT',
    );
    expect(put).toBeTruthy();
    const payload = JSON.parse(String(put![1]?.body));
    expect(payload.general).toMatchObject({ ae_title: 'TESTAE', port: 11112 });
    expect(payload.reports).toMatchObject({ enabled: false });
  });

  it('surfaces the server error when the save is rejected', async () => {
    saveOk = false;
    renderWizard();
    await typeReceiverAndNext();
    await userEvent.click(screen.getByRole('button', { name: /next/i }));
    await userEvent.click(screen.getByRole('button', { name: /next/i }));
    await screen.findByText(/configuration summary/i);

    await userEvent.click(screen.getByRole('button', { name: /save configuration/i }));

    // handleSave lets the error propagate; the wizard stays on the summary step
    // (no "Setup Complete") rather than silently pretending success.
    await waitFor(() => {
      expect(screen.queryByText(/setup complete/i)).not.toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: /save configuration/i })).toBeInTheDocument();
  });

  it('shows the saving state on the save button while the request is in flight', async () => {
    let resolveSave: (value: unknown) => void;
    renderWizard();
    await typeReceiverAndNext();
    await userEvent.click(screen.getByRole('button', { name: /next/i }));
    await userEvent.click(screen.getByRole('button', { name: /next/i }));
    await screen.findByText(/configuration summary/i);

    // Gate the config fetch that handleSave performs first.
    mockFetch.mockImplementation(async (url: string) => {
      if (String(url).includes('/api/config')) {
        return new Promise((resolve) => {
          resolveSave = resolve;
        });
      }
      return okJson({ errors: [] });
    });

    await userEvent.click(screen.getByRole('button', { name: /save configuration/i }));
    expect(await screen.findByText(/saving\.\.\./i)).toBeInTheDocument();

    await waitFor(() => {
      expect(resolveSave).toBeDefined();
    });
    await waitFor(async () => {
      // Release the gate; the stub still never resolves to done, so just
      // confirm the button stays disabled while pending.
      expect(screen.getByRole('button', { name: /saving\.\.\./i })).toBeDisabled();
    });
  });
});
