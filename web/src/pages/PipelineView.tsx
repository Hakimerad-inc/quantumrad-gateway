import { useState, useCallback, useRef } from "react";
import type {
  PipelineSnapshot,
  DestinationNode,
  DestinationRouteRow,
  StudyDetail,
  TimelineEvent,
} from "../api";
import {
  fetchPipeline,
  fetchDestinationStudies,
  fetchStudyDetail,
  fetchStudyTimeline,
  retryStudy,
  isAbortError,
} from "../api";
import { PipeNode, PipeEdge, Defs, destinationDot, destinationLines, HealthBadge } from "../ui/flow";
import { IconRefresh, IconChevronLeft } from "../ui/icons";
import { usePoll } from "../ui/usePoll";

const POLL_MS = 2000;

const EVENT_BADGE: Record<string, string> = {
  STUDY_RECEIVED: "blue", STUDY_QUEUED: "yellow", FORWARD_START: "yellow",
  FORWARD_COMPLETE: "green", STUDY_SENT: "green", FORWARD_ERROR: "red",
  STUDY_FAILED: "red", RETRY_MANUAL: "blue", REPORT_RETRIEVED: "green",
};

type Selection =
  | { kind: "destination"; name: string }
  | { kind: "study"; id: number };

export default function PipelineView() {
  const [snap, setSnap] = useState<PipelineSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [auto, setAuto] = useState(true);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [destStudies, setDestStudies] = useState<DestinationRouteRow[] | null>(null);
  const [studyDetail, setStudyDetail] = useState<StudyDetail | null>(null);
  const [timeline, setTimeline] = useState<TimelineEvent[] | null>(null);
  const prevReceived = useRef<number | null>(null);
  const [inboundFlow, setInboundFlow] = useState(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const s = await fetchPipeline(signal ? { signal } : {});
      setSnap(s);
      setError(null);
      if (prevReceived.current !== null && s.receiver_counts.received_last_hour > prevReceived.current) {
        setInboundFlow(true);
        setTimeout(() => setInboundFlow(false), 4000);
      }
      prevReceived.current = s.receiver_counts.received_last_hour;
    } catch (e) {
      // A superseded or unmounted fetch is not an error — the poller aborts
      // it deliberately (P1-20).
      if (isAbortError(e)) return;
      setError(String(e));
    }
  }, []);

  // Self-rescheduling poll: cannot stack, aborts a superseded in-flight
  // fetch, and stops while the tab is hidden (P1-20).
  const refresh = usePoll(load, { intervalMs: POLL_MS, enabled: auto });

  usePoll(
    useCallback(
      async (signal?: AbortSignal) => {
        if (selection === null) return;
        const init = signal ? { signal } : {};
        try {
          if (selection.kind === "destination") {
            setDestStudies(await fetchDestinationStudies(selection.name, init));
          } else {
            const detail = await fetchStudyDetail(selection.id, init);
            const tl = await fetchStudyTimeline(selection.id, init);
            // Both or nothing: a selection change mid-fetch aborts the pair,
            // so one panel half can't outlive the selection it belongs to.
            if (signal?.aborted) return;
            setStudyDetail(detail);
            setTimeline(tl);
          }
        } catch (e) {
          if (isAbortError(e)) return;
          /* selection panel refresh is best-effort; core snapshot shows errors */
        }
      },
      [selection],
    ),
    { intervalMs: POLL_MS * 2, enabled: selection !== null, deps: [selection] },
  );

  const clearSelection = () => {
    setSelection(null);
    setDestStudies(null);
    setStudyDetail(null);
    setTimeline(null);
  };

  const openStudy = (id: number) => setSelection({ kind: "study", id });

  const handleRetry = async (id: number) => {
    await retryStudy(id);
    await load();
  };

  const dest = (name: string): DestinationNode | undefined =>
    snap?.destinations.find((d) => d.name === name);

  // Layout constants (viewBox space)
  const W = 980;
  const destY = 330;
  const destXs = (snap?.destinations ?? []).map((_, i) => 90 + i * 200);

  const nodeFor = (d: DestinationNode, i: number) => (
    <PipeNode
      key={d.name}
      title={d.name}
      lines={destinationLines(d)}
      x={destXs[i]}
      y={destY}
      dot={destinationDot(d)}
      selected={selection?.kind === "destination" && selection.name === d.name}
      onClick={() => setSelection({ kind: "destination", name: d.name })}
    />
  );

  return (
    <div>
      <h2>Pipeline</h2>
      {error ? <div className="error-banner" role="alert">Error: {error}</div> : null}
      <div className="toolbar">
        <label className="check">
          <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
          Live (2s)
        </label>
        <button className="btn" onClick={refresh}><IconRefresh size={13} /> Refresh</button>
        <span className="hint">
          {snap ? `updated ${snap.generated_at.slice(11, 19)}Z` : "loading…"}
        </span>
      </div>

      <div className="card" style={{ padding: 0 }}>
        {/* role="group", not role="img": the destination nodes inside are
           interactive (role="button"), and children of role="img" are treated
           as presentational — they would be hidden from AT (1.3.1 / 4.1.2). */}
        <svg viewBox={`0 0 ${W} 420`} className="pipe-canvas" role="group"
          aria-label="Gateway process flow diagram">
          <Defs />
          {/* edges */}
          <PipeEdge from={[142, 92]} to={[258, 92]} flow={inboundFlow} />
          <PipeEdge from={[402, 92]} to={[518, 92]} />
          {snap?.destinations.map((d, i) => {
            const x = destXs[i] + 74;
            return (
              <PipeEdge
                key={d.name}
                from={[592, 92]}
                to={[x, destY]}
                flow={d.routes.sending > 0 || d.routes.waiting > 0}
                warn={d.routes.error > 0 || (d.health != null && d.health.status !== "ok")}
              />
            );
          })}

          {/* nodes */}
          <PipeNode
            title="Modality" x={-6} y={60}
            lines={[`${snap?.receiver_counts.received_last_hour ?? "—"} studies/hr`]}
            dot="gray"
          />
          <PipeNode
            title="Receiver" x={260} y={60}
            lines={[snap ? (snap.components.receiver ? "listening" : "stopped") : "—"]}
            dot={snap?.components.receiver ? "green" : "gray"}
          />
          <PipeNode
            title="Spool" x={404} y={60}
            lines={[
              `${snap?.queue.queued ?? "—"} queued`,
              `${snap?.queue.sending ?? "—"} sending`,
            ]}
            dot={snap && snap.queue.queued + snap.queue.sending > 0 ? "yellow" : "gray"}
          />
          <PipeNode
            title="Forwarder" x={518} y={60}
            lines={[
              snap ? (snap.components.forwarder ? "running" : "stopped") : "—",
              `err ${snap?.queue.error ?? "—"} · fail ${snap?.queue.failed ?? "—"}`,
            ]}
            dot={
              snap && (snap.queue.error > 0 || snap.queue.failed > 0) ? "red"
                : snap?.components.forwarder ? "green" : "gray"
            }
          />
          {snap?.destinations.map(nodeFor)}
        </svg>
      </div>

      {/* Detail panel */}
      {selection?.kind === "destination" && dest(selection.name) && (
        <div className="card">
          <div className="card-header" style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <button className="btn icon-only" aria-label="Back to pipeline" onClick={clearSelection}>
              <IconChevronLeft size={14} />
            </button>
            Destination: {selection.name}
            <HealthBadge health={dest(selection.name)!.health} />
            {dest(selection.name)!.host && (
              <span className="hint">
                {dest(selection.name)!.host}:{dest(selection.name)!.port} · AET {dest(selection.name)!.aet}
              </span>
            )}
          </div>
          {destStudies === null ? (
            <div className="loading">Loading routes</div>
          ) : destStudies.length === 0 ? (
            <div className="empty">No studies routed here yet</div>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th>Accession</th><th>Patient</th><th>Modality</th><th>Status</th><th>Attempts</th><th>Updated</th></tr>
                </thead>
                <tbody>
                  {destStudies.map((r) => (
                    <tr
                      key={r.route_id}
                      style={{ cursor: "pointer" }}
                      onClick={() => openStudy(r.study_id)}
                      // Keyboard parity with the click (2.1.1).
                      tabIndex={0}
                      role="button"
                      aria-label={`Study ${r.accession || r.study_uid}, ${r.modality || "unknown modality"}, ${r.status}`}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          openStudy(r.study_id);
                        }
                      }}
                    >
                      <td className="mono">{r.accession || "—"}</td>
                      <td>{r.patient_name || "—"}</td>
                      <td>{r.modality || "—"}</td>
                      <td><span className={`badge ${routeBadge(r.status)}`}>{r.status}</span></td>
                      <td className="mono">{r.attempts}</td>
                      <td className="mono">{r.updated_at}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {selection?.kind === "study" && studyDetail && (
        <div className="card">
          <div className="card-header" style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <button className="btn icon-only" aria-label="Back" onClick={clearSelection}>
              <IconChevronLeft size={14} />
            </button>
            Study {studyDetail.accession || studyDetail.study_uid.slice(0, 16) + "…"}
            <span className={`badge ${stateBadge(studyDetail.state)}`}>{studyDetail.state}</span>
            {(studyDetail.state === "FAILED" || studyDetail.state === "ERROR") && (
              <button className="btn" onClick={() => handleRetry(studyDetail.id)}>Retry</button>
            )}
          </div>
          <div className="split">
            <div>
              <div className="card-header">Forwarding routes</div>
              <table>
                <thead>
                  <tr><th>Destination</th><th>Status</th><th>Attempts</th><th>Next retry</th></tr>
                </thead>
                <tbody>
                  {studyDetail.routes.map((r) => (
                    <tr key={r.id}>
                      <td>{r.target_name}</td>
                      <td><span className={`badge ${routeBadge(r.status)}`}>{r.status}</span></td>
                      <td className="mono">{r.attempts}/5</td>
                      <td className="mono">
                        {r.next_retry_sec != null ? `in ~${Math.round(r.next_retry_sec)}s` : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {studyDetail.routes.some((r) => r.last_error) && (
                <pre className="error-banner" style={{ marginTop: 10 }}>
                  {studyDetail.routes.filter((r) => r.last_error).map((r) => r.last_error).join("\n")}
                </pre>
              )}
            </div>
            <div>
              <div className="card-header">Audit timeline</div>
              {timeline === null ? (
                <div className="loading">Loading timeline</div>
              ) : timeline.length === 0 ? (
                <div className="empty">No audit events</div>
              ) : (
                <div className="timeline">
                  {timeline.map((e) => (
                    <div key={e.id} className="timeline-event">
                      <span className="timeline-ts mono">{e.ts.slice(11, 19)}</span>
                      <span className={`badge ${EVENT_BADGE[e.event] || "gray"}`}>{e.event}</span>
                      <span className="timeline-detail mono">
                        {e.detail && "target_name" in e.detail ? String(e.detail.target_name) : ""}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {!snap && !error && <div className="loading">Loading pipeline</div>}
    </div>
  );
}

function routeBadge(status: string): string {
  if (status === "complete") return "green";
  if (status === "error") return "red";
  if (status === "sending" || status === "waiting") return "yellow";
  return "gray";
}

function stateBadge(state: string): string {
  if (state === "SENT" || state === "SENDING") return "green";
  if (state === "ERROR" || state === "FAILED") return "red";
  return "yellow";
}
