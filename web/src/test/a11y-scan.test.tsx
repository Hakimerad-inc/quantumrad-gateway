import { describe, it, expect, vi, beforeAll } from "vitest";
import axe from "axe-core";
import { render, screen, waitFor } from "@testing-library/react";
import { AuthProvider } from "../context/AuthContext";

// Regression guard: render each view with a realistic data shape and run axe
// (wcag2a/2aa/21a/21aa/22aa). API calls are stubbed so tables and forms render.
//
// NOTE: this catches structural regressions (missing labels, roles, landmarks,
// duplicate ids). It cannot catch colour-contrast or reflow failures — jsdom
// does no layout or compositing — so those are covered by the token comments in
// index.css and were verified by hand against the WCAG luminance formula.

const STATUS = {
  version: "1.1.0-rc3", receiver: true, forwarder: true, report_retriever: true,
  hub_registered: true, hub_streaming: false, config_pending_restart: false,
};

beforeAll(() => {
  // jsdom lacks matchMedia (prefers-color-scheme / reduced-motion CSS queries).
  if (!window.matchMedia) {
    window.matchMedia = vi.fn().mockReturnValue({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    });
  }
  // axe measures SVG paths; jsdom has no layout engine so hand it a length.
  const proto = window.SVGElement.prototype as SVGElement & { getTotalLength?: () => number };
  if (!proto.getTotalLength) proto.getTotalLength = () => 100;
});

function stubApi(map: Record<string, unknown>) {
  (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mockImplementation(async (url: string) => {
    const path = String(url).replace(/^https?:\/\/[^/]+/, "");
    for (const [key, val] of Object.entries(map)) {
      if (path.includes(key)) return new Response(JSON.stringify(val), { status: 200 });
    }
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  });
}

const AXE_OPTS = { runOnly: { type: "tag" as const, values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"] } };

async function runAxe(container: HTMLElement) {
  const results = await axe.run(container, AXE_OPTS);
  return results.violations.map((v) => ({
    id: v.id,
    impact: v.impact,
    help: v.help,
    tags: v.tags.filter((t) => t.startsWith("wcag")),
    nodes: v.nodes.length,
    sample: v.nodes[0]?.html.slice(0, 220),
  }));
}

describe("accessibility scan", () => {
  it("Dashboard", async () => {
    stubApi({
      "system/status": STATUS,
      "queue/stats": { queued: 1, sending: 2, sent: 3, error: 0, failed: 1 },
      "system/disk": { usage_pct: 55.5, warning_pct: 80, free_bytes: 1e11, used_bytes: 1e11, over_threshold: false, purge_on_disk_full: true },
    });
    const { Dashboard } = await import("../App");
    render(<Dashboard />);
    await waitFor(() => expect(screen.getByText("Queue Overview")).toBeInTheDocument());
    expect(await runAxe(document.body)).toEqual([]);
  });

  it("LoginView", async () => {
    // LoginView consumes useAuth, so it needs the provider in the tree.
    const LoginView = (await import("../pages/LoginView")).default;
    render(
      <AuthProvider>
        <LoginView />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByLabelText(/password/i)).toBeInTheDocument());
    expect(await runAxe(document.body)).toEqual([]);
  });

  it("QueueView", async () => {
    stubApi({ "studies": { items: [{ id: 1, study_uid: "1.2.3.4.5.6.7.8.9.10.11.12.13", accession: "A1", modality: "CT", state: "FAILED", num_destinations: 0, created_at: "2026-09-18T00:00:00Z" }], total: 1 } });
    const QueueView = (await import("../pages/QueueView")).default;
    render(<QueueView />);
    await waitFor(() => expect(screen.getByText("A1")).toBeInTheDocument());
    expect(await runAxe(document.body)).toEqual([]);
  });

  it("AuditView", async () => {
    stubApi({ "audit": [{ id: 1, ts: "2026-09-18T00:00:00Z", event: "STUDY_SENT", detail: "to pacs1", user: "admin", hash: "abcdef0123456789" }] });
    const AuditView = (await import("../pages/AuditView")).default;
    render(<AuditView />);
    await waitFor(() => expect(screen.getByText("STUDY_SENT")).toBeInTheDocument());
    expect(await runAxe(document.body)).toEqual([]);
  });

  it("ReportsView", async () => {
    stubApi({ "reports": [{ id: 1, report_type: "pdf", status: "retrieved", study_uid: "1.2.3.4.5.6.7.8.9.10", retrieved_at: "2026-09-18T00:00:00Z" }] });
    const ReportsView = (await import("../pages/ReportsView")).default;
    render(<ReportsView />);
    await waitFor(() => expect(screen.getByText("pdf")).toBeInTheDocument());
    expect(await runAxe(document.body)).toEqual([]);
  });

  it("ConfigView", async () => {
    stubApi({ "config": { general: { ae_title: "GATEWAY" } }, "config/warnings": { warnings: [] } });
    const ConfigView = (await import("../pages/ConfigView")).default;
    render(<ConfigView />);
    await waitFor(() => expect(screen.getByRole("textbox")).toBeInTheDocument());
    expect(await runAxe(document.body)).toEqual([]);
  });

  it("DestinationsView", async () => {
    stubApi({
      "config": { destinations: [{ name: "pacs1", type: "dicom", enabled: true, host: "10.0.0.1", port: 104, aet_target: "PACS", aet_source: "GATEWAY" }] },
      "config/warnings": { warnings: [] },
    });
    const DestinationsView = (await import("../pages/DestinationsView")).default;
    render(<DestinationsView />);
    await waitFor(() => expect(screen.getByDisplayValue("pacs1")).toBeInTheDocument());
    expect(await runAxe(document.body)).toEqual([]);
  });

  it("SetupWizard", async () => {
    const SetupWizard = (await import("../pages/SetupWizard")).default;
    render(<SetupWizard />);
    expect(await runAxe(document.body)).toEqual([]);
  });

  it("PipelineView", async () => {
    stubApi({
      "pipeline": {
        generated_at: "2026-09-18T00:00:00Z",
        components: { receiver: true, forwarder: true },
        receiver_counts: { received_last_hour: 5 },
        queue: { queued: 1, sending: 0, error: 0, failed: 0 },
        destinations: [{ name: "pacs1", host: "10.0.0.1", port: 104, aet: "PACS", health: { status: "ok", latency_ms: 12, checked_at: "2026-09-18T00:00:00Z" }, routes: { complete: 3, sending: 0, waiting: 0, error: 0 } }],
      },
    });
    const PipelineView = (await import("../pages/PipelineView")).default;
    render(<PipelineView />);
    await waitFor(() => expect(screen.getByText("Forwarder")).toBeInTheDocument());
    expect(await runAxe(document.body)).toEqual([]);
  });

  it("LogsView", async () => {
    stubApi({ "logs": { lines: ["2026-09-18 INFO gateway started"], total_available: 1 } });
    const LogsView = (await import("../pages/LogsView")).default;
    render(<LogsView />);
    await waitFor(() => expect(screen.getByText(/gateway started/)).toBeInTheDocument());
    expect(await runAxe(document.body)).toEqual([]);
  });
});
