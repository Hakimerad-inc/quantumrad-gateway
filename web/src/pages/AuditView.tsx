import { useState, useEffect, useCallback } from "react";
import type { AuditEvent, AuditVerifyResult } from "../api";
import { fetchAudit, verifyAudit } from "../api";

export default function AuditView() {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [verify, setVerify] = useState<AuditVerifyResult | null>(null);
  const [checking, setChecking] = useState(false);

  const load = useCallback(async () => {
    setEvents(await fetchAudit(200));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const doVerify = async () => {
    setChecking(true);
    try {
      setVerify(await verifyAudit());
    } finally {
      setChecking(false);
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
      <div className="card" style={{ marginBottom: 12 }}>
        <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
          <button className="btn primary" onClick={doVerify} disabled={checking}>
            {checking ? "Verifying..." : "Verify Chain Integrity"}
          </button>
          {verify && (
            verify.valid
              ? <span className="badge green">✓ Chain intact</span>
              : <span className="badge red">✗ {verify.errors.length} broken link(s)</span>
          )}
          <span style={{ color: "var(--muted)", fontSize: 12, marginLeft: "auto" }}>
            {events.length} events (last 200)
          </span>
        </div>
      </div>
      <div className="card" style={{ padding: 0, overflow: "hidden" }}>
        {events.length === 0 ? (
          <div className="empty"><div className="icon">☰</div>No audit events</div>
        ) : (
          <table>
            <thead>
              <tr><th>ID</th><th>Timestamp</th><th>Event</th><th>Detail</th><th>User</th><th>Hash (8)</th></tr>
            </thead>
            <tbody>
              {events.map((e) => (
                <tr key={e.id}>
                  <td>{e.id}</td>
                  <td className="mono" style={{ fontSize: 11 }}>{e.ts}</td>
                  <td><span className={`badge ${eventBadge(e.event)}`}>{e.event}</span></td>
                  <td className="mono" style={{ fontSize: 11, maxWidth: 300, overflow: "hidden", textOverflow: "ellipsis" }}>{e.detail}</td>
                  <td>{e.user || "—"}</td>
                  <td className="mono" style={{ fontSize: 10 }}>{e.hash ? `${e.hash.slice(0, 8)}…` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}