import { useState } from "react";
import { api, explain, newOperationKey } from "../api/client";
import { dateTime, money } from "../api/format";
import type { PaymentIntent, PlanLeg, PlanSummary, PlanView } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { Badge, Card, Empty, ErrorBanner, Field, Loading, Notice, StateLabel } from "../components/ui";
import { useResource } from "../components/useResource";
import { IntentRow } from "./Execution";
import { href } from "./route";
import type { PageProps } from "./Shell";

const AUTH_LABEL: Record<string, { label: string; tone: "good" | "warn" | "bad" | "neutral" }> = {
  approved: { label: "APPROVED", tone: "good" },
  declined: { label: "DECLINED", tone: "bad" },
  pending: { label: "PENDING YOUR DECISION", tone: "warn" },
  expired: { label: "APPROVAL EXPIRED", tone: "bad" },
  needs_reapproval: { label: "CONDITIONS CHANGED: RE-APPROVE", tone: "bad" },
};

/** Durable plans are created explicitly (POST); viewing never writes anything. */
export function PlanPage({ merchant, membership }: PageProps) {
  const canAuthorize = membership.permissions.includes("payment:authorize");
  const list = useResource<{ plans: PlanSummary[] }>(`/v1/m/${merchant}/plans`);
  const latest = list.data?.plans[0]?.plan_id ?? null;
  const plan = useResource<PlanView>(latest ? `/v1/m/${merchant}/plans/${latest}` : null);
  const intents = useResource<{ intents: PaymentIntent[] }>(`/v1/m/${merchant}/payment-intents`);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const reload = () => {
    list.reload();
    plan.reload();
    intents.reload();
  };

  const create = async () => {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const r = await api<{ plan: PlanView; reused: boolean }>(`/v1/m/${merchant}/plans`, {
        method: "POST",
        body: {},
        operationKey: newOperationKey(),
      });
      setNote(
        r.reused
          ? "Nothing economic changed: the current verified plan was reused (no new record)."
          : "A new plan was computed, independently verified and stored.",
      );
      reload();
    } catch (err) {
      setError(explain(err));
    } finally {
      setBusy(false);
    }
  };

  if (list.loading && !list.data) return <Loading />;
  if (list.error) return <ErrorBanner message={list.error} onRetry={list.reload} />;
  const p = plan.data;
  return (
    <>
      <Card
        title="Plan for approval"
        actions={
          canAuthorize && (
            <button type="button" className="btn-primary" disabled={busy} onClick={() => void create()}>
              {latest ? "Re-check and update plan" : "Create plan for approval"}
            </button>
          )
        }
      >
        <p className="text-sm text-slate-700">
          A durable plan is a versioned snapshot of current terms and evidence, checked by the
          independent verifier. Approving your leg records your payment authorization only; no
          PayPal payment is created or sent in this release.
        </p>
        {note && (
          <p role="status" className="mt-2 text-sm text-emerald-800">
            {note}
          </p>
        )}
        {error && (
          <div className="mt-3">
            <ErrorBanner message={error} />
          </div>
        )}
        {!latest && <div className="mt-3"><Empty>No durable plan yet.</Empty></div>}
        {latest && plan.loading && !p && <Loading />}
        {plan.error && <ErrorBanner message={plan.error} onRetry={plan.reload} />}
        {p && (
          <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Field label="Plan">
              <span className="font-mono text-xs">{p.plan_id}</span>
            </Field>
            <Field label="Computed for">{dateTime(p.as_of)}</Field>
            <Field label="Verifier">{p.solver_status}</Field>
            <Field label="Status">
              <StateLabel state={p.status} />
            </Field>
          </dl>
        )}
      </Card>

      {p && (
        <Card title="Your legs">
          {p.my_legs.length === 0 ? (
            <Empty>This plan has no payment by or to you.</Empty>
          ) : (
            <div className="space-y-4">
              {p.my_legs.map((leg) => (
                <LegCard
                  key={leg.leg_id}
                  leg={leg}
                  merchant={merchant}
                  planId={p.plan_id}
                  canAuthorize={canAuthorize}
                  intent={intents.data?.intents.find((i) => i.obligation_id === leg.obligation_id)}
                  onChange={reload}
                />
              ))}
            </div>
          )}
          {p.other_legs_in_plan !== undefined && p.other_legs_in_plan > 0 && (
            <p className="mt-3 text-xs text-slate-500">
              {p.other_legs_in_plan} other payment(s) in the plan do not involve you (shown with
              every participant&apos;s consent).
            </p>
          )}
        </Card>
      )}

      <Card title="Payment intents">
        {(intents.data?.intents ?? []).length === 0 ? (
          <Empty>No payment intents.</Empty>
        ) : (
          <>
            <p className="mb-3 text-sm text-slate-600">
              Invoices with a prepared intent are in execution: their cash stays reserved and they
              are not planned again. Payments that depend on them wait until PayPal confirms the
              payment and the receiving merchant&apos;s funds are verified usable.
            </p>
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
                  <IntentRow
                    key={i.intent_id}
                    intent={i}
                    merchant={merchant}
                    canAuthorize={canAuthorize}
                    onChange={reload}
                  />
                ))}
              </tbody>
            </table>
          </>
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
  const auth = leg.payment_authorization ?? { status: "pending" };
  const label = AUTH_LABEL[auth.status] ?? { label: auth.status, tone: "neutral" as const };
  const canDecide =
    iPay && canAuthorize && leg.authorize && leg.authorize.terms_agreed && leg.authorize.plan_current;

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
          <Badge tone={label.tone}>{label.label}</Badge>
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
        {iPay && <Field label="Fee ceiling">{money(leg.authorize?.fee_ceiling_cents ?? leg.payer_fee_cents)}</Field>}
        <Field label="Terms">{leg.terms === "in_force" ? "in force (agreed)" : "not yet agreed"}</Field>
      </dl>
      {iPay && auth.status === "needs_reapproval" && (
        <div className="mt-3">
          <Notice tone="warn">{auth.reason ?? "The conditions of your earlier approval changed."}</Notice>
        </div>
      )}
      {iPay && auth.status === "expired" && (
        <div className="mt-3">
          <Notice tone="warn">Your earlier approval expired and is no longer valid.</Notice>
        </div>
      )}
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
      {iPay && leg.authorize && leg.authorize.terms_agreed && !leg.authorize.plan_current && (
        <div className="mt-3">
          <Notice tone="warn">
            Terms, evidence or policies changed since this plan was computed. Use “Re-check and
            update plan” before deciding.
          </Notice>
        </div>
      )}
      {error && (
        <div className="mt-3">
          <ErrorBanner message={error} onRetry={onChange} />
        </div>
      )}
      {canDecide && leg.authorize && (
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
            Re-checks current conditions, your authority, the independent verifier and deployable
            cash, then reserves the cash. No PayPal call is made; the outbox message stays held.
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
