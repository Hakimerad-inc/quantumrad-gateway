/**
 * ConfigView tests — the validated raw-JSON editor (refinement 2026-09-17).
 *
 * The page is the second half of the config-warnings trio: where Destinations
 * hides the JSON, this one exposes it and lints it *as the operator types*, so
 * the E1 footgun (unquoted ``config_version: 1``) is caught under the cursor
 * instead of at gateway boot. The server stays the authority — the client only
 * offers immediate feedback and refuses to send a document that will not parse.
 */
import { cleanup, render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import ConfigView from './ConfigView';
import { stubDownloads, clearDownloads } from '../test/downloads';

// Mirrors the shape GET /api/config really emits: ae_title is a ReceiverConfig
// field, and GeneralConfig forbids extras, so a `general.ae_title` document can
// never come back from the gateway (config/__init__.py, extra="forbid").
const CLEAN_CONFIG = {
  config_version: '1.0',
  receiver: { ae_title: 'GATEWAY' },
  destinations: [
    { name: 'pacs-a', type: 'dicom', host: '10.0.0.5', port: 104, aet_target: 'ORTHANC' },
  ],
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

// The export endpoints answer with an attachment Content-Disposition that the
// client reads for the download filename.
function attachment(body: unknown, filename: string): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: {
      'Content-Type': 'application/json',
      'Content-Disposition': `attachment; filename="${filename}"`,
    },
  });
}

// One stub serves every route the page touches, keyed by URL and method.
function stubFetch(
  opts: {
    config?: unknown;
    warnings?: unknown[];
    saveResult?: unknown;
    importResult?: unknown;
  } = {},
) {
  const {
    config = CLEAN_CONFIG,
    warnings = [],
    saveResult = { status: 'ok', message: 'Config update saved', restart_required: true },
    importResult = {
      status: 'ok',
      message: 'Config import saved',
      restart_required: true,
      ignored_keys: [],
    },
  } = opts;
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    if (url.endsWith('/api/config/warnings')) return json({ warnings, config_version: '1.0' });
    if (url.endsWith('/api/config/import') && init?.method === 'POST') return json(importResult);
    if (url.endsWith('/api/config') && init?.method === 'PUT') return json(saveResult);
    if (url.endsWith('/api/config')) return json(config);
    if (url.endsWith('/api/config/export')) return attachment(config, 'mercure-gateway.json');
    if (url.endsWith('/api/diagnostics/export')) {
      return attachment(
        {
          config: {},
          audit: { events: [], count: 0, head_hash: 'abc123' },
          spool: { total: 0, states: {} },
          generated_at: '2026-09-25T00:00:00Z',
          version: '1.0.0',
        },
        'mercure-diagnostics.json',
      );
    }
    return json({});
  });
}

// The textarea is the editor's state; drive edits through it rather than
// reaching into component internals. Multi-line JSON is set via fireEvent:
// userEvent.type treats `{`/`}` as modifier syntax, and the point here is the
// lint reaction to the new text, not the keystroke path.
function setConfigText(text: string) {
  const ta = screen.getByLabelText('Configuration JSON') as HTMLTextAreaElement;
  fireEvent.change(ta, { target: { value: text } });
  return ta;
}

// Resolve once the async load has actually filled the editor. Waiting for the
// element alone races the fetch; waiting for a value from the loaded config
// (pacs-a) means a later load can no longer clobber the edit we are about to
// make.
async function loaded() {
  return (await screen.findByDisplayValue(/pacs-a/)) as HTMLTextAreaElement;
}

describe('ConfigView', () => {
  beforeEach(() => {
    // The export/import/diagnostics buttons hand a fetched blob to a
    // synthesized anchor; jsdom never implements the blob URL calls.
    stubDownloads();
    // Import replaces the running config, so the handler asks first. jsdom's
    // confirm returns true anyway but logs "not implemented" per call.
    vi.spyOn(window, 'confirm').mockReturnValue(true);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    clearDownloads();
    cleanup();
  });

  it('loads the running config into the editor', async () => {
    vi.stubGlobal('fetch', stubFetch());
    render(<ConfigView />);
    // Wait for the *value*, not the element: the textarea renders empty and
    // is filled by the async load, so findByLabelText alone races the fetch.
    const ta = (await screen.findByDisplayValue(/pacs-a/)) as HTMLTextAreaElement;
    expect(ta.value).toContain('"config_version": "1.0"');
    // Clean document, nothing dirty yet — Save stays disabled.
    expect(screen.getByText('Save')).toBeDisabled();
  });

  it('flags the E1 footgun — an unquoted config_version — as the operator types', async () => {
    vi.stubGlobal('fetch', stubFetch());
    render(<ConfigView />);
    await loaded();

    // Break the version: number instead of the string "1.0".
    setConfigText('{\n  "config_version": 1,\n  "general": {}\n}');

    expect(await screen.findByText(/must be a string/)).toBeInTheDocument();
    expect(screen.getByText(/crashes the gateway at boot/)).toBeInTheDocument();
  });

  it('blocks Save on a document that will not parse', async () => {
    vi.stubGlobal('fetch', stubFetch());
    render(<ConfigView />);
    await loaded();
    // Trailing comma — invalid JSON.
    setConfigText('{\n  "config_version": "1.0",\n}\n');

    expect(await screen.findByText(/Invalid JSON/)).toBeInTheDocument();
    // The button is gated on there being no parse error.
    expect(screen.getByText('Save')).toBeDisabled();
  });

  it('never PUTs a document with a parse error', async () => {
    const fetchStub = stubFetch();
    vi.stubGlobal('fetch', fetchStub);
    render(<ConfigView />);
    await loaded();
    setConfigText('{\n  "config_version": "1.0",\n}\n');
    await screen.findByText(/Invalid JSON/);

    // The GETs on mount are expected; the guarantee is that handleSave's guard
    // keeps an unparseable document from ever leaving the browser.
    const puts = fetchStub.mock.calls.filter(([, init]) => init?.method === 'PUT');
    expect(puts).toHaveLength(0);
  });

  it('saves a valid edited document and reports lint warnings from the server', async () => {
    const user = userEvent.setup();
    const fetchStub = stubFetch({
      saveResult: {
        status: 'ok',
        message: 'Config update saved',
        restart_required: true,
        warnings: [
          { path: 'destinations', message: 'Every destination is disabled.', severity: 'warning' },
        ],
      },
    });
    vi.stubGlobal('fetch', fetchStub);
    render(<ConfigView />);
    const ta = await loaded();

    // A valid edit that adds a destination name.
    await user.type(ta, ' ');

    await user.click(screen.getByText('Save'));

    await waitFor(() => {
      const puts = fetchStub.mock.calls.filter(([, init]) => init?.method === 'PUT');
      expect(puts).toHaveLength(1);
      const saved = JSON.parse(puts[0]![1]!.body as string);
      expect(saved.config_version).toBe('1.0');
    });
    // The server's warnings reach the banner alongside the success note.
    expect(await screen.findByText(/Every destination is disabled/)).toBeInTheDocument();
    expect(screen.getByText(/Config saved/)).toBeInTheDocument();
  });

  it('surfaces a server rejection under the Save button, not a bare failure', async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.endsWith('/api/config') && init?.method === 'PUT') {
          return json({ detail: 'Invalid config: port: Input should be greater than 0' }, 400);
        }
        if (url.endsWith('/api/config/warnings')) return json({ warnings: [], config_version: '1.0' });
        return json(CLEAN_CONFIG);
      }),
    );
    render(<ConfigView />);
    const ta = await loaded();
    await user.type(ta, ' ');
    await user.click(screen.getByText('Save'));

    expect(await screen.findByText(/Input should be greater than 0/)).toBeInTheDocument();
  });

  it('renders warnings against the running config on load', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({
        warnings: [{ path: 'forwarding_rules[0].targets', message: 'Rule targets unknown destination(s) ghost.', severity: 'warning' }],
      }),
    );
    render(<ConfigView />);
    expect(await screen.findByText(/unknown destination\(s\) ghost/)).toBeInTheDocument();
  });

  // ── Export / import / diagnostics (US-08c, S09-T5) ───────────────────
  // The controls the admin guide documents ("Config → Export", "Import",
  // "Config → Diagnostics") — review found the endpoints wired to no UI.

  it('downloads the redacted config on Export config', async () => {
    const user = userEvent.setup();
    const fetchStub = stubFetch();
    vi.stubGlobal('fetch', fetchStub);
    render(<ConfigView />);
    await loaded();

    await user.click(screen.getByText('Export config'));

    await waitFor(() => {
      const exports = fetchStub.mock.calls.filter(([url]) => url.toString().endsWith('/api/config/export'));
      expect(exports).toHaveLength(1);
    });
    // The body reached the blob URL path, so the fetch result was actually
    // handed to a download rather than only requested.
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/secrets are redacted in the downloaded file/)).toBeInTheDocument();
  });

  it('imports a config file, reports dropped keys, and reloads the editor', async () => {
    // Distinct from the pre-import document so the reload is observable.
    const IMPORTED = {
      config_version: '1.0',
      receiver: { ae_title: 'IMPORTED' },
      destinations: [],
    };
    let imported = false;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.endsWith('/api/config/import') && init?.method === 'POST') {
          imported = true;
          return json({
            status: 'ok',
            message: 'Config import saved',
            restart_required: true,
            ignored_keys: ['legacy.shim'],
          });
        }
        if (url.endsWith('/api/config/warnings')) return json({ warnings: [], config_version: '1.0' });
        if (url.endsWith('/api/config')) return json(imported ? IMPORTED : CLEAN_CONFIG);
        return json({});
      }),
    );
    render(<ConfigView />);
    await loaded();

    const input = screen.getByLabelText('Import configuration file') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File([JSON.stringify(CLEAN_CONFIG)], 'mercure-gateway.json', { type: 'application/json' })] },
    });

    // The server names the keys its schema did not carry.
    expect(await screen.findByText(/unknown key\(s\) not applied: legacy\.shim/)).toBeInTheDocument();
    // The editor now shows the imported config, not the document it replaced —
    // a later Save would otherwise silently revert the import.
    const ta = screen.getByLabelText('Configuration JSON') as HTMLTextAreaElement;
    expect(ta.value).toContain('IMPORTED');
    expect(ta.value).not.toContain('pacs-a');
    // Reloaded clean: nothing to save over the file just applied.
    expect(screen.getByText('Save')).toBeDisabled();
  });

  it('surfaces an import rejection from the server', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.endsWith('/api/config/import') && init?.method === 'POST') {
          return json({ detail: 'Unsupported config_version: 2.0. Expected \'1.0\'.' }, 400);
        }
        if (url.endsWith('/api/config/warnings')) return json({ warnings: [], config_version: '1.0' });
        if (url.endsWith('/api/config')) return json(CLEAN_CONFIG);
        return json({});
      }),
    );
    render(<ConfigView />);
    await loaded();

    const input = screen.getByLabelText('Import configuration file') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(['{"config_version": "2.0"}'], 'bad.json', { type: 'application/json' })] },
    });

    expect(await screen.findByText(/Unsupported config_version/)).toBeInTheDocument();
  });

  it('does not import when the operator cancels the confirmation', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    const fetchStub = stubFetch();
    vi.stubGlobal('fetch', fetchStub);
    render(<ConfigView />);
    await loaded();

    const input = screen.getByLabelText('Import configuration file') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File([JSON.stringify(CLEAN_CONFIG)], 'mercure-gateway.json', { type: 'application/json' })] },
    });

    await waitFor(() => {
      const imports = fetchStub.mock.calls.filter(
        ([url, init]) => url.toString().endsWith('/api/config/import') && init?.method === 'POST',
      );
      expect(imports).toHaveLength(0);
    });
  });

  it('downloads the support bundle from the Diagnostics card', async () => {
    const user = userEvent.setup();
    const fetchStub = stubFetch();
    vi.stubGlobal('fetch', fetchStub);
    render(<ConfigView />);
    await loaded();

    await user.click(screen.getByText('Download support bundle'));

    await waitFor(() => {
      const bundles = fetchStub.mock.calls.filter(([url]) =>
        url.toString().endsWith('/api/diagnostics/export'),
      );
      expect(bundles).toHaveLength(1);
    });
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/Support bundle downloaded/)).toBeInTheDocument();
  });

  it('reports an export failure without treating it as a save', async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.endsWith('/api/config/export')) return json({ detail: 'Not authenticated' }, 401);
        if (url.endsWith('/api/config/warnings')) return json({ warnings: [], config_version: '1.0' });
        if (url.endsWith('/api/config')) return json(CLEAN_CONFIG);
        return json({});
      }),
    );
    render(<ConfigView />);
    await loaded();

    await user.click(screen.getByText('Export config'));

    // 401 detail surfaces, and the failure is announced as an error, not a
    // status — the message no longer classifies by string prefix.
    expect(await screen.findByText(/Not authenticated/)).toBeInTheDocument();
    expect(screen.getByText(/Not authenticated/)).toHaveClass('error-banner');
    expect(screen.getByRole('alert')).toBeInTheDocument();
  });
});
