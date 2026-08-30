import { useState, useEffect } from "react";
import QueueView from "./pages/QueueView";
import ConfigView from "./pages/ConfigView";
import LogsView from "./pages/LogsView";
import ReportsView from "./pages/ReportsView";
import AuditView from "./pages/AuditView";
import SetupWizardPage from "./pages/SetupWizard";
import { fetchQueueStats, fetchSystemStatus } from "./api";

type Page = "dashboard" | "queue" | "reports" | "audit" | "config" | "logs" | "setup";

function Dashboard() {
  const [status, setStatus] = useState<{ receiver: string; forwarder: string; report_retriever: string; uptime_sec: number; version: string } | null>(null);
  const [stats, setStats] = useState<{ queued: number; sending: number; sent: number; error: number; failed: number } | null>(null);

  useEffect(() => {
    fetchSystemStatus().then(setStatus).catch(() => setStatus(null));
    fetchQueueStats().then(setStats).catch(() => setStats(null));
  }, []);

  const dot = (running: string) => (
    <span className={`status-dot ${running === "running" ? "green" : "gray"}`} />
  );

  return (
    <div>
      <h2>Dashboard</h2>
      <div className="card">
        <div className="card-header">System Status</div>
        <div className="stats">
          <div className="stat">
            <div className="label">Receiver</div>
            <div className="value">{status ? <>{dot(status.receiver)}{status.receiver}</> : "—"}</div>
          </div>
          <div className="stat">
            <div className="label">Forwarder</div>
            <div className="value">{status ? <>{dot(status.forwarder)}{status.forwarder}</> : "—"}</div>
          </div>
          <div className="stat">
            <div className="label">Report Retriever</div>
            <div className="value">{status ? <>{dot(status.report_retriever)}{status.report_retriever}</> : "—"}</div>
          </div>
        </div>
      </div>
      <div className="card">
        <div className="card-header">Queue Overview</div>
        <div className="stats">
          <div className="stat"><div className="label">Queued</div><div className="value accent">{stats?.queued ?? "—"}</div></div>
          <div className="stat"><div className="label">Sending</div><div className="value yellow">{stats?.sending ?? "—"}</div></div>
          <div className="stat"><div className="label">Sent</div><div className="value green">{stats?.sent ?? "—"}</div></div>
          <div className="stat"><div className="label">Error</div><div className="value yellow">{stats?.error ?? "—"}</div></div>
          <div className="stat"><div className="label">Failed</div><div className="value red">{stats?.failed ?? "—"}</div></div>
        </div>
      </div>
    </div>
  );
}

const NAV: Array<{ key: Page; icon: string; label: string }> = [
  { key: "dashboard", icon: "◉", label: "Dashboard" },
  { key: "queue", icon: "↻", label: "Queue" },
  { key: "setup", icon: "⚡", label: "Setup" },
  { key: "reports", icon: "📄", label: "Reports" },
  { key: "audit", icon: "☰", label: "Audit" },
  { key: "config", icon: "⚙", label: "Config" },
  { key: "logs", icon: "📜", label: "Logs" },
];

export default function App() {
  const [page, setPage] = useState<Page>("dashboard");
  const [version, setVersion] = useState("");

  useEffect(() => {
    fetchSystemStatus().then((s) => setVersion(s.version)).catch(() => {});
  }, []);

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="sidebar-header">
          <h1>Mercure Gateway</h1>
          <div className="version">{version ? `v${version}` : ""}</div>
        </div>
        <nav>
          {NAV.map((item) => (
            <a
              key={item.key}
              href="#"
              className={page === item.key ? "active" : ""}
              onClick={(e) => { e.preventDefault(); setPage(item.key); }}
            >
              <span className="icon">{item.icon}</span> {item.label}
            </a>
          ))}
        </nav>
        <div className="sidebar-footer">mercure-gateway</div>
      </aside>
      <main className="main">
        {page === "dashboard" && <Dashboard />}
        {page === "queue" && <QueueView />}
        {page === "setup" && <SetupWizardPage />}
        {page === "reports" && <ReportsView />}
        {page === "audit" && <AuditView />}
        {page === "config" && <ConfigView />}
        {page === "logs" && <LogsView />}
      </main>
    </div>
  );
}
