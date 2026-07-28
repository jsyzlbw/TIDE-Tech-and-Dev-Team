import {
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import {
  ACCESS_TOKEN_STORAGE_KEY,
  api,
  createApiClient,
  MAX_ACCESS_TOKEN_CHARACTERS,
  readStoredAccessToken,
} from "../shared/api/client";
import { ApiAbortError, ApiContractError, ApiError } from "../shared/api/errors";
import {
  currentUserSchema,
  tokenResponseSchema,
} from "../shared/api/schemas";
import {
  AUTH_SESSION_QUERY_KEY,
  AuthContext,
  AuthStorageError,
  type AuthContextValue,
} from "./authState";

function removeStoredToken(): void {
  try {
    window.localStorage.removeItem(ACCESS_TOKEN_STORAGE_KEY);
  } catch {
    // Memory state remains authoritative when browser storage is unavailable.
  }
}

function persistToken(token: string): void {
  try {
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, token);
    if (readStoredAccessToken() !== token) throw new AuthStorageError();
  } catch {
    removeStoredTokenIfCurrent(token);
    throw new AuthStorageError();
  }
}

function isFatalSessionError(error: unknown): boolean {
  return (
    error instanceof ApiContractError ||
    (error instanceof ApiError && (error.status === 401 || error.status === 403))
  );
}

function isPlausibleToken(value: string | null): value is string {
  if (value === null || value === "" || value.length > MAX_ACCESS_TOKEN_CHARACTERS) return false;
  return ![...value].some((character) => {
    const codePoint = character.codePointAt(0) ?? 0;
    return codePoint <= 31 || (codePoint >= 127 && codePoint <= 159);
  });
}

function removeStoredTokenIfCurrentInvalid(expectedValue: string): void {
  const currentValue = readStoredAccessToken();
  if (currentValue !== expectedValue || isPlausibleToken(currentValue)) return;
  removeStoredToken();
}

function removeStoredTokenIfCurrent(expectedValue: string): void {
  if (readStoredAccessToken() !== expectedValue) return;
  removeStoredToken();
}

function readUsableStoredToken(): string | null {
  const token = readStoredAccessToken();
  if (isPlausibleToken(token)) return token;
  if (token !== null) removeStoredTokenIfCurrentInvalid(token);
  return null;
}

function sessionQueryKey(token: string | null) {
  return [...AUTH_SESSION_QUERY_KEY, token] as const;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const generation = useRef(0);
  const [activeToken, setActiveToken] = useState<string | null>(() => readUsableStoredToken());
  const activeTokenRef = useRef(activeToken);
  const session = useQuery({
    queryKey: sessionQueryKey(activeToken),
    enabled: activeToken !== null,
    retry: false,
    queryFn: ({ queryKey, signal }) => {
      const requestToken = queryKey[2];
      if (typeof requestToken !== "string") throw new ApiAbortError();
      return createApiClient({ getToken: () => requestToken }).get(
        "/auth/me",
        currentUserSchema,
        { signal },
      );
    },
  });
  const fatalSessionError = session.error !== null && isFatalSessionError(session.error);

  const switchMemorySession = useCallback((token: string | null) => {
    const previousToken = activeTokenRef.current;
    if (previousToken !== null) {
      void queryClient.cancelQueries({
        queryKey: sessionQueryKey(previousToken),
        exact: true,
      });
    }
    if (token === null) {
      queryClient.removeQueries({ queryKey: AUTH_SESSION_QUERY_KEY });
    } else if (token !== previousToken) {
      queryClient.removeQueries({ queryKey: sessionQueryKey(token), exact: true });
    }
    activeTokenRef.current = token;
    setActiveToken(token);
    if (token !== null) {
      queueMicrotask(() => {
        void queryClient.refetchQueries({
          queryKey: sessionQueryKey(token),
          exact: true,
          type: "active",
        });
      });
    }
  }, [queryClient]);

  const logout = useCallback(() => {
    generation.current += 1;
    removeStoredToken();
    switchMemorySession(null);
  }, [switchMemorySession]);

  useEffect(() => {
    if (!fatalSessionError || activeToken === null) return;
    const failedToken = activeToken;
    let active = true;
    queueMicrotask(() => {
      if (!active) return;
      generation.current += 1;
      const storedToken = readUsableStoredToken();
      if (storedToken === failedToken) {
        removeStoredTokenIfCurrent(failedToken);
        switchMemorySession(null);
        return;
      }
      switchMemorySession(storedToken);
    });
    return () => {
      active = false;
    };
  }, [activeToken, fatalSessionError, switchMemorySession]);

  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key !== ACCESS_TOKEN_STORAGE_KEY) return;
      generation.current += 1;
      switchMemorySession(readUsableStoredToken());
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, [switchMemorySession]);

  const login = useCallback(
    async (username: string, password: string) => {
      const operation = generation.current + 1;
      generation.current = operation;
      let receivedToken: string | null = null;
      try {
        const token = await api.post(
          "/auth/login",
          { username, password },
          tokenResponseSchema,
        );
        if (generation.current !== operation) throw new ApiAbortError();
        receivedToken = token.access_token;
        persistToken(receivedToken);
        const verifiedUser = await createApiClient({ getToken: () => receivedToken }).get(
          "/auth/me",
          currentUserSchema,
        );
        if (
          generation.current !== operation ||
          readUsableStoredToken() !== receivedToken
        ) {
          throw new ApiAbortError();
        }
        void queryClient.cancelQueries({ queryKey: AUTH_SESSION_QUERY_KEY });
        queryClient.removeQueries({ queryKey: AUTH_SESSION_QUERY_KEY });
        queryClient.setQueryData(sessionQueryKey(receivedToken), verifiedUser);
        activeTokenRef.current = receivedToken;
        setActiveToken(receivedToken);
        return verifiedUser;
      } catch (error) {
        let storedToken = readUsableStoredToken();
        if (receivedToken !== null && storedToken === receivedToken) {
          removeStoredTokenIfCurrent(receivedToken);
          storedToken = readUsableStoredToken();
        }
        if (generation.current === operation) {
          switchMemorySession(storedToken);
        }
        throw error;
      }
    },
    [queryClient, switchMemorySession],
  );

  const retrySession = useCallback(async () => {
    await session.refetch();
  }, [session]);

  const value = useMemo<AuthContextValue>(
    () => ({
      user: activeToken === null ? null : (session.data ?? null),
      loading:
        activeToken !== null &&
        session.data === undefined &&
        (session.isPending || session.isFetching),
      sessionError:
        activeToken !== null && session.error !== null && !fatalSessionError
          ? session.error
          : null,
      login,
      logout,
      retrySession,
    }),
    [activeToken, fatalSessionError, login, logout, retrySession, session.data, session.error, session.isFetching, session.isPending],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
