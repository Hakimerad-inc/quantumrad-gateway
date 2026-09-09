/** Windows service status + actions (PRD §13 Q3, S07-T9).
 *
 * Rendered inside ConfigView. Hidden entirely when the backend reports the
 * service surface is unavailable (non-Windows composition root) so Linux/macOS
 * operators never see a dead card. Install/uninstall ask for confirmation —
 * they change how the gateway starts on this machine. */

import { useCallback, useEffect, useState } from "react";
import { fetchServiceStatus, postServiceAction, type ServiceAction, type ServiceStatus } from "../api";

const ACTION_LABELS: Record<ServiceAction, string> = {
  install: "Install service",
  uninstall: "Uninstall service",
  start: "Start",
  stop: "Stop",
};

export default function ServiceCard() {
  const [status, setStatus] = useState<ServiceStatus | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(() => {
    fetchServiceStatus()
      .then(setStatus)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  if (error) {
    return (
      <div className="card">
        <div className="card-header">Windows Service</div>
        <div className="error-banner" role="alert">Failed to load service status: {error}</div>
      </div>
    );
  }
  if (!status || !status.available) return null;

  const run = async (action: ServiceAction) => {
    if (
      (action === "install" &&
        !window.confirm("Install the QuantumRAD Gateway Windows service (auto-start on boot)?")) ||
      (action === "uninstall" &&
        !window.confirm("Uninstall the QuantumRAD Gateway Windows service?"))
    ) {
      return;
    }
    setBusy(true);
    try {
      await postServiceAction(action);
      refresh();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card">
      <div className="card-header">Windows Service</div>
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <span className={`badge ${status.state === "running" ? "green" : "gray"}`}>
          {status.installed ? status.state : "not installed"}
        </span>
        {(Object.keys(ACTION_LABELS) as ServiceAction[]).map((action) => (
          <button
            key={action}
            className={`btn ${action === "uninstall" ? "danger" : ""}`}
            disabled={busy || (action === "start" && status.state === "running") || (action === "stop" && status.state !== "running")}
            onClick={() => run(action)}
          >
            {ACTION_LABELS[action]}
          </button>
        ))}
      </div>
    </div>
  );
}
