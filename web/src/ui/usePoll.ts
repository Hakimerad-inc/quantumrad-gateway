// Self-rescheduling poller (review P1-20).
//
// The pages that poll the API (LogsView, PipelineView) used bare
// `setInterval(load, N)`, which has three failure modes this hook closes:
//
//  1. **Stacking.** `setInterval` fires on schedule whether or not the
//     previous call finished. A slow backend or a stalled tab queues call
//     after call; they land out of order and each overwrites the last.
//  2. **Fetch races.** No request was cancellable, so a slow response could
//     land after a fresher one and render stale data on top of it — the
//     operator clicks "Refresh" and watches the screen go *backwards*.
//  3. **Unmount.** In-flight fetches completed after unmount and wrote
//     state into a component nobody is looking at.
//
// Each call is tied to an AbortController, the next call aborts the one
// before it, and the timer re-arms only after the call settles — so polls
// can never overlap. A hidden tab stops polling (no one is there to see it)
// and refetches the moment it returns.
import { useCallback, useEffect, useRef } from "react";

type PollFn = (signal: AbortSignal) => Promise<void> | void;

export interface PollOptions {
  /** Delay between one call *settling* and the next starting. */
  intervalMs: number;
  /** When false, polling stops; the last snapshot stays on screen. */
  enabled?: boolean;
  /** Values the poll depends on — a change refetches immediately. */
  deps?: unknown[];
}

export function usePoll(fn: PollFn, { intervalMs, enabled = true, deps = [] }: PollOptions) {
  // The latest callback, reached through a ref so a `limit`/`selection`
  // change doesn't churn the effect and restart the poll from scratch.
  const fnRef = useRef(fn);
  fnRef.current = fn;

  const controllerRef = useRef<AbortController | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Manual refresh (the "Refresh" button) reaches the current scheduler
  // through this ref, so the returned callback is stable across renders.
  const scheduleRef = useRef<() => void>(() => {});

  const refresh = useCallback(() => scheduleRef.current(), []);

  useEffect(() => {
    if (!enabled) return;

    let unmounted = false;

    const poll = async () => {
      // Latest-wins: supersede any call still in flight so a slow response
      // can never land after a fresher one.
      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;
      try {
        await fnRef.current(controller.signal);
      } finally {
        // Re-arm only if this call is still the current one (a newer poll,
        // a deps change, or cleanup below owns the chain from here) and
        // only while the tab is actually visible.
        if (!unmounted && controllerRef.current === controller && !document.hidden) {
          timerRef.current = setTimeout(poll, intervalMs);
        }
      }
    };

    scheduleRef.current = () => {
      clearTimeout(timerRef.current!);
      void poll();
    };

    // A hidden tab has no one to show the data to and the backend has better
    // things to do; pick the poll back up the instant it returns.
    const onVisibility = () => {
      if (!document.hidden) void poll();
    };
    document.addEventListener("visibilitychange", onVisibility);

    void poll();

    return () => {
      unmounted = true;
      document.removeEventListener("visibilitychange", onVisibility);
      clearTimeout(timerRef.current!);
      controllerRef.current?.abort();
    };
    // deps are the page's own state (log limit, current selection) — a
    // change must refetch, which the effect cleanup → poll cycle does.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, intervalMs, ...deps]);

  return refresh;
}
