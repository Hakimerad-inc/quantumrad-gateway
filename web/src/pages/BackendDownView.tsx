/** Connection-error page — the gateway could not be reached (B3 tray leg).
 *
 * `AuthContext.checkAuth` treats a *transport* failure (connection refused,
 * DNS, rejected CORS preflight) as `backendUnreachable` rather than "not
 * authenticated". Before this view existed, that state rendered the login
 * screen: an operator whose backend had crashed stared at a password prompt,
 * typed a valid password, and received a generic "Network error" that read
 * like a wrong password. No credential can fix a refused connection, so this
 * page shows the port that was probed and a Retry instead of a password form.
 */

import { useState } from "react";
import { useAuth } from "../context/AuthContext";
import { API_BASE } from "../api";
import { BrandMark } from "../ui/icons";

export default function BackendDownView() {
  const { checkAuth } = useAuth();
  const [retrying, setRetrying] = useState(false);

  // The base the SPA actually probes: the shell-injected port inside Tauri,
  // the bundle-time override, or same-origin (empty) for the browser case.
  const target = API_BASE || "the server that served this page";

  const handleRetry = async () => {
    setRetrying(true);
    try {
      await checkAuth();
    } finally {
      setRetrying(false);
    }
  };

  return (
    <div className="login-page">
      <div className="login-card">
        <div className="brand"><BrandMark size={48} /></div>
        <h1>Gateway unreachable</h1>
        <p className="brand-tagline">Diagnostic Clarity, Quantum Fast.</p>
        <p className="subtitle">
          The admin panel cannot reach the gateway backend at <code>{target}</code>.
        </p>

        <div className="error-message" role="alert">
          No connection could be established. This is a backend or network
          problem — not a sign-in failure, and no password will help. Check that
          the gateway service is running and that nothing else holds its port.
        </div>

        <button type="button" onClick={handleRetry} disabled={retrying}>
          {retrying ? "Retrying…" : "Retry connection"}
        </button>

        {/* The shell injects the port via __MERCURE_PORT__ from the Rust side's
            MERCURE_BACKEND_PORT. A mismatch here — shell and backend started
            with different ports — is the usual cause, so name it. */}
        <p className="subtitle">
          The panel probes <code>/api/system/status</code>. If the port above is
          wrong, the desktop shell and the gateway were started with different
          <code> MERCURE_BACKEND_PORT</code> values.
        </p>
      </div>
    </div>
  );
}
