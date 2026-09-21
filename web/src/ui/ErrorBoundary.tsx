/** Feature-boundary error gate (fault-tolerant-error-boundaries).
 *
 * Without this, a crash inside any view unmounts the whole React tree and the
 * operator is left with a blank console. One boundary wraps the page switch in
 * App.tsx (keyed by page name, so navigating away resets it): a crashed panel
 * degrades to a readable banner with a reset button while the sidebar, banner
 * row and the rest of the shell keep working.
 *
 * This boundary also catches the other failure mode a lazy page can hit: a
 * dynamic import that never resolves. React re-throws a rejected
 * `React.lazy` promise on the next render pass, so it lands here rather than
 * surfacing as an unhandled rejection and a permanently blank page. The two
 * kinds need different recovery, which is why they have separate branches:
 *
 *   render crash     — clearing the error state and re-rendering is a real
 *                      retry, so "Reload panel" can fix it in place.
 *   module load fail — React.lazy caches a rejected import and re-throws the
 *                      same error on every subsequent render, so in-place
 *                      retry loops on a blank pane. Only a full page reload
 *                      re-fetches the chunk, so that is what we offer. */

import { Component, type ErrorInfo, type ReactNode } from "react";

function isModuleLoadFailure(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  // Vite throws a TypeError carrying the module URL; webpack names the error
  // ChunkLoadError. Match both rather than the exact string, so a bundler
  // upgrade does not silently drop the recovery path.
  return (
    error.name === "ChunkLoadError" ||
    /(?:failed to fetch|error loading) dynamically imported module/i.test(error.message)
  );
}

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Console only: the audit/ops trail lives on the backend; here we just
    // keep the stack visible for support screenshots.
    console.error("UI panel crashed:", error, info.componentStack);
  }

  private reset = () => this.setState({ error: null });

  render() {
    const { error } = this.state;
    if (error) {
      // A chunk that failed to load cannot be retried in place (see the file
      // header): offer a reload of the whole console instead.
      const loadFailure = isModuleLoadFailure(error);
      return (
        <div className="error-banner" role="alert">
          {loadFailure
            ? <>This page failed to load: {error.message}</>
            : <>This panel failed to render: {error.message}</>}
          <div style={{ marginTop: 8 }}>
            {loadFailure ? (
              <button className="btn" onClick={() => window.location.reload()}>
                Reload the gateway console
              </button>
            ) : (
              <button className="btn" onClick={this.reset}>Reload panel</button>
            )}
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
