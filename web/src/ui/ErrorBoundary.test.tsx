import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import ErrorBoundary from "./ErrorBoundary";

function Bomb({ message }: { message: string }): React.JSX.Element {
  throw new Error(message);
}

describe("ErrorBoundary", () => {
  afterEach(() => vi.restoreAllMocks());

  it("renders the fallback banner when a child throws", () => {
    // React logs the caught error to console.error; silence it for the assert.
    vi.spyOn(console, "error").mockImplementation(() => {});
    render(<ErrorBoundary><Bomb message="boom" /></ErrorBoundary>);
    expect(screen.getByRole("alert")).toHaveTextContent("This panel failed to render: boom");
  });

  // App.tsx keys the boundary by page name: navigating away remounts it with
  // fresh state, so a crashed panel does not poison the next one.
  it("resets its state when remounted with a fresh key", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const { rerender } = render(
      <ErrorBoundary key="old">
        <Bomb message="crash" />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    rerender(
      <ErrorBoundary key="new">
        <div>all good</div>
      </ErrorBoundary>,
    );
    expect(screen.getByText("all good")).toBeInTheDocument();
  });

  // Once state.error is set, a plain re-render of the same (still-throwing)
  // children keeps the banner — the boundary never retries a crashing panel.
  it("keeps showing the fallback while children keep throwing", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const { rerender } = render(
      <ErrorBoundary>
        <Bomb message="still broken" />
      </ErrorBoundary>,
    );
    rerender(
      <ErrorBoundary>
        <Bomb message="still broken" />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("still broken");
  });
});
