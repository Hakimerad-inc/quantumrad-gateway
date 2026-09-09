/** Feature-boundary error gate (fault-tolerant-error-boundaries).
 *
 * Without this, a crash inside any view unmounts the whole React tree and the
 * operator is left with a blank console. One boundary wraps the page switch in
 * App.tsx (keyed by page name, so navigating away resets it): a crashed panel
 * degrades to a readable banner with a reset button while the sidebar, banner
 * row and the rest of the shell keep working. */

import { Component, type ErrorInfo, type ReactNode } from "react";

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
      return (
        <div className="error-banner" role="alert">
          This panel failed to render: {error.message}
          <div style={{ marginTop: 8 }}>
            <button className="btn" onClick={this.reset}>Reload panel</button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
