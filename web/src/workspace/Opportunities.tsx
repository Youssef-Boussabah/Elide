import { dateTime, money } from "../api/format";
import type { Opportunities as Data } from "../api/types";
import { Badge, Card, Empty, ErrorBanner, Field, Loading, Notice, StateLabel } from "../components/ui";
import { useResource } from "../components/useResource";
import { href } from "./route";
import type { PageProps } from "./Shell";

const ACTION_TEXT: Record<string, { label: string; tone: "good" | "warn" | "info" | "neutral" | "bad" }> = {
  RECOMMEND_PLAN: { label: "Candidate plan for your approval", tone: "good" },
  REVIEW_AND_DECIDE: { label: "Review your part and decide", tone: "good" },
  REVIEW_ECONOMICS: { label: "Review the economics first", tone: "warn" },
  NEGOTIATE: { label: "A negotiated change could help", tone: "info" },
  NO_ACTION: { label: "No action needed", tone: "neutral" },
  NOTHING_FOR_YOU: { label: "Nothing for you", tone: "neutral" },
  BLOCKED: { label: "Blocked", tone: "bad" },
};

function cents(s: string | undefined): number | null {
  if (s === undefined) return null;
  const n = Number(String(s).split(" ")[0]);
  return Number.isFinite(n) ? Math.round(n) : null;
}

export function Opportunities({ merchant }: PageProps) {
  const r = useResource<Data>(`/v1/m/${merchant}/opportunities`);
  if (r.loading && !r.data) return <Loading label="Analysing your current data…" />;
  if (r.error) return <ErrorBanner message={r.error} onRetry={r.reload} />;
  const d = r.data;
  if (!d) return null;
  const o = d.opportunity;
  const a = ACTION_TEXT[o.recommendation.action] ?? { label: o.recommendation.action, tone: "neutral" as const };
  const c = o.comparison;
  return (
    <>
      <Notice>
        Read-only analysis computed at {dateTime(d.as_of)} from your authorized data. It is a{" "}
        <strong>simulation</strong>: nothing is paid, approved or changed, and no downstream
        funds are verified spendable.
      </Notice>
      <Card
        title="Recommendation"
        actions={
          <span className="flex gap-2">
            <Badge tone={a.tone}>{a.label}</Badge>
            <StateLabel state={o.status} />
          </span>
        }
      >
        <p className="text-sm">{o.recommendation.reason}</p>
        {o.my_flags.length > 0 && (
          <ul className="mt-3 space-y-1 text-sm">
            {o.my_flags.map((f) => (
              <li key={f.code + f.detail} className="flex gap-2">
                <Badge tone="warn">{f.code.replaceAll("_", " ")}</Badge>
                <span className="text-slate-700">{f.detail}</span>
              </li>
            ))}
          </ul>
        )}
        <div className="mt-4 flex flex-wrap gap-2">
          {o.my_negotiation_hints.length > 0 && (
            <a className="btn-primary" href={href(merchant, "negotiations")}>
              Open negotiations
            </a>
          )}
          {d.plan_preview.my_legs.length > 0 && (
            <a className="btn-secondary" href={href(merchant, "plan")}>
              Create a plan for approval
            </a>
          )}
        </div>
      </Card>

      {o.decision && (
        <Card title="Decision breakdown">
          {o.decision.feasible !== undefined ? (
            <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <Field label="Feasible (verified plan)">{o.decision.feasible ? "Yes" : "No"}</Field>
              <Field label="Your modeled net benefit">{money(o.decision.buyer_net_benefit_cents)}</Field>
              <Field label="Participants with undeclared losses">
                {o.decision.participants_with_undeclared_losses}
              </Field>
              <Field label="Approvals recorded / required">
                {o.decision.consent?.approvals_recorded} / {o.decision.consent?.approvals_required}
              </Field>
            </dl>
          ) : (
            <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3">
              <Field label="Your part in the plan">{o.decision.my_part_in_plan ? "Yes" : "None"}</Field>
              <Field label="Your modeled net benefit">{money(o.decision.my_net_benefit_cents)}</Field>
              <Field label="Your payments to approve">{o.decision.my_payments_to_approve ?? 0}</Field>
            </dl>
          )}
        </Card>
      )}

      {c ? (
        <Card title="Matched-baseline comparison">
          <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <Field label="Extra paid on time vs doing nothing (B0)">{money(cents(c.incremental_vs_B0))}</Field>
            <Field label="vs earliest-due-first (B1)">{money(cents(c.incremental_vs_B1))}</Field>
            <Field label="vs random tie-break (B3)">{money(cents(c.incremental_vs_B3))}</Field>
            <Field label="Gross flow (not new liquidity)">{money(cents(String(c.gross_flow_cents)))}</Field>
          </dl>
          <p className="mt-2 text-xs text-slate-500">
            Gross flow counts re-spent cash and is not created liquidity. Only the matched
            increments compare like with like.
          </p>
        </Card>
      ) : (
        o.aggregates?.status?.startsWith("WITHHELD") && (
          <Notice tone="neutral">{o.aggregates.explanation}</Notice>
        )
      )}

      <Card title="Your part of the simulated plan">
        {o.my_actions.length === 0 ? (
          <Empty>The plan includes no payment by or to you.</Empty>
        ) : (
          <table className="table">
            <caption className="sr-only">Payments in the plan that involve you</caption>
            <thead>
              <tr>
                <th scope="col">Invoice</th>
                <th scope="col">You</th>
                <th scope="col">Amount</th>
                <th scope="col">When</th>
                <th scope="col">State</th>
              </tr>
            </thead>
            <tbody>
              {o.my_actions.map((x) => (
                <tr key={x.obligation_id + x.action}>
                  <td className="font-mono text-xs">{x.obligation_id}</td>
                  <td>{x.action === "pay" ? `pay ${x.payee}` : `receive from ${x.payer}`}</td>
                  <td>{money(x.amount_cents)}</td>
                  <td>
                    {dateTime(x.slot_start)} {x.on_time ? "" : <Badge tone="bad">late</Badge>}
                  </td>
                  <td>
                    <StateLabel state="SIMULATED" />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <Card title="Blocked invoices">
        {o.my_bottlenecks.length === 0 ? (
          <Empty>None of your invoices is blocked.</Empty>
        ) : (
          <ul className="space-y-2 text-sm">
            {o.my_bottlenecks.map((b) => (
              <li key={b.obligation_id} className="flex flex-wrap items-center gap-2">
                <StateLabel state={b.unlocked_by_plan ? "PROPOSED" : "BLOCKED"} />
                <span className="font-mono text-xs">{b.obligation_id}</span>
                <span>{money(b.face_cents)}:</span>
                <span className="text-slate-700">{b.reason}</span>
              </li>
            ))}
          </ul>
        )}
        {o.my_negotiation_hints.map((h) => (
          <p key={h.obligation_id + h.kind} className="mt-3 text-sm">
            {h.kind === "DISCOUNT_COUNTEROFFER"
              ? `For ${h.obligation_id} the current offer is ${money(h.current_offer_cents)} off; at least ${money(h.minimum_needed_cents)} would fit your rules.`
              : h.detail}
          </p>
        ))}
      </Card>

      <Card title="Explanation">
        <p className="text-xs text-slate-500">
          {d.explanation.status === "TEMPLATE"
            ? "Deterministic template built only from the verified facts below (no model used)."
            : "Model-written from the verified facts below and checked."}
        </p>
        <ol className="mt-2 list-decimal space-y-1 pl-5 text-sm">
          {d.explanation.facts.map((f) => (
            <li key={f.id}>{f.text}</li>
          ))}
        </ol>
      </Card>
    </>
  );
}
