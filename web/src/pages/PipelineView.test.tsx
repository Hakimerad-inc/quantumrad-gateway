/**
 * PipelineView tests — the selection poller (review P1-20).
 *
 * PipelineView runs two polls: the live snapshot and the selected
 * destination/study detail panel. The detail poll used to guard its state
 * writes with a `cancelled` flag while still firing on a fixed `setInterval`;
 * switching selection mid-fetch left the previous fetch free to write a
 * different study's detail into the panel. Both polls now go through
 * usePoll, so a selection change aborts the fetch it replaces.
 */
import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import PipelineView from './PipelineView';

const FETCH_DELAY_MS = 1000;

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

// Same semantics as a real aborted fetch: settle after the delay, or reject
// the moment the caller's AbortSignal fires.
function slowJson(body: unknown, signal?: AbortSignal): Promise<Response> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => resolve(json(body)), FETCH_DELAY_MS);
    signal?.addEventListener('abort', () => {
      clearTimeout(timer);
      reject(new DOMException('Aborted', 'AbortError'));
    });
  });
}

const SNAPSHOT = {
  generated_at: '2026-09-19T12:00:00Z',
  components: { receiver: true, forwarder: true },
  receiver_counts: { received_last_hour: 3 },
  queue: { queued: 0, sending: 0, error: 0, failed: 0 },
  destinations: [
    {
      name: 'pacs-a',
      host: '10.0.0.5',
      port: 104,
      aet: 'ORTHANC',
      routes: { sending: 0, waiting: 0, error: 0 },
      health: null,
    },
  ],
};

// Signals per endpoint, so a test can assert which request was aborted.
const signals: Record<string, AbortSignal[]> = { studies: [], detail: [], timeline: [] };

function stubApi() {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const signal = (init?.signal as AbortSignal) ?? null!;
    if (url.endsWith('/api/pipeline')) return json(SNAPSHOT);
    // Order matters: /api/studies/7/detail also contains "/studies".
    if (url.endsWith('/detail')) {
      signals.detail.push(signal);
      return slowJson({ id: 7, accession: 'ACC-1', study_uid: '1.2.3', state: 'SENT', routes: [] }, signal);
    }
    if (url.endsWith('/timeline')) {
      signals.timeline.push(signal);
      return slowJson([], signal);
    }
    if (url.includes('/destinations/') && url.includes('/studies')) {
      signals.studies.push(signal);
      return slowJson([{ route_id: 1, study_id: 7, accession: 'ACC-1', patient_name: 'Doe^Jane', modality: 'CT', status: 'complete', attempts: 1, updated_at: '2026-09-19T12:00:00Z' }], signal);
    }
    return json({});
  });
}

const settle = async () => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(FETCH_DELAY_MS);
  });
};

describe('PipelineView', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    Object.values(signals).forEach((arr) => arr.splice(0));
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    cleanup();
  });

  it('renders the snapshot and opens a destination panel', async () => {
    vi.stubGlobal('fetch', stubApi());
    render(<PipelineView />);

    await settle();
    expect(await screen.findByText('pacs-a')).toBeInTheDocument();

    await userEvent.setup({ advanceTimers: vi.advanceTimersByTime }).click(
      screen.getByRole('button', { name: 'pacs-a' }),
    );
    await settle();
    expect(await screen.findByText('Doe^Jane')).toBeInTheDocument();
  });

  it('aborts the panel fetch when the selection is cleared', async () => {
    vi.stubGlobal('fetch', stubApi());
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(<PipelineView />);
    await settle();

    await user.click(screen.getByRole('button', { name: 'pacs-a' }));
    expect(signals.studies).toHaveLength(1);
    expect(signals.studies[0]!.aborted).toBe(false);

    // Leave the panel while its fetch is still in flight.
    await user.click(screen.getByRole('button', { name: 'Back to pipeline' }));

    expect(signals.studies[0]!.aborted).toBe(true);
  });

  it('aborts a study detail fetch when the selection moves elsewhere', async () => {
    // The old `cancelled` flag guarded the state writes, but the fetch
    // itself ran to completion and could paint a study's timeline into a
    // panel that no longer showed it.
    vi.stubGlobal('fetch', stubApi());
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(<PipelineView />);
    await settle();

    await user.click(screen.getByRole('button', { name: 'pacs-a' }));
    await settle();
    // Open the study row — its detail + timeline are the slow pair.
    await user.click(screen.getByText('Doe^Jane'));
    expect(signals.detail[0]!.aborted).toBe(false);

    // The pair is still in flight; re-selecting the destination supersedes
    // the study selection and aborts it.
    await user.click(screen.getByRole('button', { name: 'pacs-a' }));

    expect(signals.detail[0]!.aborted).toBe(true);
    // The panel fetches detail then timeline in sequence, so aborting the
    // first prevents the second from ever going out — the pair lands
    // together or not at all.
    expect(signals.timeline).toHaveLength(0);
  });

  it('does not stack snapshot polls when a response outlasts the interval', async () => {
    let calls = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (!url.endsWith('/api/pipeline')) return json({});
        calls += 1;
        return slowJson(SNAPSHOT, init?.signal as AbortSignal);
      }),
    );
    render(<PipelineView />);

    // The poll interval is 2 s; 6 s pass while every response takes 1 s.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6_000);
    });

    expect(calls).toBeGreaterThan(1);
    expect(calls).toBeLessThan(10);
  });
});
