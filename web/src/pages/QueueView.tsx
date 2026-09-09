import { useState, useEffect, useCallback } from "react";
import type { StudySummary } from "../api";
import { fetchStudies, retryStudy, requestReport } from "../api";
import { IconInbox, IconChevronLeft, IconChevronRight } from "../ui/icons";

const PAGE_SIZE = 50;

const STATE_BADGE: Record<string, string> = {
  SENT: "green",
  RECEIVED: "blue",
  QUEUED: "yellow",
  SENDING: "yellow",
  ERROR: "red",
  FAILED: "red",
  RECEIVING: "blue",
};

function Badge({ state }: { state: string }) {
  return <span className={`badge ${STATE_BADGE[state] || "gray"}`}>{state}</span>;
}

function TruncateUid({ uid }: { uid: string }) {
  return <span title={uid}>{uid.length > 20 ? `${uid.slice(0, 20)}…` : uid}</span>;
}

export default function QueueView() {
  const [page, setPage] = useState(1);
  const [data, setData] = useState<{ items: StudySummary[]; total: number } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await fetchStudies(page, PAGE_SIZE);
      setData(result);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [page]);

  useEffect(function loadOnMountAndPageChange() {
    load();
  }, [load]);

  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;

  const handleRetry = async (studyId: number) => {
    await retryStudy(studyId);
    load();
  };

  const handleRequestReport = async (studyId: number) => {
    await requestReport(studyId, "sr");
    load();
  };

  return (
    <div>
      <h2>Study Queue</h2>
      {error ? <div className="error-banner">Error: {error}</div> : null}
      <div className="card" style={{ padding: 0, overflow: "hidden" }}>
        <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Study UID</th>
              <th>Accession</th>
              <th>Modality</th>
              <th>State</th>
              <th>Dest.</th>
              <th>Created</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={8} className="loading">Loading studies</td></tr>
            ) : !data || data.items.length === 0 ? (
              <tr><td colSpan={8} className="empty"><span className="icon"><IconInbox size={28} /></span><br />No studies in queue</td></tr>
            ) : (
              data.items.map((s) => (
                <tr key={s.id}>
                  <td>{s.id}</td>
                  <td className="mono"><TruncateUid uid={s.study_uid} /></td>
                  <td>{s.accession || "—"}</td>
                  <td>{s.modality || "—"}</td>
                  <td><Badge state={s.state} /></td>
                  <td>{s.num_destinations}</td>
                  <td className="mono" style={{ fontSize: 11 }}>{s.created_at}</td>
                  <td>
                    {s.state === "FAILED" && (
                      <button className="btn" onClick={() => handleRetry(s.id)}>
                        Retry
                      </button>
                    )}
                    <button className="btn" onClick={() => handleRequestReport(s.id)}>
                      Report
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
        </div>
      </div>
      {data && data.total > PAGE_SIZE && (
        <div className="pagination">
          <button className="btn" disabled={page <= 1} onClick={() => setPage(page - 1)}>
            <IconChevronLeft size={14} /> Prev
          </button>
          <span className="page-info">
            Page {page} of {totalPages} ({data.total} total)
          </span>
          <button className="btn" disabled={page >= totalPages} onClick={() => setPage(page + 1)}>
            Next <IconChevronRight size={14} />
          </button>
        </div>
      )}
    </div>
  );
}