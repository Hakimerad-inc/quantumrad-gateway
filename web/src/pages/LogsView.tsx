import { useState, useEffect, useCallback } from "react";
import { fetchLogs } from "../api";

export default function LogsView() {
  const [lines, setLines] = useState<string[]>([]);
  const [total, setTotal] = useState(0);
  const [limit, setLimit] = useState(200);
  const [auto, setAuto] = useState(true);

  const load = useCallback(async () => {
    const r = await fetchLogs(limit);
    setLines(r.lines);
    setTotal(r.total_available);
  }, [limit]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (!auto) return;
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, [auto, load]);

  return (
    <div>
      <h2>Operations Log</h2>
      <div className="card" style={{ marginBottom: 12 }}>
        <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
          <label style={{ color: "var(--muted)", fontSize: 12 }}>
            Lines
            <select
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value))}
              style={{ marginLeft: 6, background: "var(--surface2)", color: "var(--text)", border: "1px solid var(--border)", borderRadius: 4, padding: "4px 8px" }}
            >
              {[50, 100, 200, 500].map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>
          <label style={{ color: "var(--muted)", fontSize: 12, display: "flex", alignItems: "center", gap: 6 }}>
            <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
            Auto-refresh (5s)
          </label>
          <button className="btn" onClick={load}>Refresh</button>
          <span style={{ color: "var(--muted)", fontSize: 12, marginLeft: "auto" }}>
            showing last {lines.length} of {total} lines
          </span>
        </div>
      </div>
      <div className="card" style={{ padding: 0, overflow: "hidden" }}>
        {lines.length === 0 ? (
          <div className="empty"><div className="icon">📜</div>No log lines</div>
        ) : (
          <pre style={{ maxHeight: 600, overflowY: "auto", padding: 12 }}>{lines.join("\n")}</pre>
        )}
      </div>
    </div>
  );
}