import { useState } from "react";
import type { ReportRow, ReportContent } from "../api";
import { fetchReports, fetchReportContent, refreshReport } from "../api";
import { useAsync } from "../hooks/useAsync";
import { IconFileStack, IconRefresh } from "../ui/icons";

const STATUS_BADGE: Record<string, string> = {
  retrieved: "green",
  pending: "yellow",
  retrieving: "yellow",
  failed: "red",
};

export default function ReportsView() {
  // No loading flag: the panel renders an empty list until the fetch lands.
  const { data, error, setError, reload } = useAsync<ReportRow[]>((signal) =>
    fetchReports(200, { signal }),
  );
  const reports = data ?? [];
  const [selected, setSelected] = useState<ReportContent | null>(null);

  const view = async (reportId: number) => {
    try {
      setSelected(await fetchReportContent(reportId));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const doRefresh = async (reportId: number) => {
    try {
      await refreshReport(reportId);
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div>
      <h2>Reports</h2>
      {error ? <div className="error-banner" role="alert">Report action failed: {error}</div> : null}
      <div className="split">
        <div className="card" style={{ padding: 0, overflow: "hidden" }}>
          {reports.length === 0 ? (
            <div className="empty"><span className="icon"><IconFileStack size={28} /></span><br />No reports</div>
          ) : (
            <table>
              <thead>
                <tr><th>ID</th><th>Type</th><th>Status</th><th>Study UID</th><th>Retrieved</th><th></th></tr>
              </thead>
              <tbody>
                {reports.map((r) => (
                  <tr
                    key={r.id}
                    style={{ cursor: "pointer" }}
                    onClick={() => view(r.id)}
                    // A row that acts as a button must be reachable and
                    // activatable from the keyboard too (2.1.1).
                    tabIndex={0}
                    role="button"
                    aria-label={`Report ${r.id}, ${r.report_type}, ${r.status}`}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        view(r.id);
                      }
                    }}
                  >
                    <td>{r.id}</td>
                    <td><span className={`badge ${r.report_type === "pdf" ? "blue" : "green"}`}>{r.report_type}</span></td>
                    <td><span className={`badge ${STATUS_BADGE[r.status] || "gray"}`}>{r.status}</span></td>
                    <td className="mono" title={r.study_uid}>
                      {r.study_uid.length > 20 ? `${r.study_uid.slice(0, 20)}…` : r.study_uid}
                    </td>
                    <td className="mono">{r.retrieved_at || "—"}</td>
                    <td>
                      {r.status !== "retrieved" && (
                        <button className="btn" onClick={(e) => { e.stopPropagation(); doRefresh(r.id); }}>
                          <IconRefresh size={13} /> Refresh
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {selected && (
          <div className="card">
            <div className="card-header">
              Report #{selected.report_id} — {selected.report_type} ({selected.status})
            </div>
            {selected.content ? (
              selected.mime === "application/pdf" ? (
                <iframe
                  title={`report-${selected.report_id}`}
                  src={`data:application/pdf;base64,${selected.content}`}
                  style={{ width: "100%", height: 500, border: "1px solid var(--border)", borderRadius: 6 }}
                />
              ) : (
                <pre style={{ whiteSpace: "pre-wrap" }}>{selected.content}</pre>
              )
            ) : (
              <div className="empty">No content {selected.status !== "retrieved" ? "(not retrieved yet)" : ""}</div>
            )}
            <button className="btn" style={{ marginTop: 8 }} onClick={() => setSelected(null)}>
              Close
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
