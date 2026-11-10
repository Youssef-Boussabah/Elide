import { useEffect, useState } from "react";
import { fetchHealth, type HealthView } from "./api/health";

type Row = { label: string; value: string; tone: "good" | "warn" | "bad" | "neutral" };

const toneClass: Record<Row["tone"], string> = {
  good: "bg-emerald-50 text-emerald-800 ring-emerald-200",
  warn: "bg-amber-50 text-amber-800 ring-amber-200",
  bad: "bg-rose-50 text-rose-800 ring-rose-200",
  neutral: "bg-slate-50 text-slate-700 ring-slate-200",
};

function rows(view: HealthView | null): Row[] {
  if (view === null) return [{ label: "Backend API", value: "Checking…", tone: "neutral" }];
  if (view.kind === "api-unreachable") {
    return [{ label: "Backend API", value: `Unreachable: ${view.message}`, tone: "bad" }];
  }
  const r = view.readiness;
  return [
    { label: "Backend API", value: "Reachable", tone: "good" },
    {
      label: "PostgreSQL",
      value: r.database === "ok" ? `Connected (v${r.postgres_version ?? "?"})` : "Unreachable",
      tone: r.database === "ok" ? "good" : "bad",
    },
    {
      // Configured is not the same as verified: verification is the Sandbox check script.
      label: "PayPal Sandbox credentials",
      value: r.paypal_sandbox_configured ? "Configured (not yet verified here)" : "Not configured",
      tone: r.paypal_sandbox_configured ? "neutral" : "warn",
    },
    {
      label: "LLM adapter",
      value: r.llm_configured ? "Configured" : "Not configured (Phase 3)",
      tone: r.llm_configured ? "neutral" : "warn",
    },
  ];
}

export default function App() {
  const [view, setView] = useState<HealthView | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    fetchHealth(ctrl.signal).then(setView);
    return () => ctrl.abort();
  }, []);

  return (
    <main className="min-h-screen bg-slate-100 px-4 py-10 text-slate-900">
      <div className="mx-auto max-w-2xl">
        <h1 className="text-3xl font-semibold tracking-tight">Elide</h1>
        <p className="mt-2 text-slate-600">
          Permissioned, network-aware invoice payment and liquidity coordination.
        </p>
        <div className="mt-4 rounded-md border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
          Phase 0 development scaffold. No financial planning, approvals or payments are
          implemented yet.
        </div>

        <section aria-labelledby="status-heading" className="mt-8 rounded-lg bg-white p-6 shadow-sm">
          <h2 id="status-heading" className="text-lg font-medium">
            System status
          </h2>
          <dl className="mt-4 divide-y divide-slate-100">
            {rows(view).map((row) => (
              <div key={row.label} className="flex flex-wrap items-center justify-between gap-2 py-3">
                <dt className="text-sm text-slate-600">{row.label}</dt>
                <dd className={`rounded px-2 py-0.5 text-sm ring-1 ${toneClass[row.tone]}`}>
                  {row.value}
                </dd>
              </div>
            ))}
          </dl>
        </section>
      </div>
    </main>
  );
}
