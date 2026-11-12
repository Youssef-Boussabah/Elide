import { dateTime, money } from "../api/format";
import type { Overview as OverviewData } from "../api/types";
import { Badge, Card, Empty, ErrorBanner, Field, Loading, Notice, StateLabel } from "../components/ui";
import { useResource } from "../components/useResource";
import { href } from "./route";
import type { PageProps } from "./Shell";

const BASIS_TONE: Record<string, "good" | "warn" | "info" | "bad"> = {
  PROVIDER_VERIFIED: "good",
  MERCHANT_ATTESTED: "warn",
  SYNTHETIC: "info",
  NONE: "bad",
  UNVERIFIED: "bad",
};

export function Overview({ merchant }: PageProps) {
  const r = useResource<OverviewData>(`/v1/m/${merchant}/overview`);
  if (r.loading && !r.data) return <Loading />;
  if (r.error) return <ErrorBanner message={r.error} onRetry={r.reload} />;
  const d = r.data;
  if (!d) return null;
  const cap = d.capacity;
  return (
    <>
      {d.labels.includes("SYNTHETIC_NETWORK") && (
        <Notice>
          Synthetic network: balances, invoices and counterparties are simulation data, not
          real merchant records.
        </Notice>
      )}
      <Card
        title="Liquidity"
        actions={
          <span className="flex items-center gap-2 text-xs text-slate-500">
            Evidence <Badge tone={BASIS_TONE[cap.basis] ?? "warn"}>{cap.basis.replaceAll("_", " ")}</Badge>
            <StateLabel state={cap.status} />
          </span>
        }
      >
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
          <Field label="Deployable now">{money(cap.deployable_cents)}</Field>
          <Field label="Verified spendable">{money(cap.verified_spendable_cents)}</Field>
          <Field label="Protected reserve">{money(cap.protected_reserve_cents)}</Field>
          <Field label="Committed">{money(cap.committed_cents)}</Field>
          <Field label="Held / pending">
            {money(cap.held_cents)} / {money(cap.pending_cents)}
          </Field>
          <Field label="Observed · valid until">
            <span className="font-normal">
              {dateTime(cap.observed_at)}
              <br />
              {dateTime(cap.valid_until)}
            </span>
          </Field>
        </dl>
        <p className="mt-3 text-xs text-slate-500">{cap.explanation}</p>
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Card title="Needs your attention">
          <ul className="space-y-2 text-sm">
            <li>
              {d.offers_awaiting_me > 0 ? (
                <a className="text-indigo-800 underline" href={href(merchant, "negotiations")}>
                  {d.offers_awaiting_me} offer(s) awaiting your decision
                </a>
              ) : (
                "No offers awaiting your decision."
              )}
            </li>
            <li>
              <a className="text-indigo-800 underline" href={href(merchant, "opportunities")}>
                Review recommendations from your current data
              </a>
            </li>
          </ul>
        </Card>
        <Card title="Policy">
          {d.policy.confirmed_revision ? (
            <p className="text-sm">Confirmed policy revision {d.policy.confirmed_revision} is in force.</p>
          ) : (
            <Notice tone="warn">
              No confirmed policy. Elide assumes no change is acceptable to you until you{" "}
              <a className="underline" href={href(merchant, "policy")}>
                confirm one
              </a>
              .
            </Notice>
          )}
        </Card>
      </div>

      <Card title="Your invoices">
        {d.invoices.length === 0 ? (
          <Empty>No invoices involve this merchant.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="table">
              <caption className="sr-only">Invoices where you are the payer or the payee</caption>
              <thead>
                <tr>
                  <th scope="col">Invoice</th>
                  <th scope="col">Direction</th>
                  <th scope="col">Counterparty</th>
                  <th scope="col">Open</th>
                  <th scope="col">Due</th>
                  <th scope="col">Terms</th>
                  <th scope="col">Payment</th>
                </tr>
              </thead>
              <tbody>
                {d.invoices.map((i) => (
                  <tr key={i.obligation_id}>
                    <td className="font-mono text-xs">{i.obligation_id}</td>
                    <td>{i.direction === "payable" ? "You pay" : "You receive"}</td>
                    <td>{i.counterparty_name ?? i.counterparty}</td>
                    <td>
                      {money(i.open_cents, i.currency)}
                      {i.concession_cents > 0 && (
                        <span className="block text-xs text-slate-500">
                          after {money(i.concession_cents, i.currency)} agreed discount
                        </span>
                      )}
                    </td>
                    <td>{dateTime(i.due_at)}</td>
                    <td>
                      {i.open_offer_id ? <StateLabel state="PROPOSED" /> : <span className="text-slate-600">rev {i.revision_no} in force</span>}
                    </td>
                    <td>
                      <StateLabel state={i.payment_state} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}
