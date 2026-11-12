import { useState } from "react";
import type { FormEvent } from "react";
import { api, explain, newOperationKey } from "../api/client";
import { dateTime, fromLocalInput, money, parseDollarsToCents, toLocalInput } from "../api/format";
import type { Offer } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { Badge, Card, Empty, ErrorBanner, Field, Loading, Notice, StateLabel } from "../components/ui";
import { useResource } from "../components/useResource";
import type { PageProps } from "./Shell";

type Decision = "accept" | "reject" | "counter" | "withdraw";

export function Negotiations({ merchant, membership }: PageProps) {
  const r = useResource<{ offers: Offer[] }>(`/v1/m/${merchant}/offers`);
  const canDecide = membership.permissions.includes("offer:decide");
  // Page-level confirmation: a decided offer leaves the open list, so its card unmounts.
  const [notice, setNotice] = useState<string | null>(null);
  const changed = (message?: string) => {
    if (message) setNotice(message);
    r.reload();
  };
  if (r.loading && !r.data) return <Loading />;
  if (r.error) return <ErrorBanner message={r.error} onRetry={r.reload} />;
  const offers = r.data?.offers ?? [];
  const open = offers.filter((o) => o.status === "proposed");
  const closed = offers.filter((o) => o.status !== "proposed");
  return (
    <>
      {notice && (
        <p role="status" className="rounded-md bg-emerald-50 px-4 py-2 text-sm text-emerald-900 ring-1 ring-emerald-200">
          {notice}
        </p>
      )}
      <Notice tone="neutral">
        Each party decides independently from its own session. An agreed amendment changes the
        invoice terms only; it authorizes no payment. Payment approval is a separate step.
      </Notice>
      <Card title="Open offers">
        {open.length === 0 ? (
          <Empty>No open offers.</Empty>
        ) : (
          <div className="space-y-4">
            {open.map((o) => (
              <OfferCard key={o.offer_id} offer={o} merchant={merchant} canDecide={canDecide} onChange={changed} />
            ))}
          </div>
        )}
      </Card>
      <Card title="History">
        {closed.length === 0 ? (
          <Empty>No decided offers yet.</Empty>
        ) : (
          <table className="table">
            <caption className="sr-only">Decided offers</caption>
            <thead>
              <tr>
                <th scope="col">Invoice</th>
                <th scope="col">Proposed by</th>
                <th scope="col">Discount</th>
                <th scope="col">Pay</th>
                <th scope="col">Outcome</th>
                <th scope="col">Decisions</th>
              </tr>
            </thead>
            <tbody>
              {closed.map((o) => (
                <tr key={o.offer_id}>
                  <td className="font-mono text-xs">{o.obligation_id}</td>
                  <td>{o.proposer}</td>
                  <td>{money(o.concession_cents)}</td>
                  <td>{money(o.cash_cents)}</td>
                  <td>
                    <StateLabel state={o.status} />
                  </td>
                  <td className="text-xs text-slate-600">
                    {o.decisions.map((d) => `${d.merchant_id}: ${d.decision}`).join(" · ")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  );
}

function OfferCard({
  offer: o,
  merchant,
  canDecide,
  onChange,
}: {
  offer: Offer;
  merchant: string;
  canDecide: boolean;
  onChange: (message?: string) => void;
}) {
  const { withStepUp } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [countering, setCountering] = useState(false);
  const iPay = o.my_role === "debtor";

  const decide = async (decision: Decision, counter?: Record<string, unknown>) => {
    setBusy(true);
    setError(null);
    const key = newOperationKey(); // the same key is reused if a step-up retry happens
    try {
      const res = await withStepUp(() =>
        api<{ status: string }>(`/v1/m/${merchant}/offers/${o.offer_id}/decision`, {
          method: "POST",
          body: { decision, terms_digest: o.terms_digest, ...(counter ? { counter } : {}) },
          operationKey: key,
        }),
      );
      onChange(`Recorded: ${res.status}.`);
    } catch (err) {
      setError(explain(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <article aria-label={`Offer on ${o.obligation_id}`} className="rounded-md border border-slate-200 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-medium">
          <span className="font-mono text-sm">{o.obligation_id}</span>: {money(o.concession_cents)} off
          if paid by {dateTime(o.pay_by)}
        </h3>
        <span className="flex gap-2">
          <StateLabel state={o.status} />
          {o.proposer === merchant ? <Badge>your offer</Badge> : <Badge tone="info">from {o.proposer}</Badge>}
        </span>
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Field label="Invoice face">{money(o.face_cents)}</Field>
        <Field label={iPay ? "You would pay" : "You would receive"}>{money(o.cash_cents)}</Field>
        <Field label={iPay ? "You save" : "You give up"}>{money(o.concession_cents)}</Field>
        <Field label="Decide before">{dateTime(o.expires_at)}</Field>
      </dl>
      <p className="mt-2 text-sm text-slate-700">
        {iPay
          ? "Your consequence: you pay less, but by the pay-by date instead of the original due date. Check that this fits your deployable cash."
          : "Your consequence: you receive less, but sooner. Only you can judge whether earlier cash is worth the discount."}
      </p>
      {o.message && <blockquote className="mt-2 border-l-4 border-slate-200 pl-3 text-sm italic text-slate-600">{o.message}</blockquote>}
      <p className="mt-2 text-xs text-slate-500">
        Required independent approvals: {o.debtor} and {o.creditor}. Recorded:{" "}
        {o.decisions.map((d) => `${d.merchant_id} (${d.decision})`).join(", ") || "none"}. Terms digest{" "}
        <span className="font-mono">{o.terms_digest.slice(0, 12)}…</span>
      </p>
      {error && (
        <div className="mt-3">
          <ErrorBanner message={error} onRetry={() => onChange()} />
        </div>
      )}
      {canDecide && (o.can_decide || o.can_withdraw) && !countering && (
        <div className="mt-4 flex flex-wrap gap-2">
          {o.can_decide && (
            <>
              <button type="button" className="btn-primary" disabled={busy} onClick={() => void decide("accept")}>
                Accept these terms
              </button>
              <button type="button" className="btn-secondary" disabled={busy} onClick={() => setCountering(true)}>
                Counter
              </button>
              <button type="button" className="btn-danger" disabled={busy} onClick={() => void decide("reject")}>
                Reject
              </button>
            </>
          )}
          {o.can_withdraw && (
            <button type="button" className="btn-danger" disabled={busy} onClick={() => void decide("withdraw")}>
              Withdraw my offer
            </button>
          )}
        </div>
      )}
      {!canDecide && o.can_decide && <p className="mt-3 text-sm text-slate-600">Your role can view but not decide offers.</p>}
      {countering && (
        <CounterForm
          offer={o}
          merchant={merchant}
          busy={busy}
          onCancel={() => setCountering(false)}
          onSubmit={(terms) => void decide("counter", terms).then(() => setCountering(false))}
        />
      )}
    </article>
  );
}

function CounterForm({
  offer,
  merchant,
  busy,
  onCancel,
  onSubmit,
}: {
  offer: Offer;
  merchant: string;
  busy: boolean;
  onCancel: () => void;
  onSubmit: (terms: Record<string, unknown>) => void;
}) {
  const now = new Date();
  const [discount, setDiscount] = useState((offer.concession_cents / 100).toFixed(2));
  const [expires, setExpires] = useState(toLocalInput(new Date(now.getTime() + 24 * 3600e3)));
  const [payBy, setPayBy] = useState(toLocalInput(new Date(now.getTime() + 48 * 3600e3)));
  const [message, setMessage] = useState("");
  const [hint, setHint] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const iPay = offer.my_role === "debtor";

  const suggest = async () => {
    setHint(null);
    try {
      const r = await api<{ suggested_concession_cents: number | null; allowed_range_cents: number[] | null; note: string; reason_codes: string[] }>(
        `/v1/m/${merchant}/offers/draft`,
        { method: "POST", body: { obligation_id: offer.obligation_id, max_concession_cents: 5000 } },
      );
      if (r.suggested_concession_cents !== null) {
        setDiscount((r.suggested_concession_cents / 100).toFixed(2));
        setHint(`Smallest discount that fits your own rules and cash: ${money(r.suggested_concession_cents)}. ${r.note}`);
      } else {
        setHint(r.note || `No bounded counteroffer fits (${r.reason_codes.join(", ")}).`);
      }
    } catch (err) {
      setHint(explain(err));
    }
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const c = parseDollarsToCents(discount);
    if (c === null) {
      setError("Enter the discount in dollars, for example 10.00.");
      return;
    }
    setError(null);
    onSubmit({
      concession_cents: c,
      pay_by: fromLocalInput(payBy),
      expires_at: fromLocalInput(expires),
      message: message.trim() || null,
    });
  };

  return (
    <form onSubmit={submit} className="mt-4 space-y-3 rounded-md bg-slate-50 p-4" aria-label="Counteroffer">
      <div className="grid gap-3 sm:grid-cols-3">
        <label className="text-sm">
          Discount (USD)
          <input className="input mt-1" inputMode="decimal" value={discount} onChange={(e) => setDiscount(e.target.value)} required />
        </label>
        <label className="text-sm">
          Offer expires
          <input className="input mt-1" type="datetime-local" value={expires} onChange={(e) => setExpires(e.target.value)} required />
        </label>
        <label className="text-sm">
          Pay by
          <input className="input mt-1" type="datetime-local" value={payBy} onChange={(e) => setPayBy(e.target.value)} required />
        </label>
      </div>
      <label className="block text-sm">
        Message to the counterparty (optional; no private figures)
        <textarea className="input mt-1" rows={2} maxLength={500} value={message} onChange={(e) => setMessage(e.target.value)} />
      </label>
      {hint && <p className="text-sm text-slate-700">{hint}</p>}
      {error && (
        <p role="alert" className="text-sm text-rose-700">
          {error}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <button type="submit" className="btn-primary" disabled={busy}>
          Send counteroffer
        </button>
        {iPay && (
          <button type="button" className="btn-secondary" onClick={() => void suggest()}>
            Suggest a bounded discount
          </button>
        )}
        <button type="button" className="btn-secondary" onClick={onCancel}>
          Cancel
        </button>
      </div>
      <p className="text-xs text-slate-500">
        Sending a counteroffer records your own acceptance of these terms. The other party
        must still accept independently.
      </p>
    </form>
  );
}
