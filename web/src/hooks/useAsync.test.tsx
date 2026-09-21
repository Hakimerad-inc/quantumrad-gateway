/**
 * useAsync tests — the load must supersede, not stack or clobber.
 *
 * The hand-rolled `useCallback(load) + useEffect` this hook replaced had one
 * open race: nothing cancelled the in-flight fetch, so a QueueView page change
 * (or an unmount) let a slow response land afterwards and overwrite the
 * fresher render. These tests pin the behaviours that close it.
 *
 * Every assertion here is one that goes red if the hook regresses. Verified
 * by mutating useAsync and re-running this file — each mutation below was
 * confirmed to turn at least one test red, then reverted:
 *
 *  - drop the post-await `controller.signal.aborted` guard → the
 *    superseded-result test renders "STALE-page-1" instead of "page-2";
 *  - drop the abort in the effect cleanup → the unmount test sees an
 *    un-aborted signal;
 *  - drop `isAbortError` from the catch → the bare-AbortError test reports a
 *    failure the hook should have suppressed;
 *  - make the catch return unconditionally → the genuine-error test reports
 *    nothing where it should say "backend down".
 *
 * Note on the unmount case: React 18 silently drops a state write to an
 * unmounted component, so a text-only assertion there passes with or without
 * the guard — the signal assertion is what makes it able to fail.
 */
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useAsync, type UseAsyncOptions } from "./useAsync";

interface HarnessProps<T> extends UseAsyncOptions {
  fn: (signal: AbortSignal) => Promise<T>;
  render?: (value: T | null, error: string | null, loading: boolean) => string;
}

/** Renders the hook's output as text so the assertions see state changes (or
 * their absence) rather than poking at React internals. */
function Harness<T>({ fn, render: renderValue, ...opts }: HarnessProps<T>) {
  const { data, loading, error, reload } = useAsync<T>(fn, opts);
  const body = renderValue ? renderValue(data, error, loading) : String(data);
  return (
    <div>
      <span data-testid="body">{body}</span>
      <span data-testid="loading">{loading ? "loading" : "idle"}</span>
      <button onClick={() => void reload()}>reload</button>
    </div>
  );
}

interface Pending<T> {
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
  signal: AbortSignal;
}

/** A load that only settles when the test says so, mimicking a slow fetch.
 * `calls` is every load started, oldest first, so a test can settle an older
 * one after a newer one and watch what happens. */
function controllable<T>(): {
  fn: (signal: AbortSignal) => Promise<T>;
  calls: Pending<T>[];
} {
  const calls: Pending<T>[] = [];
  return {
    fn: (signal) =>
      new Promise<T>((resolve, reject) => {
        calls.push({ resolve, reject, signal });
      }),
    calls,
  };
}

/** Settle the call at `index` (0 = oldest). */
function settle<T>(calls: Pending<T>[], index: number, value: T): void {
  calls[index]?.resolve(value);
}

describe("useAsync", () => {
  afterEach(cleanup);

  it("fires the load on mount and exposes the resolved data", async () => {
    const fn = vi.fn(async () => "payload");
    const { getByTestId } = render(<Harness fn={fn} render={(v) => `data=${v}`} />);

    await waitFor(() => expect(getByTestId("body").textContent).toBe("data=payload"));
    expect(fn).toHaveBeenCalledOnce();
    expect(getByTestId("loading").textContent).toBe("idle");
  });

  it("refetches when a dependency changes (QueueView's page case)", async () => {
    let page = 1;
    const calls: number[] = [];
    const renderValue = (v: string | null) => `data=${v}`;
    const mk = () => async () => {
      calls.push(page);
      return `page-${page}`;
    };

    const { getByTestId, rerender } = render(
      <Harness<string> fn={mk()} deps={[page]} render={renderValue} />,
    );
    await waitFor(() => expect(getByTestId("body").textContent).toBe("data=page-1"));

    page = 2;
    rerender(<Harness<string> fn={mk()} deps={[page]} render={renderValue} />);
    await waitFor(() => expect(getByTestId("body").textContent).toBe("data=page-2"));

    expect(calls).toEqual([1, 2]);
  });

  it("drops a superseded result that lands after a dependency change", async () => {
    // Two pages, two in-flight fetches. The page-1 fetch is slower; without
    // the abort guard it resolves last and renders page 1's data under a
    // "page 2" heading — the operator watches the table go backwards.
    const slow = controllable<string>();
    const fast = controllable<string>();
    let page = 1;
    const mk = () => (page === 1 ? slow.fn : fast.fn);

    const { getByTestId, rerender } = render(
      <Harness fn={mk()} deps={[page]} render={(v) => `data=${v}`} />,
    );
    expect(slow.calls).toHaveLength(1);

    // Page 1's fetch is still in flight when the page changes.
    page = 2;
    rerender(<Harness fn={mk()} deps={[page]} render={(v) => `data=${v}`} />);
    expect(fast.calls).toHaveLength(1);
    expect(slow.calls[0]!.signal.aborted).toBe(true);

    // The newer fetch lands first...
    settle(fast.calls, 0, "page-2");
    await waitFor(() => expect(getByTestId("body").textContent).toBe("data=page-2"));

    // ...and the stale one resolving afterwards must not overwrite it.
    settle(slow.calls, 0, "STALE-page-1");
    await new Promise((r) => setTimeout(r, 10));
    expect(getByTestId("body").textContent).toBe("data=page-2");
  });

  it("aborts on unmount and a late resolution does not update state", async () => {
    const ctrl = controllable<string>();
    const { getByTestId, unmount } = render(
      <Harness fn={ctrl.fn} render={(v) => `data=${v}`} />,
    );

    await waitFor(() => expect(getByTestId("loading").textContent).toBe("loading"));
    const body = getByTestId("body");
    const beforeUnmount = body.textContent;
    const inFlight = ctrl.calls[0]!;
    unmount();

    // The cancellation is observable here, and it is the part that matters:
    // the request the hook owns is withdrawn so its response cannot land.
    expect(inFlight.signal.aborted).toBe(true);

    // Resolving anyway must not update state. React 18 silently drops updates
    // to an unmounted component, so the text assertion alone would pass with
    // or without the guard — the signal check above is what makes this test
    // able to fail, and the two together pin the contract.
    settle(ctrl.calls, 0, "late");
    await new Promise((r) => setTimeout(r, 10));
    expect(body.textContent).toBe(beforeUnmount);
  });

  it("surfaces a genuine error and does not classify it as an abort", async () => {
    const ctrl = controllable<string>();
    const { getByTestId } = render(
      <Harness fn={ctrl.fn} render={(_v, error) => (error ? `error=${error}` : "no-error")} />,
    );

    // A real failure — not the DOMException AbortError a cancellation raises.
    ctrl.calls[0]!.reject(new Error("backend down"));

    await waitFor(() => expect(getByTestId("body").textContent).toBe("error=backend down"));
    expect(getByTestId("loading").textContent).toBe("idle");
  });

  it("treats a caller-raised AbortError as suppression, not a failure", async () => {
    // The hook threads its signal into the fetch; when a newer load supersedes
    // this one the fetch rejects with AbortError. That is a deliberate
    // cancellation and must never reach the error banner — otherwise a fast
    // page change reports the request it cancelled as a failure, the failure
    // mode usePoll closed for pollers (P1-20).
    const seen: AbortSignal[] = [];
    const { getByText, getByTestId } = render(
      <Harness
        fn={(signal) => {
          seen.push(signal);
          return new Promise<string>((_resolve, reject) => {
            signal.addEventListener("abort", () =>
              reject(new DOMException("Aborted", "AbortError")),
            );
          });
        }}
        render={(_v, error) => (error ? `error=${error}` : "no-error")}
      />,
    );

    expect(seen[0]!.aborted).toBe(false);

    // A reload supersedes the mount-time load; the first signal is aborted.
    getByText("reload").click();
    await waitFor(() => expect(seen[0]!.aborted).toBe(true));

    // Suppressed: no error banner for the deliberately cancelled request.
    await new Promise((r) => setTimeout(r, 10));
    expect(getByTestId("body").textContent).toBe("no-error");
  });

  it("suppresses an AbortError that arrives without an aborted signal", async () => {
    // api.ts classifies a DOMException named AbortError as cancellation
    // (api.ts:isAbortError). A fetch can reject that way through a path the
    // hook's own controller did not drive — the signal is still un-aborted
    // when the rejection lands — so the classification is what has to save it
    // from rendering as a failure. Removing the isAbortError check (leaving
    // only the signal check) makes this test fail.
    const ctrl = controllable<string>();
    const { getByTestId } = render(
      <Harness fn={ctrl.fn} render={(_v, error) => (error ? `error=${error}` : "no-error")} />,
    );

    // The controller is still live — nothing superseded or unmounted.
    expect(ctrl.calls[0]!.signal.aborted).toBe(false);
    ctrl.calls[0]!.reject(new DOMException("Aborted", "AbortError"));

    await new Promise((r) => setTimeout(r, 10));
    expect(getByTestId("body").textContent).toBe("no-error");
  });

  it("reload re-runs the load and aborts the one in flight", async () => {
    const calls: string[] = [];
    const { getByTestId, getByText } = render(
      <Harness
        fn={async () => {
          calls.push("call");
          await new Promise((r) => setTimeout(r, 0));
          return `attempt-${calls.length}`;
        }}
        render={(v) => `data=${v}`}
      />,
    );
    await waitFor(() => expect(getByTestId("body").textContent).toBe("data=attempt-1"));

    await act(async () => {
      getByText("reload").click();
      await new Promise((r) => setTimeout(r, 20));
    });
    expect(getByTestId("body").textContent).toBe("data=attempt-2");
    expect(calls).toHaveLength(2);
  });
});
