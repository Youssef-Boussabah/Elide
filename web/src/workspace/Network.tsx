import { dateTime, money } from "../api/format";
import type { NetworkView } from "../api/types";
import { Card, Empty, ErrorBanner, Loading, Notice, StateLabel } from "../components/ui";
import { useResource } from "../components/useResource";
import type { PageProps } from "./Shell";

/** Merchant-scoped graph: you and your direct counterparties only, plus a table. */
export function NetworkPage({ merchant }: PageProps) {
  const r = useResource<NetworkView>(`/v1/m/${merchant}/network`);
  if (r.loading && !r.data) return <Loading />;
  if (r.error) return <ErrorBanner message={r.error} onRetry={r.reload} />;
  const d = r.data;
  if (!d) return null;
  const others = d.nodes.filter((n) => !n.is_me);
  const W = 520;
  const H = 320;
  const pos = new Map<string, { x: number; y: number }>();
  pos.set(merchant, { x: W / 2, y: H / 2 });
  others.forEach((n, i) => {
    const angle = (2 * Math.PI * i) / Math.max(others.length, 1) - Math.PI / 2;
    pos.set(n.merchant_id, { x: W / 2 + 190 * Math.cos(angle), y: H / 2 + 115 * Math.sin(angle) });
  });
  const name = (id: string) => d.nodes.find((n) => n.merchant_id === id)?.display_name ?? id;
  return (
    <>
      <Notice tone="neutral">
        You see only invoices you are a party to. Other merchants&apos; invoices, balances and
        counterparties are never shown.
      </Notice>
      <Card title="Your payment relationships">
        {d.edges.length === 0 ? (
          <Empty>No relationships yet.</Empty>
        ) : (
          <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Diagram of your direct payment relationships; the table below lists the same data" className="h-auto w-full max-w-2xl">
            <defs>
              <marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                <path d="M 0 0 L 10 5 L 0 10 z" fill="#475569" />
              </marker>
            </defs>
            {d.edges.map((e) => {
              const a = pos.get(e.from);
              const b = pos.get(e.to);
              if (!a || !b) return null;
              const dx = b.x - a.x;
              const dy = b.y - a.y;
              const len = Math.hypot(dx, dy) || 1;
              const pad = 46;
              return (
                <line
                  key={e.obligation_id}
                  x1={a.x + (dx / len) * pad}
                  y1={a.y + (dy / len) * pad}
                  x2={b.x - (dx / len) * pad}
                  y2={b.y - (dy / len) * pad}
                  stroke={e.terms_state === "OFFER_OPEN" ? "#4f46e5" : "#475569"}
                  strokeDasharray={e.terms_state === "OFFER_OPEN" ? "6 4" : undefined}
                  strokeWidth={2}
                  markerEnd="url(#arrow)"
                />
              );
            })}
            {d.nodes.map((n) => {
              const p = pos.get(n.merchant_id);
              if (!p) return null;
              return (
                <g key={n.merchant_id}>
                  <circle cx={p.x} cy={p.y} r={40} fill={n.is_me ? "#e0e7ff" : "#f1f5f9"} stroke={n.is_me ? "#4338ca" : "#94a3b8"} />
                  <text x={p.x} y={p.y + 4} textAnchor="middle" fontSize="11" fill="#0f172a">
                    {(n.display_name ?? n.merchant_id).replace(" (synthetic)", "").slice(0, 16)}
                  </text>
                </g>
              );
            })}
          </svg>
        )}
      </Card>
      <Card title="Relationship table">
        <div className="overflow-x-auto">
          <table className="table">
            <caption className="sr-only">Your direct payment relationships</caption>
            <thead>
              <tr>
                <th scope="col">Invoice</th>
                <th scope="col">From</th>
                <th scope="col">To</th>
                <th scope="col">Open</th>
                <th scope="col">Due</th>
                <th scope="col">Terms</th>
                <th scope="col">Payment</th>
                <th scope="col">Funds</th>
              </tr>
            </thead>
            <tbody>
              {d.edges.map((e) => (
                <tr key={e.obligation_id}>
                  <td className="font-mono text-xs">{e.obligation_id}</td>
                  <td>{name(e.from)}</td>
                  <td>{name(e.to)}</td>
                  <td>{money(e.open_cents)}</td>
                  <td>{dateTime(e.due_at)}</td>
                  <td>
                    <StateLabel state={e.terms_state === "OFFER_OPEN" ? "PROPOSED" : "IN_FORCE"} />
                  </td>
                  <td>
                    <StateLabel state={e.payment_state} />
                  </td>
                  <td>
                    <StateLabel state={e.funds_state} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="mt-3 text-xs text-slate-500">
          States are kept distinct: {d.legend.map((l) => l.replaceAll("_", " ").toLowerCase()).join(" · ")}.
          A PayPal-confirmed payment is not spendable until separately verified.
        </p>
      </Card>
    </>
  );
}
