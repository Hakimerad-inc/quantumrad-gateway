import { lazy, Suspense, useState, useEffect } from "react";
import { AuthProvider, useAuth } from "./context/AuthContext";
// Route-level code splitting (batch W4). What this is, and is not:
//
//   IS  — a bound on the initial parse/eval of the JS bundle. On a cold start
//          on a slow machine that shows up as first-paint time, and it is the
//          whole point for the headless HTTP deployment (05-final-report.md
//          §"Do not invest in route-level code splitting for the Tauri origin
//          — there is no network to wait on. It only matters for the headless
//          HTTP deployment."). An operator who only opens the Dashboard no
//          longer parses SetupWizard, ConfigView and friends.
//   IS NOT — a network win in the Tauri build: the frontend is served from
//          the same process on localhost, so a split chunk costs a
//          same-process fetch and gains nothing over a warm bundle cache.
//
// Dashboard (below) is deliberately NOT lazy: it is the default page and
// renders on first paint, so putting it behind a chunk boundary would make
// first paint worse and defeat the point. LoginView and BackendDownView stay
// eager for the same reason — they are the pre-auth and connection-failure
// entry points, and the page you land on must not itself be subject to a
// chunk load failure.
import LoginView from "./pages/LoginView";
import BackendDownView from "./pages/BackendDownView";
const QueueView = lazy(() => import("./pages/QueueView"));
const DestinationsView = lazy(() => import("./pages/DestinationsView"));
const ConfigView = lazy(() => import("./pages/ConfigView"));
const LogsView = lazy(() => import("./pages/LogsView"));
const ReportsView = lazy(() => import("./pages/ReportsView"));
const AuditView = lazy(() => import("./pages/AuditView"));
const SetupWizardPage = lazy(() => import("./pages/SetupWizard"));
const PipelineView = lazy(() => import("./pages/PipelineView"));
import { fetchDiskStatus, fetchQueueStats, fetchSystemStatus, isRestartRequired, onRestartRequired, type DiskStatus, type SystemStatus } from "./api";
import UpdaterBanner from "./ui/UpdaterBanner";
import ErrorBoundary from "./ui/ErrorBoundary";
import { BrandMark, IconPulse, IconQueue, IconBolt, IconFileText, IconShield, IconSettings, IconTerminal, IconWorkflow, IconLogout, IconSend } from "./ui/icons";

type Page =
  | "dashboard"
  | "pipeline"
  | "queue"
  | "destinations"
  | "reports"
  | "audit"
  | "config"
  | "logs"
  | "setup";

export function Dashboard() {
  const [status, setStatus] = useState<SystemStatus | null>(null);
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

const NAV: Array<{ key: Page; icon: React.ReactNode; label: string }> = [
  { key: "dashboard", icon: <IconPulse />, label: "Dashboard" },
  { key: "pipeline", icon: <IconWorkflow />, label: "Pipeline" },
  { key: "queue", icon: <IconQueue />, label: "Queue" },
  { key: "destinations", icon: <IconSend />, label: "Destinations" },
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

function PageFallback() {
  return <div className="loading">Loading page…</div>;
}

function AppContent() {
  const [page, setPage] = useState<Page>(pageFromHash);
  const [version, setVersion] = useState("");
  // Persistent "restart required" banner: the gateway must be restarted for a
  // saved config change to take effect (review H5).
  //
  // Two sources, OR'd: the server's config_pending_restart is the *truth* (it
  // compares the config the running components were built with against the
  // saved one) and survives a page reload; the module signal gives instant
  // feedback the moment a save lands, before the next status fetch. Relying on
  // the module flag alone meant a reload silently cleared the banner and let
  // an operator believe a saved change was live (C4).
  const [serverPending, setServerPending] = useState(false);
  const [clientPending, setClientPending] = useState(isRestartRequired());
  const restartNeeded = serverPending || clientPending;
  const { isAuthenticated, logout, isLoading: authLoading, backendUnreachable } = useAuth();

  useEffect(() => {
    fetchSystemStatus()
      .then((s) => {
        setVersion(s.version);
        setServerPending(s.config_pending_restart);
      })
      .catch(() => {});
  }, []);

  // Re-read the hash on browser navigation (back/forward, manual edit).
  useEffect(function syncPageWithHash() {
    const onHash = () => setPage(pageFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  // Subscribe to the module-level "restart required" signal (review H5).
  useEffect(function subscribeToRestartRequired() {
    return onRestartRequired(setClientPending);
  }, []);

  const handleNavigate = (key: Page) => {
    window.location.hash = `/${key}`;
    setPage(key);
  };

  // Clearing the session flips `isAuthenticated` to false, which re-renders
  // AppContent into the LoginView branch below. No explicit navigation needed.
  const handleLogout = async () => {
    await logout();
  };

  // A refused connection is not an auth failure: the login screen would ask
  // for a password no credential can make useful. Checked before the
  // not-authenticated branch so a dead backend never reads as "sign in".
  if (backendUnreachable && !authLoading) {
    return <BackendDownView />;
  }

  // Show login page when not authenticated (and not loading)
  if (!isAuthenticated && !authLoading) {
    return <LoginView onLogin={() => setPage("dashboard")} />;
  }

  // Show loading state while checking auth
  if (authLoading) {
    return <div className="shell"><main className="main" id="main"><div className="loading">Loading…</div></main></div>;
  }

  return (
    <div className="shell">
      {/* Skip link: the sidebar is 9 nav links before the content — keyboard
         users need a way past it (2.4.1). Off-screen until focused. */}
      <a className="skip-link" href="#main">Skip to main content</a>
      <aside className="sidebar">
        <div className="sidebar-header">
          <span className="brand-mark"><BrandMark size={22} /></span>
          <div>
            <h1>Quantum<span className="brand-accent">RAD</span> Gateway</h1>
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
      <main className="main" id="main">
        {restartNeeded && (
          <div className="banner warn" role="status">
            Configuration saved but not yet active — the gateway must restart for changes to take
            effect. Quit and relaunch the gateway (or restart its service); stopping and starting
            components from the panel does <strong>not</strong> reload configuration.
          </div>
        )}
        <UpdaterBanner />
        {/* Feature boundary (fault-tolerant-error-boundaries): a crash in one
            panel degrades to a banner inside the panel area — the shell (nav,
            banners) stays alive. Keyed by page so switching resets the gate. */}
        {/* One Suspense boundary for the lazy pages above. It sits inside the
            ErrorBoundary, which is what makes a chunk that fails to load
            recoverable rather than a permanently blank pane — see
            ErrorBoundary's module-load branch. The fallback is deliberately a
            plain loading line, not a full-page mask: the shell (nav, banners)
            stays visible and usable while the chunk resolves. */}
        <ErrorBoundary key={page}>
          <Suspense fallback={<PageFallback />}>
            {page === "dashboard" && <Dashboard />}
            {page === "pipeline" && <PipelineView />}
            {page === "queue" && <QueueView />}
            {page === "destinations" && <DestinationsView />}
            {page === "setup" && <SetupWizardPage />}
            {page === "reports" && <ReportsView />}
            {page === "audit" && <AuditView />}
            {page === "config" && <ConfigView />}
            {page === "logs" && <LogsView />}
          </Suspense>
        </ErrorBoundary>
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
