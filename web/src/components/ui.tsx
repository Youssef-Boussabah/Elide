import type { ReactNode } from "react";

type Tone = "good" | "warn" | "bad" | "info" | "neutral";

const TONES: Record<Tone, string> = {
  good: "bg-emerald-50 text-emerald-800 ring-emerald-200",
  warn: "bg-amber-50 text-amber-900 ring-amber-200",
  bad: "bg-rose-50 text-rose-800 ring-rose-200",
  info: "bg-indigo-50 text-indigo-800 ring-indigo-200",
  neutral: "bg-slate-100 text-slate-700 ring-slate-200",
};

export function Badge({ tone = "neutral", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span className={`inline-flex items-center rounded px-2 py-0.5 text-xs font-medium ring-1 ${TONES[tone]}`}>
      {children}
    </span>
  );
}

// Distinct state labels (doc 13): never conflate proposed, approved, paid and spendable.
const STATE_TONES: Record<string, Tone> = {
  SIMULATED: "info",
  SYNTHETIC: "info",
  SYNTHETIC_NETWORK: "info",
  PROPOSED: "neutral",
  proposed: "neutral",
  APPROVED: "good",
  approved: "good",
  agreed: "good",
  AGREED: "good",
  pending: "warn",
  PAYPAL_CONFIRMED: "good",
  FUNDS_USABLE_ATTESTED: "good",
  BLOCKED: "bad",
  declined: "bad",
  rejected: "bad",
  expired: "bad",
  superseded: "bad",
  withdrawn: "neutral",
  countered: "neutral",
  NOT_STARTED: "neutral",
  INTENT_CREATED_NOT_SENT: "warn",
  PREPARING_PAYPAL_INVOICE: "warn",
  AWAITING_PAYER_AT_PAYPAL: "warn",
  PROVIDER_PENDING: "warn",
  PROVIDER_OUTCOME_UNKNOWN: "bad",
  PAYPAL_CONFIRMED_NOT_BOOKED: "good",
  PAYPAL_CONFIRMED_RECONCILED: "good",
  FAILED: "bad",
  EXPIRED: "bad",
  CANCELLED: "neutral",
  NEEDS_OPERATOR_REVIEW: "bad",
  PROVIDER_CONFIRMED_NOT_SPENDABLE: "warn",
  NOT_USABLE_UNTIL_VERIFIED: "warn",
};

export function StateLabel({ state }: { state: string }) {
  return <Badge tone={STATE_TONES[state] ?? "neutral"}>{state.replaceAll("_", " ")}</Badge>;
}

export function Card({
  title,
  children,
  actions,
  id,
}: {
  title: string;
  children: ReactNode;
  actions?: ReactNode;
  id?: string;
}) {
  const hid = id ?? `card-${title.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
  return (
    <section aria-labelledby={hid} className="rounded-lg bg-white p-5 shadow-sm ring-1 ring-slate-200">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id={hid} className="text-base font-semibold text-slate-900">
          {title}
        </h2>
        {actions}
      </div>
      <div className="mt-3">{children}</div>
    </section>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <p role="status" aria-live="polite" className="py-6 text-sm text-slate-500">
      {label}
    </p>
  );
}

export function ErrorBanner({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-900">
      <span>{message}</span>
      {onRetry && (
        <button type="button" className="btn-secondary" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

export function Notice({ tone = "info", children }: { tone?: Tone; children: ReactNode }) {
  return <div className={`rounded-md px-4 py-3 text-sm ring-1 ${TONES[tone]}`}>{children}</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="rounded-md border border-dashed border-slate-300 px-4 py-6 text-center text-sm text-slate-500">{children}</p>;
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-xs uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className="text-sm font-medium text-slate-900">{children}</dd>
    </div>
  );
}
