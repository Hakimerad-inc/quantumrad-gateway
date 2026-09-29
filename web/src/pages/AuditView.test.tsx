/**
 * AuditView tests — the chain-verify page and its export control.
 *
 * The audit export (GET /api/audit/export) ships the events with their chain
 * hashes and the head hash for offline anchoring. Review found the endpoint
 * wired to no UI: the page had exactly one button. These cover the export's
 * happy path and its failure reporting, which is kept separate from the
 * list-load error so the banner text stays true.
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import AuditView from './AuditView';
import { stubDownloads, clearDownloads } from '../test/downloads';

const EVENTS = [
  { id: 1, ts: '2026-09-24T00:00:00Z', event: 'STUDY_RECEIVED', detail: 'a1', user: 'system', hash: 'aaa' },
  { id: 2, ts: '2026-09-24T00:01:00Z', event: 'STUDY_SENT', detail: 'b2', user: 'system', hash: 'bbb' },
];

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function attachment(body: unknown, filename: string): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: {
      'Content-Type': 'application/json',
      'Content-Disposition': `attachment; filename="${filename}"`,
    },
  });
}

// The list fetch carries a limit query (?limit=200), so match on the path
// rather than the full URL.
function pathOf(input: RequestInfo | URL): string {
  const url = typeof input === 'string' ? input : input.toString();
  return url.split('?')[0];
}

describe('AuditView', () => {
  beforeEach(() => {
    stubDownloads();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    clearDownloads();
    cleanup();
  });

  it('renders the audit events', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        if (pathOf(input).endsWith('/api/audit')) return json(EVENTS);
        return json({});
      }),
    );
    render(<AuditView />);

    expect(await screen.findByText('STUDY_RECEIVED')).toBeInTheDocument();
    expect(screen.getByText('2 events (last 200)')).toBeInTheDocument();
  });

  it('downloads the audit chain on Export audit log', async () => {
    const user = userEvent.setup();
    const fetchStub = vi.fn(async (input: RequestInfo | URL) => {
      if (pathOf(input).endsWith('/api/audit/export')) {
        return attachment({ events: EVENTS, count: EVENTS.length }, 'audit-log.json');
      }
      if (pathOf(input).endsWith('/api/audit')) return json(EVENTS);
      return json({});
    });
    vi.stubGlobal('fetch', fetchStub);
    render(<AuditView />);
    await screen.findByText('STUDY_RECEIVED');

    await user.click(screen.getByText('Export audit log'));

    await waitFor(() => {
      const exports = fetchStub.mock.calls.filter(([url]) => url.toString().endsWith('/api/audit/export'));
      expect(exports).toHaveLength(1);
    });
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
    // An export failure would reuse the list's error banner; a clean export
    // leaves the page free of any error text.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('reports an export failure without claiming the load failed', async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        if (pathOf(input).endsWith('/api/audit/export')) return json({ detail: 'Disk read error' }, 500);
        if (pathOf(input).endsWith('/api/audit')) return json(EVENTS);
        return json({});
      }),
    );
    render(<AuditView />);
    await screen.findByText('STUDY_RECEIVED');

    await user.click(screen.getByText('Export audit log'));

    const banner = await screen.findByText(/Disk read error/);
    // The banner names the export, not the audit log load it did not fail.
    expect(banner.textContent).toContain('Audit export failed');
  });
});
