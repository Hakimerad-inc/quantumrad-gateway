// Route-level code splitting (batch W4): the page switch in App.tsx renders
// React.lazy views inside one Suspense boundary, with an ErrorBoundary that
// turns a rejected import into a readable message instead of a blank pane.
//
// jsdom resolves a dynamic import synchronously once the module is in the
// registry, so these tests assert on real rendered content (the stub-map
// pattern from a11y-scan.test.tsx) rather than on the presence of a Suspense
// node — finding the page's own text proves the lazy view came through the
// boundary, not that some boundary existed.

import { describe, it, expect, vi } from "vitest";
import { lazy, Suspense, type ReactNode } from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App, { Dashboard } from "../App";
import ErrorBoundary from "../ui/ErrorBoundary";

const STATUS = {
  version: "1.1.0-rc3", receiver: true, forwarder: true, report_retriever: true,
  hub_registered: true, hub_streaming: false, config_pending_restart: false,
};

function stubApi(map: Record<string, unknown>) {
  (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mockImplementation(async (url: string) => {
    const path = String(url).replace(/^https?:\/\/[^/]+/, "");
    for (const [key, val] of Object.entries(map)) {
      if (path.includes(key)) return new Response(JSON.stringify(val), { status: 200 });
    }
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  });
}

// The authenticated shell: checkAuth (AuthProvider) and the version probe
// (AppContent) both need system/status to answer 200.
function stubAuthenticated() {
  stubApi({ "system/status": STATUS });
}

describe("App page switch", () => {
  it("keeps Dashboard eager so first paint does not wait on a chunk", async () => {
    stubAuthenticated();
    render(<App />);
    // Dashboard is the default page, so it renders on first paint (App.tsx).
    await waitFor(() => expect(screen.getByText("Queue Overview")).toBeInTheDocument());
    expect(screen.getByText("System Status")).toBeInTheDocument();

    // The property that matters — "Dashboard is not behind a chunk boundary" —
    // cannot be observed behaviourally here: AuthProvider starts in its
    // loading state and only clears it in the async auth probe's finally
    // block, so Dashboard was never on the first committed render anyway, and
    // jsdom resolves an already-registered dynamic import synchronously enough
    // that a lazy Dashboard would still pass the assertions above. The thing
    // that IS observable about the decision is the wrapper itself: React.lazy
    // hands back a lazy-type object, never a function component. This pins the
    // App.tsx design decision ("Dashboard is deliberately NOT lazy") so a
    // future refactor that writes `const Dashboard = lazy(...)` fails here
    // instead of silently moving first paint behind a chunk load.
    expect(typeof Dashboard).toBe("function");
  });

  it("renders a lazy page through the Suspense boundary", async () => {
    stubApi({
      "system/status": STATUS,
      "logs": { lines: ["2026-09-18 INFO gateway started"], total_available: 1 },
    });
    const user = userEvent.setup();
    render(<App />);

    // Navigate via the sidebar, exactly as an operator would. findByRole
    // waits out the auth probe's loading state.
    const nav = await screen.findByRole("navigation");
    await user.click(within(nav).getByRole("link", { name: /logs/i }));

    // LogsView's own heading — present only if the lazy chunk resolved and
    // the view rendered. A Suspense node alone would not produce this.
    await waitFor(() =>
      expect(within(screen.getByRole("main")).getByText("Operations Log")).toBeInTheDocument(),
    );
  });

  it("surfaces a failed dynamic import as an actionable message, not a blank pane", async () => {
    // The mock location in setup.ts has no reload, so install one.
    const reload = vi.fn();
    Object.assign(window.location, { reload });

    // The shape Vite throws when a chunk 404s or the import is rejected.
    const BrokenPage = lazy(() =>
      Promise.reject(new TypeError("Failed to fetch dynamically imported module: /logs-abc123.js")),
    );

    render(
      <ErrorBoundary>
        <Suspense fallback={<div>Loading page…</div>}>
          <BrokenPage />
        </Suspense>
      </ErrorBoundary>,
    );

    // The message names the failure, and the recovery offered is a full
    // reload — an in-place "Reload panel" retry cannot clear React.lazy's
    // cached rejection, so it would just re-throw on every render.
    const banner = await screen.findByRole("alert");
    expect(banner.textContent).toContain("Failed to fetch dynamically imported module");
    const button = within(banner).getByRole("button", { name: /reload the gateway console/i });
    expect(button).toBeInTheDocument();

    await userEvent.setup().click(button);
    expect(reload).toHaveBeenCalledOnce();
  });

  it("keeps the in-place retry for a render crash", async () => {
    // A genuine render crash is recoverable by re-rendering, so it must keep
    // the original "Reload panel" path — the load-failure branch must not
    // swallow it. React.lazy is not involved here.
    function Boom(): ReactNode {
      throw new Error("the panel blew up");
    }

    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );

    const banner = await screen.findByRole("alert");
    expect(banner.textContent).toContain("This panel failed to render");
    expect(within(banner).getByRole("button", { name: /reload panel/i })).toBeInTheDocument();
  });
});
