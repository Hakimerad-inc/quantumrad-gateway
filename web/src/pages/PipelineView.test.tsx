import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import PipelineView from "./PipelineView";
import type { PipelineSnapshot } from "../api";

// M8: PipelineView (the largest view, 317 LOC) previously had zero coverage.
// These tests pin the flow-diagram skeleton, the error banner on a failed
// snapshot fetch, and the live-poll toggle.

function snapshot(overrides: Partial<PipelineSnapshot> = {}): PipelineSnapshot {
  return {
    generated_at: "2026-09-09T12:00:00Z",
    components: { receiver: true, forwarder: true, reports: false },
    receiver_counts: { received_last_hour: 3 },
    queue: {
      total: 5, queued: 2, sending: 1, sent: 1, error: 1, failed: 0,
    },
    destinations: [
      {
        name: "pacs",
        type: "dicom",
        host: "127.0.0.1",
        port: 11112,
        aet: "PACS",
        routes: { complete: 2, sending: 0, waiting: 1, error: 0 },
        last_activity: "2026-09-09 12:00:00",
        health: { status: "ok", checked_at: "2026-09-09T12:00:00Z", latency_ms: 12 },
      },
    ],
    ...overrides,
  };
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200 });
}

describe("PipelineView", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("renders the flow diagram with component and destination nodes", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(snapshot()));

    render(<PipelineView />);

    const diagram = await screen.findByRole("img", { name: /gateway process flow/i });
    expect(diagram).toBeInTheDocument();
    // Component labels from the snapshot.
    expect(screen.getByText("Receiver")).toBeInTheDocument();
    expect(screen.getByText("Forwarder")).toBeInTheDocument();
    expect(screen.getByText("Spool")).toBeInTheDocument();
    // Destination node with its route lines (host/port render only in the
    // destination drill-down panel, not the diagram — assert the route lines).
    expect(screen.getByText("pacs")).toBeInTheDocument();
    expect(screen.getAllByText(/✔ 2 sent/).length).toBeGreaterThan(0);
  });

  it("renders an error banner when the snapshot fetch fails (M8)", async () => {
    vi.mocked(fetch).mockRejectedValueOnce(new Error("boom"));

    render(<PipelineView />);

    await waitFor(() => {
      expect(screen.getByText(/Error:/)).toHaveTextContent(/boom/i);
    });
  });

  it("toggling Live pauses the 2s polling", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(snapshot()));

    const { container } = render(<PipelineView />);
    await screen.findByRole("img", { name: /gateway process flow/i });
    const callsAfterMount = vi.mocked(fetch).mock.calls.length;

    // Advance well past one poll interval with Live ON → a refetch happens.
    await vi.advanceTimersByTimeAsync(2100);
    await waitFor(() => {
      expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(callsAfterMount);
    });

    // Toggle Live OFF → no further refetches.
    const checkbox = container.querySelector('input[type="checkbox"]') as HTMLInputElement;
    checkbox.click();
    const callsAfterToggle = vi.mocked(fetch).mock.calls.length;
    await vi.advanceTimersByTimeAsync(5000);
    expect(vi.mocked(fetch).mock.calls.length).toBe(callsAfterToggle);
  });
});
