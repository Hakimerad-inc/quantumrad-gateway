import { useState, useEffect } from "react";
import { AuthProvider, useAuth } from "./context/AuthContext";
import QueueView from "./pages/QueueView";
import ConfigView from "./pages/ConfigView";
import LogsView from "./pages/LogsView";
import ReportsView from "./pages/ReportsView";
import AuditView from "./pages/AuditView";
import SetupWizardPage from "./pages/SetupWizard";
import PipelineView from "./pages/PipelineView";
import LoginView from "./pages/LoginView";
import { fetchDiskStatus, fetchQueueStats, fetchSystemStatus, navigate, type DiskStatus } from "./api";
import {
  IconBrand,
  IconPulse,
  IconQueue,
  IconBolt,
  IconFileText,
  IconShield,
  IconSettings,
  IconTerminal,
  IconWorkflow,
  IconLogout,
} from "./ui/icons";

type Page = "dashboard" | "pipeline" | "queue" | "reports" | "audit" | "config" | "logs" | "setup" | "login";

export function Dashboard() {
  const [status, setStatus] = useState<{ receiver: string; forwarder: string; report_retriever: string; uptime_sec: number; version: string; hub_registered: boolean | null; hub_streaming: boolean | null } | null>(null);
  const [stats, setStats] = useState<{ queued: number; sending: number; sent: number; error: number; failed: number } | null>(null);
  const [disk, setDisk] = useState<DiskStatus | null>(null);

  useEffect(() => {
    fetchSystemStatus().then(setStatus).catch(() => setStatus(null));
    fetchQueueStats().then(setStats).catch(() => setStats(null));
    fetchDiskStatus().then(setDisk).catch(() => setDisk(null));
  }, []);

  const dot = (running: string) => (
    <span className={`status-dot ${running === "running" ? "green" : "gray"}`} />
  );

  const gaugeClass = disk ? (disk.usage_pct >= 98 ? "fill crit" : disk.over_threshold ? "fill warn" : "fill") : "fill";
  const capacity = disk ? `${disk.usage_pct.toFixed(1)}%` : "—";

  return (
    <div>
      <h2>Dashboard</h2>
      {disk?.over_threshold && (
        <div className="banner warn" role="alert">
          Spool disk usage at {disk.usage_pct.toFixed(1)}% — at or above the {disk.warning_pct}%
          warning threshold{disk.purge_on_disk_full ? " (auto-purge of oldest delivered studies is armed)" : ". Enable purge_on_disk_full or free space."}
        </div>
      )}
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
        <div className="card-header">Hub Reporting</div>
        <div className="stats">
          <div className="stat">
            <div className="label">Registration</div>
            <div className="value">
              {status && status.hub_registered !== null
                ? <>{dot(status.hub_registered ? "running" : "stopped")}{status.hub_registered ? "registered" : "not registered"}</>
                : "—"}
            </div>
          </div>
          <div className="stat">
            <div className="label">Event Streaming</div>
            <div className="value">
              {status && status.hub_streaming !== null
                ? <>{dot(status.hub_streaming ? "running" : "stopped")}{status.hub_streaming ? "active" : "inactive"}</>
                : "—"}
            </div>
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
      <div className="card">
        <div className="card-header">Storage</div>
        {disk ? (
          <>
            <div className="gauge" aria-hidden="true"><div className={gaugeClass} style={{ width: `${Math.min(100, disk.usage_pct)}%` }} /></div>
            <div className="gauge-row">
              <span className={`gauge-value ${disk.over_threshold ? "warn" : ""}`}>{capacity}</span>
              <span className="gauge-meta">warning at {disk.warning_pct}%</span>
            </div>
            <div className="stats">
              <div className="stat"><div className="label">Available</div><div className="value mono">{(disk.free_bytes / 1e9).toFixed(1)} GB</div></div>
              <div className="stat"><div className="label">Used</div><div className="value mono">{(disk.used_bytes / 1e9).toFixed(1)} GB</div></div>
              <div className="stat">
                <div className="label">Auto-purge delivered</div>
                <div className="value">
                  {disk.purge_on_disk_full
                    ? <span className="badge green">armed</span>
                    : <span className="badge gray">disabled</span>}
                </div>
              </div>
            </div>
          </>
        ) : (
          <div className="stat"><div className="label">Capacity</div><div className="value">—</div></div>
        )}
      </div>
    </div>
  );
}

// Protected route wrapper
function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading } = useAuth();

  if (isLoading) {
    return <div className="loading">Loading…</div>;
  }

  if (!isAuthenticated) {
    navigate('login');
    return null;
  }

  return <>{children}</>;
}

const NAV: Array<{ key: Page; icon: React.ReactNode; label: string }> = [
  { key: "dashboard", icon: <IconPulse />, label: "Dashboard" },
  { key: "pipeline", icon: <IconWorkflow />, label: "Pipeline" },
  { key: "queue", icon: <IconQueue />, label: "Queue" },
  { key: "setup", icon: <IconBolt />, label: "Setup" },
  { key: "reports", icon: <IconFileText />, label: "Reports" },
  { key: "audit", icon: <IconShield />, label: "Audit" },
  { key: "config", icon: <IconSettings />, label: "Config" },
  { key: "logs", icon: <IconTerminal />, label: "Logs" },
];

const PAGES = new Set(NAV.map((n) => n.key));

function pageFromHash(): Page {
  const h = window.location.hash.replace(/^#\/?/, "");
  return (PAGES.has(h as Page) ? h : "dashboard") as Page;
}

function AppContent() {
  const [page, setPage] = useState<Page>(pageFromHash);
  const [version, setVersion] = useState("");
  const { isAuthenticated, logout, isLoading: authLoading } = useAuth();

  useEffect(() => {
    fetchSystemStatus().then((s) => setVersion(s.version)).catch(() => {});
    const onHash = () => setPage(pageFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const handleNavigate = (key: Page) => {
    window.location.hash = `/${key}`;
    setPage(key);
  };

  const handleLogout = async () => {
    await logout();
    navigate('login');
  };

  // Show login page when not authenticated (and not loading)
  if (!isAuthenticated && !authLoading) {
    return <LoginView onLogin={() => setPage("dashboard")} />;
  }

  // Show loading state while checking auth
  if (authLoading) {
    return <div className="shell"><main className="main"><div className="loading">Loading…</div></main></div>;
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="sidebar-header">
          <span className="brand-mark"><IconBrand size={17} /></span>
          <div>
            <h1>Mercure Gateway</h1>
            <div className="version">{version ? `v${version}` : ""}</div>
          </div>
        </div>
        <nav>
          {NAV.map((item) => (
            <a
              key={item.key}
              href={`#/${item.key}`}
              className={page === item.key ? "active" : ""}
              aria-current={page === item.key ? "page" : undefined}
              onClick={(e) => { e.preventDefault(); handleNavigate(item.key); }}
            >
              <span className="icon">{item.icon}</span> {item.label}
            </a>
          ))}
        </nav>
        <div className="sidebar-footer">
          <button className="logout-btn" onClick={handleLogout} title="Sign out">
            <IconLogout size={16} />
            <span>Sign out</span>
          </button>
        </div>
      </aside>
      <main className="main">
        <ProtectedRoute>
          {page === "dashboard" && <Dashboard />}
          {page === "pipeline" && <PipelineView />}
          {page === "queue" && <QueueView />}
          {page === "setup" && <SetupWizardPage />}
          {page === "reports" && <ReportsView />}
          {page === "audit" && <AuditView />}
          {page === "config" && <ConfigView />}
          {page === "logs" && <LogsView />}
        </ProtectedRoute>
      </main>
    </div>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <AppContent />
    </AuthProvider>
  );
}
