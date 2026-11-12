import { useState } from "react";
import { api, explain, newOperationKey } from "../api/client";
import { dateTime, money } from "../api/format";
import type { Opportunities, PaymentIntent, PlanLeg } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { Badge, Card, Empty, ErrorBanner, Field, Loading, Notice, StateLabel } from "../components/ui";
import { useResource } from "../components/useResource";
import { href } from "./route";
import type { PageProps } from "./Shell";

export function PlanPage({ merchant, membership }: PageProps) {
  const r = useResource<Opportunities>(`/v1/m/${merchant}/opportunities`);
  const intents = useResource<{ intents: PaymentIntent[] }>(`/v1/m/${merchant}/payment-intents`);
  const canAuthorize = membership.permissions.includes("payment:authorize");
  if (r.loading && !r.data) return <Loading label="Computing and verifying the plan…" />;
  if (r.error) return <ErrorBanner message={r.error} onRetry={r.reload} />;
  const plan = r.data?.plan;
  if (!plan) return null;
  const reload = () => {
    r.reload();
    intents.reload();
  };
  return (
    <>
      <Notice tone="neutral">
        Plan {plan.plan_id} was verified by the independent replay verifier (solver{" "}
        {plan.solver_status}). Approving your leg records your payment authorization only; no
        PayPal payment is created or sent in this release.
      </Notice>
      <Card title="Your legs" actions={<StateLabel state={plan.status} />}>
        {plan.my_legs.length === 0 ? (
          <Empty>The current plan has no payment by or to you.</Empty>
        ) : (
          <div className="space-y-4">
            {plan.my_legs.map((leg) => (
              <LegCard
                key={leg.leg_id}
                leg={leg}
                merchant={merchant}
                planId={plan.plan_id}
                canAuthorize={canAuthorize}
                intent={intents.data?.intents.find((i) => i.obligation_id === leg.obligation_id)}
                onChange={reload}
              />
            ))}
          </div>
        )}
        {plan.downstream_legs_count > 0 && (
          <p className="mt-3 text-xs text-slate-500">
            {plan.downstream_legs_count} other payment(s) in the plan do not involve you and are
            not shown.
          </p>
        )}
      </Card>
      <Card title="Payment intents">
        {(intents.data?.intents ?? []).length > 0 && (
          <p className="mb-3 text-sm text-slate-600">
            Invoices with a prepared intent are in execution: their cash stays reserved and they
            are not planned again. Payments that depend on them wait until PayPal confirms the
            payment and the receiving merchant&apos;s funds are verified usable.
          </p>
        )}
        {(intents.data?.intents ?? []).length === 0 ? (
          <Empty>No payment intents.</Empty>
        ) : (
          <table className="table">
            <caption className="sr-only">Your payment intents</caption>
            <thead>
              <tr>
                <th scope="col">Intent</th>
                <th scope="col">Invoice</th>
                <th scope="col">Payee</th>
                <th scope="col">Amount</th>
                <th scope="col">Status</th>
                <th scope="col">Execution</th>
              </tr>
            </thead>
            <tbody>
              {intents.data?.intents.map((i) => (
                <tr key={i.intent_id}>
                  <td className="font-mono text-xs">{i.intent_id}</td>
                  <td className="font-mono text-xs">{i.obligation_id}</td>
                  <td>{i.payee_merchant}</td>
                  <td>{money(i.amount_cents)}</td>
                  <td>
                    <StateLabel state={i.status === "awaiting_payer_action" ? "INTENT_CREATED_NOT_SENT" : i.status} />
                  </td>
                  <td>
                    <Badge tone="warn">{i.execution === "disabled" ? "held: no PayPal call" : i.execution}</Badge>
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

function LegCard({
  leg,
  merchant,
  planId,
  canAuthorize,
  intent,
  onChange,
}: {
  leg: PlanLeg;
  merchant: string;
  planId: string;
  canAuthorize: boolean;
  intent?: PaymentIntent;
  onChange: () => void;
}) {
  const { withStepUp } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<"approve" | "decline" | null>(null);
  const iPay = leg.payer === merchant;
  const auth = leg.payment_authorization;

  const authorize = async (decision: "approve" | "decline") => {
    if (!leg.authorize) return;
    setBusy(true);
    setError(null);
    const key = newOperationKey();
    try {
      await withStepUp(() =>
        api(`/v1/m/${merchant}/plans/${planId}/authorizations`, {
          method: "POST",
          body: { leg_id: leg.leg_id, decision, binding_digest: leg.authorize?.binding_digest },
          operationKey: key,
        }),
      );
      setConfirming(null);
      onChange();
    } catch (err) {
      setError(explain(err));
    } finally {
      setBusy(false);
    }
  };

  const createIntent = async () => {
    if (!auth.authorization_id) return;
    setBusy(true);
    setError(null);
    const key = newOperationKey();
    try {
      await withStepUp(() =>
        api(`/v1/m/${merchant}/payment-intents`, {
          method: "POST",
          body: { authorization_id: auth.authorization_id },
          operationKey: key,
        }),
      );
      onChange();
    } catch (err) {
      setError(explain(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <article aria-label={`Leg ${leg.leg_id}`} className="rounded-md border border-slate-200 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-medium">
          {iPay ? `You pay ${leg.payee}` : `You receive from ${leg.payer}`}: {money(leg.amount_cents)}
        </h3>
        <span className="flex gap-2">
          <StateLabel state={auth.status === "approved" ? "APPROVED" : auth.status} />
          {leg.has_upstream_dependency && <Badge tone="warn">depends on an incoming payment</Badge>}
        </span>
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-5">
        <Field label="Invoice / revision">
          <span className="font-mono text-xs">
            {leg.obligation_id} r{leg.revision_no}
          </span>
        </Field>
        <Field label="Planned date">{dateTime(leg.slot_start)}</Field>
        <Field label="Agreed discount">{money(leg.concession_cents)}</Field>
        <Field label="Fee ceiling">{money(leg.authorize?.fee_ceiling_cents ?? leg.payer_fee_cents)}</Field>
        <Field label="Terms">{leg.terms === "in_force" ? "in force (agreed)" : "not yet agreed"}</Field>
      </dl>
      {iPay && leg.authorize && !leg.authorize.terms_agreed && (
        <div className="mt-3">
          <Notice tone="warn">
            This leg relies on terms that are not yet an agreed amendment. Agree them in{" "}
            <a className="underline" href={href(merchant, "negotiations")}>
              Negotiations
            </a>{" "}
            before authorizing payment.
          </Notice>
        </div>
      )}
      {error && (
        <div className="mt-3">
          <ErrorBanner message={error} onRetry={onChange} />
        </div>
      )}
      {iPay && canAuthorize && leg.authorize && leg.authorize.terms_agreed && auth.status === "pending" && (
        <div className="mt-4">
          {confirming ? (
            <div className="rounded-md bg-slate-50 p-3 text-sm">
              <p>
                {confirming === "approve" ? "Authorize" : "Decline"} paying exactly{" "}
                <strong>{money(leg.amount_cents)}</strong> to <strong>{leg.payee}</strong> for{" "}
                {leg.obligation_id} (revision {leg.revision_no}), fee ceiling{" "}
                {money(leg.authorize.fee_ceiling_cents)}, valid until {dateTime(leg.authorize.expires_at)}?
              </p>
              <div className="mt-2 flex gap-2">
                <button type="button" className={confirming === "approve" ? "btn-primary" : "btn-danger"} disabled={busy} onClick={() => void authorize(confirming)}>
                  Confirm {confirming}
                </button>
                <button type="button" className="btn-secondary" onClick={() => setConfirming(null)}>
                  Back
                </button>
              </div>
            </div>
          ) : (
            <div className="flex gap-2">
              <button type="button" className="btn-primary" onClick={() => setConfirming("approve")}>
                Approve my payment…
              </button>
              <button type="button" className="btn-danger" onClick={() => setConfirming("decline")}>
                Decline…
              </button>
            </div>
          )}
        </div>
      )}
      {iPay && canAuthorize && auth.status === "approved" && !intent && (
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <button type="button" className="btn-secondary" disabled={busy} onClick={() => void createIntent()}>
            Prepare payment intent
          </button>
          <span className="text-xs text-slate-500">
            Re-checks terms, your authority and deployable cash, then reserves the cash. No PayPal
            call is made; the outbox message stays held.
          </span>
        </div>
      )}
      {intent && (
        <p className="mt-3 text-sm">
          Intent {intent.intent_id}: <StateLabel state="INTENT_CREATED_NOT_SENT" /> Money moved: no.
        </p>
      )}
    </article>
  );
}
