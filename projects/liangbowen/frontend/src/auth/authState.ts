import { createContext, useContext } from "react";

import type { CurrentUser } from "../shared/api/schemas";

export const AUTH_SESSION_QUERY_KEY = ["auth", "session"] as const;

export class AuthStorageError extends Error {
  constructor() {
    super("Authentication storage is unavailable");
    this.name = "AuthStorageError";
  }
}

export interface AuthContextValue {
  user: CurrentUser | null;
  loading: boolean;
  sessionError: unknown | null;
  login(username: string, password: string): Promise<CurrentUser>;
  logout(): void;
  retrySession(): Promise<void>;
}

export const AuthContext = createContext<AuthContextValue | null>(null);

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (context === null) throw new Error("useAuth must be used within AuthProvider");
  return context;
}
