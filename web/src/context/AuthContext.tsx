/** Auth context for web UI session management (S06-T8). */

import { createContext, useContext, useState, useEffect, ReactNode } from 'react';

export interface LoginResult {
  ok: boolean;
  error?: string;
}

interface AuthContextType {
  isAuthenticated: boolean;
  isLoading: boolean;
  login: (password: string) => Promise<LoginResult>;
  logout: () => Promise<void>;
  checkAuth: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isLoading, setIsLoading] = useState(true);

  const checkAuth = async () => {
    try {
      const res = await fetch('/api/system/status', { credentials: 'include' });
      setIsAuthenticated(res.ok);
    } catch {
      setIsAuthenticated(false);
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    checkAuth();
  }, []);

  // Single source of truth for authentication: callers (LoginView) must go
  // through this rather than posting to /api/login themselves, otherwise the
  // session cookie is set while `isAuthenticated` stays false and the app
  // re-renders the login screen forever (review C1).
  const login = async (password: string): Promise<LoginResult> => {
    try {
      const res = await fetch('/api/login', {
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
      return { ok: false, error: 'Network error. Please try again.' };
    }
  };

  const logout = async () => {
    try {
      await fetch('/api/logout', { method: 'POST', credentials: 'include' });
    } finally {
      setIsAuthenticated(false);
    }
  };

  return (
    <AuthContext.Provider value={{ isAuthenticated, isLoading, login, logout, checkAuth }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within AuthProvider');
  return ctx;
}
