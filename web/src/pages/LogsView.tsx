import { useState, useEffect, useCallback } from "react";
import { fetchLogs } from "../api";

export default function LogsView() {
  const [lines, setLines] = useState<string[]>([]);
  const [total, setTotal] = useState(0);
  const [limit, setLimit] = useState(200);
  const [auto, setAuto] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const r = await fetchLogs(limit);
      setLines(r.lines);
      setTotal(r.total_available);
      setError("");
    } catch (e) {
      // Keep the last good lines on screen; surface why refresh failed (M8).
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [limit]);

  useEffect(function loadLogsOnLimitChange() {
    load();
  }, [load]);

  useEffect(function pollLogsWhileAuto() {
    if (!auto) return;
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, [auto, load]);

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
          <button className="btn" onClick={load}>Refresh</button>
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
