import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api, explain } from "../api/client";

/** Step-up: confirm the password before a money- or terms-changing decision. */
export function ReauthDialog({ onDone }: { onDone: (ok: boolean) => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    input.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onDone(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onDone]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/v1/auth/reauth", { method: "POST", body: { password } });
      onDone(true);
    } catch (err) {
      setError(explain(err));
    } finally {
      setBusy(false);
      setPassword("");
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="reauth-title"
        className="w-full max-w-sm rounded-lg bg-white p-6 shadow-xl"
      >
        <h2 id="reauth-title" className="text-lg font-semibold">
          Confirm it is you
        </h2>
        <p className="mt-1 text-sm text-slate-600">
          Decisions that change terms or authorize payments need your password again.
        </p>
        <form onSubmit={submit} className="mt-4 space-y-3">
          <label className="block text-sm font-medium" htmlFor="reauth-password">
            Password
          </label>
          <input
            ref={input}
            id="reauth-password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full rounded border border-slate-300 px-3 py-2 focus:outline-2 focus:outline-indigo-600"
            required
          />
          {error && (
            <p role="alert" className="text-sm text-rose-700">
              {error}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <button type="button" className="btn-secondary" onClick={() => onDone(false)}>
              Cancel
            </button>
            <button type="submit" className="btn-primary" disabled={busy || !password}>
              {busy ? "Checking…" : "Confirm"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
