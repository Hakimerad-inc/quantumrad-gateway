// One-shot async load with supersession (review P2-P3 backlog, Step 7).
//
// Seven pages hand-rolled `useState(loading/error) + useCallback(load) +
// useEffect`. The shape is NOT uniform, so this hook covers only what those
// loads share and leaves the rest where it is:
//
//  - QueueView refetches on `page` → `deps: [page]`.
//  - AuditView/ReportsView render an empty list until data lands and have no
//    loading flag at all → they ignore `loading` and read `data ?? []`.
//  - ReportsView's view()/doRefresh() are user-triggered, not mount-triggered
//    → they stay as they are; doRefresh calls `reload()`.
//
// What the hook adds over the hand-rolled version: the load is tied to an
// AbortController owned by the effect. A deps change or an unmount aborts the
// in-flight request, and a result that lands after either is dropped instead
// of overwriting fresher state — the race the boilerplate left open. usePoll
// closed it for pollers (LogsView, PipelineView); this closes it for the
// one-shot loads.
//
// What it deliberately does NOT cover, because converting would delete real
// behaviour rather than boilerplate:
//
//  - Polling. LogsView and PipelineView use usePoll, which keeps its own
//    abort plus document.hidden gating (P1-20). Those pages stay on it.
//  - Loads whose result feeds a stateful guard rather than plain data.
//    ConfigView's load does `setText(prev => prev ? prev : …)` so a slow
//    config fetch cannot clobber edits the operator is typing; the fetch
//    result there is an input to a decision about existing state, not a
//    value to store. DestinationsView's load resets `dirty`/`error` and
//    seeds a list the operator then mutates in place. Neither is a plain
//    data assignment, so neither migrates.
import { useCallback, useEffect, useRef, useState } from "react";
import { isAbortError } from "../api";

export interface UseAsyncOptions {
  /** Values the load depends on — a change aborts the fetch in flight and
   * reloads. QueueView passes `[page]`. */
  deps?: unknown[];
}

export interface UseAsyncResult<T> {
  /** null until the first load settles. */
  data: T | null;
  /** true from mount (and from any reload) until the load settles. */
  loading: boolean;
  /** The message of a genuine failure; null while loading and on success. */
  error: string | null;
  /** Write to the same banner the load reports to. QueueView's retry/enqueue
   * actions are user-triggered, not loads, but their failures belong on the
   * same line as a failed fetch — and the next successful reload clears them,
   * as it did before. */
  setError: (message: string | null) => void;
  /** Re-run the load, aborting whatever is in flight. Settles when the load
   * does; resolves immediately (with no state write) if a newer load has
   * already superseded it. */
  reload: () => Promise<void>;
}

export function useAsync<T>(
  fn: (signal: AbortSignal) => Promise<T>,
  { deps = [] }: UseAsyncOptions = {},
): UseAsyncResult<T> {
  const [data, setData] = useState<T | null>(null);
  // QueueView renders a loading row from the first paint; AuditView and
  // ReportsView simply ignore this and render `data ?? []`.
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // The latest callback, reached through a ref so a deps change does not
  // churn the effect and restart the load from scratch (same pattern as
  // usePoll).
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const controllerRef = useRef<AbortController | null>(null);

  const run = useCallback(async () => {
    // Latest-wins: a fetch still in flight when a newer one starts must not
    // land after it and render stale data on top.
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;

    setLoading(true);
    setError(null);
    try {
      const result = await fnRef.current(controller.signal);
      // The promise resolves regardless of whether its caller still wants the
      // answer — a page change or an unmount has already aborted this
      // controller. Writing anyway would clobber the fresher render.
      if (controller.signal.aborted) return;
      setData(result);
    } catch (e) {
      // Suppression is not a failure: this controller was aborted
      // deliberately. A genuine error must still surface — treating it as an
      // abort would silently drop a failed fetch (api.ts:isAbortError).
      if (controller.signal.aborted || isAbortError(e)) return;
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      // A superseded or unmounted load must not clear a newer load's flag.
      if (!controller.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void run();
    return () => {
      // Unmount, or a deps change: nothing this controller is still fetching
      // can land anywhere that still wants it.
      controllerRef.current?.abort();
    };
    // `run` is stable; deps are the page's own state (QueueView's page) and a
    // change must refetch, which the cleanup → run cycle does.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run, ...deps]);

  return { data, loading, error, setError, reload: run };
}
