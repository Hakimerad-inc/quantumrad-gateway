import { useState } from "react";
import type { StudyPage } from "../api";
import { fetchStudies, retryStudy, enqueueStudy, requestReport } from "../api";
import { useAsync } from "../hooks/useAsync";
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
  const { data, loading, error, setError, reload } = useAsync<StudyPage>(
    (signal) => fetchStudies(page, PAGE_SIZE, undefined, undefined, { signal }),
    { deps: [page] },
  );

  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;

  const handleRetry = async (studyId: number) => {
    const ok = await retryStudy(studyId);
    if (!ok) {
      setError("Retry failed — the study has no incomplete routes, or the request was rejected.");
      return;
    }
    void reload();
  };

  const handleEnqueue = async (studyId: number) => {
    // A 409 here is a real condition the operator can act on (no destination
    // enabled, or the study is already terminal) — not a silent no-op.
    const ok = await enqueueStudy(studyId);
    if (!ok) {
      setError(
        "Enqueue failed — enable a destination first (Destinations tab), or the study is already sent.",
      );
      return;
    }
    void reload();
  };

  const handleRequestReport = async (studyId: number) => {
    await requestReport(studyId, "sr");
    void reload();
  };

  return (
    <div>
      <h2>Study Queue</h2>
      {error ? <div className="error-banner" role="alert">Error: {error}</div> : null}
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
                    {s.state === "RECEIVED" && s.num_destinations === 0 && (
                      <button className="btn" onClick={() => handleEnqueue(s.id)}>
                        Enqueue
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
