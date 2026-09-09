/** Opt-in update banner (ADR-0006): Tauri-only, never auto-installs.
 *
 * Rendered in App.tsx. Outside the Tauri shell (browser / backend-served SPA)
 * it renders nothing — the browser update story is "download the new
 * installer". Inside Tauri it checks the update endpoint once on mount; when
 * an update is available the operator must explicitly click to download and
 * install, and the relaunch (plugin-process) only happens after install. */

import { useEffect, useState } from "react";

type Update = { version: string; downloadAndInstall: () => Promise<void> };

function inTauri(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ !==
      "undefined"
  );
}

async function checkForUpdate(): Promise<Update | null> {
  // Dynamic imports: the plugin modules only exist inside the Tauri runtime;
  // in a plain browser the import alone would 404.
  const { check } = await import("@tauri-apps/plugin-updater");
  return check();
}

export default function UpdaterBanner() {
  const [update, setUpdate] = useState<Update | null>(null);
  const [busy, setBusy] = useState(false);
  const [dismissed, setDismissed] = useState(false);

  useEffect(function checkUpdateOnceOnMount() {
    if (!inTauri()) return;
    checkForUpdate()
      .then(setUpdate)
      .catch(() => undefined); // update check must never break the UI
  }, []);

  if (!inTauri() || !update || dismissed) return null;

  const install = async () => {
    setBusy(true);
    try {
      await update.downloadAndInstall();
      const { relaunch } = await import("@tauri-apps/plugin-process");
      await relaunch();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="banner" role="status">
      Version {update.version} is available.{" "}
      <button className="btn primary" onClick={install} disabled={busy}>
        {busy ? "Updating..." : "Restart to update"}
      </button>
      <button className="btn" onClick={() => setDismissed(true)}>
        Dismiss
      </button>
    </div>
  );
}
