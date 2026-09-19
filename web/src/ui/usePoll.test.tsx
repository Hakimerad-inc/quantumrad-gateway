/**
 * usePoll tests — the poller cannot stack, supersede, or leak past unmount
 * (review P1-20).
 *
 * Before this hook, LogsView and PipelineView polled with bare
 * `setInterval(load, N)`: no request was cancellable, and the timer fired
 * whether or not the previous call had finished. These tests pin the three
 * behaviours that removes: no overlapping calls, an in-flight fetch aborted
 * the instant a newer one starts, and nothing scheduled after unmount.
 */
import { render, cleanup } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { usePoll, type PollOptions } from './usePoll';

interface Stats {
  calls: number;
  concurrent: number;
  maxConcurrent: number;
}

// Every signal the poller handed out, in call order — captured at call start
// so an aborted-but-unsettled call is still observable.
const seenSignals: AbortSignal[] = [];

// The hidden-tab test overrides document.hidden; the override and the
// assertions share this value.
let hiddenValue = false;

type HarnessProps = Omit<PollOptions, 'fn'> & { stats: Stats };

function Harness({ stats, ...opts }: HarnessProps) {
  const refresh = usePoll(
    async (signal: AbortSignal) => {
      seenSignals.push(signal);
      stats.calls += 1;
      stats.concurrent += 1;
      stats.maxConcurrent = Math.max(stats.maxConcurrent, stats.concurrent);
      try {
        // A call slower than the interval is exactly the stacking case.
        await new Promise((resolve) => setTimeout(resolve, 50));
      } finally {
        stats.concurrent -= 1;
      }
    },
    opts,
  );
  return <button onClick={refresh}>refresh</button>;
}

function newStats(): Stats {
  return { calls: 0, concurrent: 0, maxConcurrent: 0 };
}

function setHidden(value: boolean) {
  hiddenValue = value;
  document.dispatchEvent(new Event('visibilitychange'));
}

describe('usePoll', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    seenSignals.length = 0;
    hiddenValue = false;
    Object.defineProperty(document, 'hidden', {
      configurable: true,
      get: () => hiddenValue,
    });
  });
  afterEach(() => {
    vi.useRealTimers();
    cleanup();
  });

  it('never overlaps two calls — the timer re-arms after settlement, not on a fixed cadence', async () => {
    const stats = newStats();
    render(<Harness stats={stats} intervalMs={10} />);

    // Well past several intervals while every call is still slow. The async
    // variant awaits the promise continuations that re-arm the chain.
    await vi.advanceTimersByTimeAsync(500);

    expect(stats.calls).toBeGreaterThan(1);
    // setInterval would have had several in flight at once here.
    expect(stats.maxConcurrent).toBe(1);
  });

  it('aborts the in-flight call when a newer one starts', () => {
    const stats = newStats();
    const { getByText } = render(<Harness stats={stats} intervalMs={10} />);

    const first = seenSignals[0]!;
    expect(first.aborted).toBe(false);

    // A manual refresh supersedes the mount-time call before it settles.
    getByText('refresh').click();
    expect(first.aborted).toBe(true);
  });

  it('aborts the in-flight call on unmount and schedules nothing after', () => {
    const stats = newStats();
    const { unmount } = render(<Harness stats={stats} intervalMs={10} />);
    const inFlight = seenSignals[0]!;

    unmount();
    expect(inFlight.aborted).toBe(true);

    const callsAtUnmount = stats.calls;
    vi.advanceTimersByTime(500);
    expect(stats.calls).toBe(callsAtUnmount);
  });

  it('stops polling while the tab is hidden and resumes when it returns', () => {
    const stats = newStats();
    render(<Harness stats={stats} intervalMs={10} />);

    setHidden(true);
    const callsWhenHidden = stats.calls;
    vi.advanceTimersByTime(500);
    // A hidden tab has no one to show the data to.
    expect(stats.calls).toBe(callsWhenHidden);

    setHidden(false);
    expect(stats.calls).toBeGreaterThan(callsWhenHidden);
  });

  it('does not poll while disabled, and starts when enabled', () => {
    const stats = newStats();
    const { rerender } = render(<Harness stats={stats} intervalMs={10} enabled={false} />);

    vi.advanceTimersByTime(200);
    expect(stats.calls).toBe(0);

    rerender(<Harness stats={stats} intervalMs={10} enabled={true} />);
    expect(stats.calls).toBe(1);
  });

  it('refetches immediately when a dep changes', () => {
    const stats = newStats();
    const { rerender } = render(<Harness stats={stats} intervalMs={10} deps={['a']} />);

    expect(stats.calls).toBe(1);
    rerender(<Harness stats={stats} intervalMs={10} deps={['b']} />);
    expect(stats.calls).toBe(2);
  });

  it('cancels the armed timer when the tab hides', async () => {
    // The visibility listener used to only *re-arm* on the hidden branch —
    // going hidden cancelled nothing, so a timer already armed still fired
    // and polled a tab nobody was looking at.
    const stats = newStats();
    render(<Harness stats={stats} intervalMs={100} />);

    // Let the mount-time call settle and arm its timer.
    await vi.advanceTimersByTimeAsync(60);
    const armed = stats.calls;

    setHidden(true);
    await vi.advanceTimersByTimeAsync(500);
    expect(stats.calls).toBe(armed);
  });

  it('does not fork a second poll chain when the tab returns while a timer is armed', async () => {
    // poll() had three entry points (mount, visibility, refresh) and only
    // refresh cleared the timer. Returning while one was armed started a
    // second self-perpetuating chain; timerRef was overwritten so unmount
    // cleared only the surviving handle and the orphan kept fetching. Every
    // tab switch added another chain, so the poll rate compounded.
    const stats = newStats();
    render(<Harness stats={stats} intervalMs={100} />);

    await vi.advanceTimersByTimeAsync(60);
    const armed = stats.calls;

    // Leave and come back while that timer is still pending.
    setHidden(true);
    setHidden(false);

    await vi.advanceTimersByTimeAsync(600);
    // One chain: ~150 ms per call (100 ms interval + 50 ms settlement) over
    // 600 ms. Two chains roughly doubled this and could overlap.
    expect(stats.calls - armed).toBeLessThan(8);
    expect(stats.maxConcurrent).toBe(1);
  });
});
