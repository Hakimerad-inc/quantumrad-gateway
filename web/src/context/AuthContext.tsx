/** Auth context for web UI session management (S06-T8). */

import { createContext, useContext, useState, useEffect, ReactNode } from 'react';
import { apiUrl } from '../api';

export interface LoginResult {
  ok: boolean;
  error?: string;
  /** True when the request never reached the gateway — connection refused,
   *  DNS failure, CORS preflight rejected. Distinct from "wrong password":
   *  no credential can fix it, so the UI must not offer a password field. */
  unreachable?: boolean;
}

interface AuthContextType {
  isAuthenticated: boolean;
  isLoading: boolean;
  /** True when the status probe could not reach the gateway at all. Render a
   *  connection error, not the login screen — otherwise a dead backend asks
   *  the operator for a password they cannot possibly need (B3 tray leg,
   *  2026-09-17: the shell pointed at a port with no listener). */
  backendUnreachable: boolean;
  login: (password: string) => Promise<LoginResult>;
  logout: () => Promise<void>;
  checkAuth: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [backendUnreachable, setBackendUnreachable] = useState(false);
  const [isLoading, setIsLoading] = useState(true);

  const checkAuth = async () => {
    try {
      const res = await fetch(apiUrl('/api/system/status'), { credentials: 'include' });
      // fetch rejects only on a transport failure, never on an HTTP status,
      // so reaching here means the gateway answered. A 401 is a real auth
      // failure; a connection refusal would have thrown instead.
      setBackendUnreachable(false);
      setIsAuthenticated(res.ok);
    } catch {
      setIsAuthenticated(false);
      setBackendUnreachable(true);
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(function checkSessionOnMount() {
    checkAuth();
  }, []);

  // Single source of truth for authentication: callers (LoginView) must go
  // through this rather than posting to /api/login themselves, otherwise the
  // session cookie is set while `isAuthenticated` stays false and the app
  // re-renders the login screen forever (review C1).
  const login = async (password: string): Promise<LoginResult> => {
    try {
      const res = await fetch(apiUrl('/api/login'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ password }),
      });
      if (res.ok) {
        setIsAuthenticated(true);
        return { ok: true };
      }
      const data = await res.json().catch(() => null);
      const detail =
        (data && typeof data.detail === 'string' && data.detail) ||
        (res.status === 401 ? 'invalid credentials' : 'Login failed');
      return { ok: false, error: detail };
    } catch {
      // Same transport distinction as checkAuth: the gateway never saw the
      // attempt. Report it as unreachable, not as an invalid password, and
      // flip the connection flag so App renders BackendDownView instead of
      // looping the operator on a password form that cannot succeed.
      setBackendUnreachable(true);
      return { ok: false, unreachable: true, error: 'Cannot reach the gateway.' };
    }
  };

  const logout = async () => {
    try {
      await fetch(apiUrl('/api/logout'), { method: 'POST', credentials: 'include' });
    } finally {
      setIsAuthenticated(false);
    }
  };

  return (
    <AuthContext.Provider value={{ isAuthenticated, isLoading, backendUnreachable, login, logout, checkAuth }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within AuthProvider');
  return ctx;
}
