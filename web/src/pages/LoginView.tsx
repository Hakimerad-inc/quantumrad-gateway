/** Login page for web UI authentication (S06-T8). */

import { useState, FormEvent } from 'react';
import { useAuth } from '../context/AuthContext';

interface LoginViewProps {
  onLogin?: () => void;
}

export default function LoginView({ onLogin }: LoginViewProps) {
  const { login } = useAuth();
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setLoading(true);

    try {
      // Delegate to AuthContext so `isAuthenticated` flips and the app leaves
      // the login screen. Calling /api/login directly here sets the cookie but
      // leaves the auth state stale, which re-renders this view forever (C1).
      const result = await login(password);
      if (!result.ok) {
        setError(result.error ?? 'Login failed');
        return;
      }
      onLogin?.();
    } catch {
      setError('Network error. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="login-page">
      <div className="login-card">
        <h1>Mercure Gateway</h1>
        <p className="subtitle">Sign in to access the admin panel</p>

        <form onSubmit={handleSubmit}>
          {error && <div className="error-message" role="alert">{error}</div>}

          <div className="form-group">
            <label htmlFor="password">Password</label>
            <input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Enter password"
              disabled={loading}
              required
              autoFocus
            />
          </div>

          <button type="submit" disabled={loading || !password}>
            {loading ? 'Signing in…' : 'Sign In'}
          </button>
        </form>
      </div>
    </div>
  );
}
