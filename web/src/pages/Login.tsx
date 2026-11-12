import { useState } from "react";
import type { FormEvent } from "react";
import { explain } from "../api/client";
import { useAuth } from "../auth/AuthContext";

export function Login({ notice }: { notice?: string }) {
  const { login } = useAuth();
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(name, password);
    } catch (err) {
      setError(explain(err));
    } finally {
      setBusy(false);
      setPassword("");
    }
  };

  return (
    <main className="flex min-h-screen items-center justify-center bg-slate-100 px-4">
      <div className="w-full max-w-sm rounded-lg bg-white p-8 shadow-sm ring-1 ring-slate-200">
        <h1 className="text-2xl font-semibold tracking-tight">Elide</h1>
        <p className="mt-1 text-sm text-slate-600">Sign in to your merchant workspace.</p>
        {notice && (
          <p role="status" className="mt-4 rounded bg-slate-100 px-3 py-2 text-sm text-slate-700">
            {notice}
          </p>
        )}
        <form onSubmit={submit} className="mt-6 space-y-4">
          <div>
            <label htmlFor="login" className="block text-sm font-medium">
              Email
            </label>
            <input
              id="login"
              type="email"
              autoComplete="username"
              className="input mt-1"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
            />
          </div>
          <div>
            <label htmlFor="password" className="block text-sm font-medium">
              Password
            </label>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              className="input mt-1"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>
          {error && (
            <p role="alert" className="text-sm text-rose-700">
              {error}
            </p>
          )}
          <button type="submit" className="btn-primary w-full" disabled={busy}>
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
        <p className="mt-6 text-xs text-slate-500">
          Development builds use synthetic merchants and labelled TEST identities only.
        </p>
      </div>
    </main>
  );
}
