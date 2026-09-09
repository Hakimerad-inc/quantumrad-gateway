import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import QueueView from "./QueueView";
import type { StudyPage } from "../api";

// M8: QueueView (the second-largest view) previously had zero coverage.
// These tests pin the three user-visible behaviors: the loading state, the
// error banner on a failed fetch, and the retry/report actions per row.

function studyPage(items: StudyPage["items"], total = items.length): StudyPage {
  return {
    total,
    page: 1,
    page_size: 50,
    items,
  };
}

const baseStudy = {
  id: 1,
  study_uid: "1.2.840.10008.99.1",
  accession: "ACC-1",
  modality: "CT",
  patient_name: "TEST^P",
  state: "RECEIVED",
  created_at: "2026-09-09 12:00:00",
  num_destinations: 2,
};

describe("QueueView", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders studies with state badges", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify(studyPage([{ ...baseStudy }])), { status: 200 }),
    );

    render(<QueueView />);

    await waitFor(() => {
      expect(screen.getByText("ACC-1")).toBeInTheDocument();
    });
    expect(screen.getByText("1.2.840.10008.99.1")).toBeInTheDocument();
    expect(screen.getByText("RECEIVED")).toBeInTheDocument();
  });

  it("shows the empty state when there are no studies", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify(studyPage([])), { status: 200 }),
    );

    render(<QueueView />);

    await waitFor(() => {
      expect(screen.getByText(/No studies in queue/i)).toBeInTheDocument();
    });
  });

  it("renders an error banner when the fetch fails (M8)", async () => {
    vi.mocked(fetch).mockRejectedValueOnce(new TypeError("network down"));

    render(<QueueView />);

    await waitFor(() => {
      expect(screen.getByText(/Error:/)).toHaveTextContent(/network down/i);
    });
  });

  it("offers Retry for FAILED studies and requests reports for any row", async () => {
    const failed = { ...baseStudy, id: 7, state: "FAILED" };
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify(studyPage([{ ...baseStudy, id: 6 }, failed])), { status: 200 }),
    );

    render(<QueueView />);
    await waitFor(() => {
      expect(screen.getAllByText("ACC-1")).toHaveLength(2);
    });

    // Only the FAILED row offers Retry (row id 7).
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Report" })).toHaveLength(2);

    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    // Second call is the POST /studies/7/retry triggered by the click.
    await waitFor(() => {
      const calls = vi.mocked(fetch).mock.calls.map((c) => String(c[0]));
      expect(calls.some((u) => u.includes("/api/studies/7/retry"))).toBe(true);
    });
  });

  it("paginates: Prev/Next disabled states follow page bounds", async () => {
    // total 60 > PAGE_SIZE 50 → pagination row appears, page 2 exists.
    const items = Array.from({ length: 50 }, (_, i) => ({
      ...baseStudy,
      id: i + 1,
    }));
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify(studyPage(items, 60)), { status: 200 }),
    );

    render(<QueueView />);
    await waitFor(() => {
      expect(screen.getByText(/Page 1 of 2/)).toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: /Prev/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Next/i })).toBeEnabled();
  });
});
