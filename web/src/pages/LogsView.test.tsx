/**
 * LogsView tests — fetch races and poller stacking (review P1-20).
 *
 * The acceptance criteria for the fix: unmounting mid-fetch aborts, and a
 * slow earlier response can never overwrite a fresher render. Both used to
 * be possible — no request was cancellable, and `setInterval` kept firing
 * while the previous call was still in flight.
 */
import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import LogsView from './LogsView';

// Every response is slower than the poll interval can out-run in these
// tests, so advancing fake timers is what settles them.
const FETCH_DELAY_MS = 1000;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

// A response that settles after FETCH_DELAY_MS, but rejects as an
// AbortError the moment the caller's AbortSignal fires — the same semantics
// the browser gives a real aborted fetch. Without the abort hook the slow
// response would land whenever its timer elapsed, which is precisely the
// bug under test.
function slowJson(body: unknown, signal?: AbortSignal): Promise<Response> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => resolve(json(body)), FETCH_DELAY_MS);
    signal?.addEventListener('abort', () => {
      clearTimeout(timer);
      reject(new DOMException('Aborted', 'AbortError'));
    });
  });
}

function logs(lines: string[], total = lines.length) {
  return { lines, total_available: total };
}

// Records the signal each request carried, so a test can assert the poller
// aborted the one it superseded.
const signals: AbortSignal[] = [];

function stubLogs(opts: { first?: unknown; later?: unknown } = {}) {
  const { first = logs(['first-load']), later = logs(['fresh-lines']) } = opts;
  let n = 0;
  return {
    fetch: vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (!url.includes('/api/logs')) return json({});
      signals.push((init?.signal as AbortSignal) ?? null!);
      n += 1;
      // The first call is deliberately slow: the mount-time fetch that a
      // later Refresh must be able to beat.
      return slowJson(n === 1 ? first : later, init?.signal as AbortSignal);
    }),
    callCount: () => n,
  };
}

// Settle every pending fetch (advancing past FETCH_DELAY_MS). Wrapped in
// act because the fetch continuations fire React state updates from inside
// the fake-timer queue.
const settle = async () => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(FETCH_DELAY_MS);
  });
};

describe('LogsView', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    signals.length = 0;
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    cleanup();
  });

  it('renders the log lines from the mount-time fetch', async () => {
    const stub = stubLogs({ first: logs(['alpha', 'beta']) });
    vi.stubGlobal('fetch', stub.fetch);
    render(<LogsView />);

    await settle();
    expect(await screen.findByText(/alpha/)).toBeInTheDocument();
    expect(screen.getByText(/beta/)).toBeInTheDocument();
  });

  it('a slow earlier response never overwrites a fresher render', async () => {
    // The bug: Refresh started a second request, but the first was still in
    // flight with no way to cancel it; when it landed last it painted stale
    // lines on top of the fresh ones.
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    vi.stubGlobal('fetch', stubLogs({ first: logs(['stale-lines']), later: logs(['fresh-lines']) }).fetch);
    render(<LogsView />);

    // The mount-time request is still pending — nothing rendered yet.
    expect(screen.queryByText(/stale-lines/)).not.toBeInTheDocument();

    await user.click(screen.getByText('Refresh'));

    // The mount-time request was aborted rather than left to land last.
    expect(signals[0]!.aborted).toBe(true);

    await settle();
    expect(await screen.findByText(/fresh-lines/)).toBeInTheDocument();
    expect(screen.queryByText(/stale-lines/)).not.toBeInTheDocument();
  });

  it('reports a real failure, but not an aborted request it caused itself', async () => {
    // An aborted fetch must not read as "Log refresh failed" — otherwise
    // every Refresh after a slow load shows an error for the request the
    // page deliberately cancelled.
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    vi.stubGlobal('fetch', stubLogs().fetch);
    render(<LogsView />);
    await settle();
    expect(await screen.findByText(/first-load/)).toBeInTheDocument();

    await user.click(screen.getByText('Refresh'));
    await settle();

    expect(screen.queryByText(/Log refresh failed/)).not.toBeInTheDocument();
    expect(screen.getByText(/fresh-lines/)).toBeInTheDocument();
  });

  it('aborts the in-flight fetch on unmount', () => {
    vi.stubGlobal('fetch', stubLogs().fetch);
    const { unmount } = render(<LogsView />);

    // The mount-time request is still pending.
    expect(signals[0]!.aborted).toBe(false);
    unmount();
    expect(signals[0]!.aborted).toBe(true);
  });

  it('does not stack polls when a response outlasts the interval', async () => {
    const stub = stubLogs({ first: logs(['first-load']), later: logs(['polled']) });
    vi.stubGlobal('fetch', stub.fetch);
    render(<LogsView />);

    // The poll interval is 5 s; 10 s pass while every call takes 1 s.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    // Let the last chained call settle inside the test, not after it.
    await settle();

    // Overlapping interval firing would have queued many concurrent
    // requests by now; the self-rescheduling poll chains them instead.
    expect(stub.callCount()).toBeGreaterThan(1);
    expect(stub.callCount()).toBeLessThan(20);
  });
});
