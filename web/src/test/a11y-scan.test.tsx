import { describe, it, expect, vi, beforeAll } from "vitest";
import axe from "axe-core";
import { render, screen, waitFor } from "@testing-library/react";
import type { ComponentType, ReactElement } from "react";
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

// One row per scanned view. Everything outside this table — the axe options,
// the jsdom shims, the stub map mechanism, and the "violations must be empty"
// assertion — is invariant and lives in the harness below, so adding an
// eleventh view is a table row and nothing else.
type A11yCase = {
  name: string;
  load: () => Promise<ComponentType>;
  stubs?: Record<string, unknown>;
  // Assertion run inside waitFor, i.e. the text/element that proves the view
  // has rendered its data before axe walks the tree. Omit only for a view
  // that renders synchronously and fetches nothing.
  anchor?: () => unknown;
  // Tree wrapper for views that need a context provider. Do NOT add a
  // provider to a row that does not need one: an extra wrapper changes what
  // axe sees and can hide a real landmark violation.
  wrap?: (node: ReactElement) => ReactElement;
};

const CASES: readonly A11yCase[] = [
  {
    name: "Dashboard",
    load: () => import("../App").then((m) => m.Dashboard),
    stubs: {
      "system/status": STATUS,
      "queue/stats": { queued: 1, sending: 2, sent: 3, error: 0, failed: 1 },
      "system/disk": { usage_pct: 55.5, warning_pct: 80, free_bytes: 1e11, used_bytes: 1e11, over_threshold: false, purge_on_disk_full: true },
    },
    anchor: () => expect(screen.getByText("Queue Overview")).toBeInTheDocument(),
  },
  {
    // LoginView consumes useAuth, so it needs the provider in the tree.
    name: "LoginView",
    load: () => import("../pages/LoginView").then((m) => m.default),
    anchor: () => expect(screen.getByLabelText(/password/i)).toBeInTheDocument(),
    wrap: (node) => <AuthProvider>{node}</AuthProvider>,
  },
  {
    name: "QueueView",
    load: () => import("../pages/QueueView").then((m) => m.default),
    stubs: { "studies": { items: [{ id: 1, study_uid: "1.2.3.4.5.6.7.8.9.10.11.12.13", accession: "A1", modality: "CT", state: "FAILED", num_destinations: 0, created_at: "2026-09-18T00:00:00Z" }], total: 1 } },
    anchor: () => expect(screen.getByText("A1")).toBeInTheDocument(),
  },
  {
    name: "AuditView",
    load: () => import("../pages/AuditView").then((m) => m.default),
    stubs: { "audit": [{ id: 1, ts: "2026-09-18T00:00:00Z", event: "STUDY_SENT", detail: "to pacs1", user: "admin", hash: "abcdef0123456789" }] },
    anchor: () => expect(screen.getByText("STUDY_SENT")).toBeInTheDocument(),
  },
  {
    name: "ReportsView",
    load: () => import("../pages/ReportsView").then((m) => m.default),
    stubs: { "reports": [{ id: 1, report_type: "pdf", status: "retrieved", study_uid: "1.2.3.4.5.6.7.8.9.10", retrieved_at: "2026-09-18T00:00:00Z" }] },
    anchor: () => expect(screen.getByText("pdf")).toBeInTheDocument(),
  },
  {
    name: "ConfigView",
    load: () => import("../pages/ConfigView").then((m) => m.default),
    stubs: { "config": { general: { ae_title: "GATEWAY" } }, "config/warnings": { warnings: [] } },
    anchor: () => expect(screen.getByRole("textbox")).toBeInTheDocument(),
  },
  {
    name: "DestinationsView",
    load: () => import("../pages/DestinationsView").then((m) => m.default),
    stubs: {
      "config": { destinations: [{ name: "pacs1", type: "dicom", enabled: true, host: "10.0.0.1", port: 104, aet_target: "PACS", aet_source: "GATEWAY" }] },
      "config/warnings": { warnings: [] },
    },
    anchor: () => expect(screen.getByDisplayValue("pacs1")).toBeInTheDocument(),
  },
  {
    // Renders synchronously and makes no fetch calls, so there is no anchor
    // to wait for — axe walks the first paint as-is.
    name: "SetupWizard",
    load: () => import("../pages/SetupWizard").then((m) => m.default),
  },
  {
    name: "PipelineView",
    load: () => import("../pages/PipelineView").then((m) => m.default),
    stubs: {
      "pipeline": {
        generated_at: "2026-09-18T00:00:00Z",
        components: { receiver: true, forwarder: true },
        receiver_counts: { received_last_hour: 5 },
        queue: { queued: 1, sending: 0, error: 0, failed: 0 },
        destinations: [{ name: "pacs1", host: "10.0.0.1", port: 104, aet: "PACS", health: { status: "ok", latency_ms: 12, checked_at: "2026-09-18T00:00:00Z" }, routes: { complete: 3, sending: 0, waiting: 0, error: 0 } }],
      },
    },
    anchor: () => expect(screen.getByText("Forwarder")).toBeInTheDocument(),
  },
  {
    name: "LogsView",
    load: () => import("../pages/LogsView").then((m) => m.default),
    stubs: { "logs": { lines: ["2026-09-18 INFO gateway started"], total_available: 1 } },
    anchor: () => expect(screen.getByText(/gateway started/)).toBeInTheDocument(),
  },
];

describe("accessibility scan", () => {
  it.each(CASES)("$name", async ({ load, stubs, anchor, wrap }) => {
    if (stubs) stubApi(stubs);
    const View = await load();
    render(wrap ? wrap(<View />) : <View />);
    if (anchor) await waitFor(anchor);
    // The whole point of the file: zero axe violations against the tag set
    // above. This stays `toEqual([])` — do not soften it to a threshold or
    // skip on "known" failures; either is the guard silently disarmed. If a
    // violation appears, fix the view (contrast/reflow excepted — see the
    // header for why those are out of scope here).
    expect(await runAxe(document.body)).toEqual([]);
  });
});
