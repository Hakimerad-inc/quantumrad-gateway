import { useState } from "react";
import type { AuditEvent, AuditVerifyResult } from "../api";
import { fetchAudit, verifyAudit, exportAuditLog } from "../api";
import { useAsync } from "../hooks/useAsync";
import { IconList, IconCheck, IconX } from "../ui/icons";

export default function AuditView() {
  // No loading flag here: the panel renders an empty list until the fetch
  // lands, exactly as before.
  const { data, error, setError } = useAsync<AuditEvent[]>((signal) => fetchAudit(200, { signal }));
  const events = data ?? [];
  const [verify, setVerify] = useState<AuditVerifyResult | null>(null);
  const [checking, setChecking] = useState(false);
  const [exporting, setExporting] = useState(false);
  // Separate from the list's load error: the banner text names the audit log,
  // so an export failure reported there would read as a load failure.
  const [exportError, setExportError] = useState("");

  const doVerify = async () => {
    setChecking(true);
    try {
      setVerify(await verifyAudit());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setChecking(false);
    }
  };

  const doExport = async () => {
    setExporting(true);
    setExportError("");
    try {
      await exportAuditLog();
    } catch (e) {
      setExportError(e instanceof Error ? e.message : String(e));
    } finally {
      setExporting(false);
    }
  };

  const eventBadge = (event: string) => {
    if (event.includes("ERROR") || event.includes("FAIL")) return "red";
    if (event.includes("SENT") || event.includes("COMPLETE")) return "green";
    if (event.includes("REPORT")) return "blue";
    return "gray";
  };

  return (
    <div>
      <h2>Audit Log</h2>
      {error ? <div className="error-banner" role="alert">Failed to load audit log: {error}</div> : null}
      {exportError ? (
        <div className="error-banner" role="alert">Audit export failed: {exportError}</div>
      ) : null}
      <div className="toolbar">
        <button className="btn primary" onClick={doVerify} disabled={checking}>
          {checking ? "Verifying..." : "Verify Chain Integrity"}
        </button>
        <button className="btn" onClick={doExport} disabled={exporting}>
          {exporting ? "Exporting..." : "Export audit log"}
        </button>
        {verify && (
          verify.valid
            ? <span className="ok-note" role="status" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}><IconCheck size={15} /> Chain intact</span>
            : <span className="error-banner" role="alert" style={{ display: "inline-flex", alignItems: "center", gap: 6, margin: 0 }}><IconX size={15} /> {verify.errors.length} broken link(s)</span>
        )}
        <span className="hint" style={{ marginLeft: "auto" }}>
          {events.length} events (last 200)
        </span>
      </div>
      <div className="card" style={{ padding: 0, overflow: "hidden" }}>
        {events.length === 0 ? (
          <div className="empty"><span className="icon"><IconList size={28} /></span><br />No audit events</div>
        ) : (
          <table>
            <thead>
              <tr><th>ID</th><th>Timestamp</th><th>Event</th><th>Detail</th><th>User</th><th>Hash (8)</th></tr>
            </thead>
            <tbody>
              {events.map((e) => (
                <tr key={e.id}>
                  <td>{e.id}</td>
                  <td className="mono">{e.ts}</td>
                  <td><span className={`badge ${eventBadge(e.event)}`}>{e.event}</span></td>
                  <td className="mono" style={{ maxWidth: 300, overflow: "hidden", textOverflow: "ellipsis" }}>{e.detail}</td>
                  <td>{e.user || "—"}</td>
                  <td className="mono">{e.hash ? `${e.hash.slice(0, 8)}…` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
