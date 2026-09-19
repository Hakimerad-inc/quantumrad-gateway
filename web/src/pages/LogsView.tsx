import { useState, useCallback } from "react";
import { fetchLogs, isAbortError } from "../api";
import { usePoll } from "../ui/usePoll";

export default function LogsView() {
  const [lines, setLines] = useState<string[]>([]);
  const [total, setTotal] = useState(0);
  const [limit, setLimit] = useState(200);
  const [auto, setAuto] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const r = await fetchLogs(limit, signal ? { signal } : {});
        setLines(r.lines);
        setTotal(r.total_available);
        setError("");
      } catch (e) {
        // A superseded or unmounted fetch is not a failure to report — the
        // poller aborts it deliberately (P1-20).
        if (isAbortError(e)) return;
        // Keep the last good lines on screen; surface why refresh failed (M8).
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [limit],
  );

  // Self-rescheduling poll: cannot stack, aborts a superseded in-flight
  // fetch, and stops while the tab is hidden (P1-20). A `limit` change
  // refetches immediately.
  const refresh = usePoll(load, { intervalMs: 5000, enabled: auto, deps: [limit] });

  return (
    <div>
      <h2>Operations Log</h2>
      {error ? <div className="error-banner" role="alert">Log refresh failed: {error}</div> : null}
      <div className="toolbar">
        <label className="check">
          Lines
          <select
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
          >
            {[50, 100, 200, 500].map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
          <label className="check">
            <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
            Auto-refresh (5s)
          </label>
          <button className="btn" onClick={refresh}>Refresh</button>
          <span className="hint" style={{ marginLeft: "auto" }}>
            showing last {lines.length} of {total} lines
          </span>
      </div>
      <div className="card" style={{ padding: 0, overflow: "hidden" }}>
        {lines.length === 0 ? (
          <div className="empty">No log lines</div>
        ) : (
          <pre style={{ maxHeight: 600, overflowY: "auto", padding: 12 }}>{lines.join("\n")}</pre>
        )}
      </div>
    </div>
  );
}
