import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ApiError, api, setCsrfToken } from "../api/client";
import type { SessionBody } from "../api/types";
import { ReauthDialog } from "./ReauthDialog";

type AuthState =
  | { status: "loading" }
  | { status: "signed-out"; notice?: string }
  | { status: "signed-in"; session: SessionBody };

type AuthApi = {
  state: AuthState;
  login: (login: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
  /** Runs a money- or terms-changing action; asks for the password again if the server
   * requires a recent step-up, then retries the same action (same operation key). */
  withStepUp: <T>(action: () => Promise<T>) => Promise<T>;
};

const Ctx = createContext<AuthApi | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading" });
  const [reauthOpen, setReauthOpen] = useState(false);
  const pending = useRef<((ok: boolean) => void) | null>(null);

  const accept = useCallback((s: SessionBody) => {
    setCsrfToken(s.csrf_token);
    setState({ status: "signed-in", session: s });
  }, []);

  const refresh = useCallback(async () => {
    try {
      accept(await api<SessionBody>("/v1/auth/session"));
    } catch {
      setCsrfToken(null);
      setState({ status: "signed-out" });
    }
  }, [accept]);

  useEffect(() => {
    let live = true;
    api<SessionBody>("/v1/auth/session")
      .then((s) => {
        if (live) accept(s);
      })
      .catch(() => {
        if (!live) return;
        setCsrfToken(null);
        setState({ status: "signed-out" });
      });
    return () => {
      live = false;
    };
  }, [accept]);

  const login = useCallback(
    async (login: string, password: string) => {
      accept(await api<SessionBody>("/v1/auth/login", { method: "POST", body: { login, password } }));
    },
    [accept],
  );

  const logout = useCallback(async () => {
    try {
      await api("/v1/auth/logout", { method: "POST" });
    } finally {
      setCsrfToken(null);
      setState({ status: "signed-out", notice: "You signed out." });
    }
  }, []);

  const withStepUp = useCallback(async <T,>(action: () => Promise<T>): Promise<T> => {
    try {
      return await action();
    } catch (err) {
      if (err instanceof ApiError && err.code === "UNAUTHENTICATED") {
        setCsrfToken(null);
        setState({ status: "signed-out", notice: "Your session ended. Sign in again." });
      }
      if (!(err instanceof ApiError && err.code === "REAUTH_REQUIRED")) throw err;
      const ok = await new Promise<boolean>((resolve) => {
        pending.current = resolve;
        setReauthOpen(true);
      });
      if (!ok) throw new ApiError(401, "REAUTH_CANCELLED", "Password confirmation cancelled.");
      return await action();
    }
  }, []);

  const finishReauth = (ok: boolean) => {
    setReauthOpen(false);
    pending.current?.(ok);
    pending.current = null;
  };

  return (
    <Ctx.Provider value={{ state, login, logout, refresh, withStepUp }}>
      {children}
      {reauthOpen && <ReauthDialog onDone={finishReauth} />}
    </Ctx.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export function useAuth(): AuthApi {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside AuthProvider");
  return v;
}
