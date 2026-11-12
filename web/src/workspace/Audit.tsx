import { dateTime } from "../api/format";
import type { AuditEvent } from "../api/types";
import { Badge, Card, Empty, ErrorBanner, Loading, Notice } from "../components/ui";
import { useResource } from "../components/useResource";
import type { PageProps } from "./Shell";

export function Audit({ merchant, membership }: PageProps) {
  const allowed = membership.permissions.includes("audit:read");
  const r = useResource<{ events: AuditEvent[] }>(allowed ? `/v1/m/${merchant}/audit` : null);
  if (!allowed) return <Notice tone="neutral">Your role cannot read the audit trail.</Notice>;
  if (r.loading && !r.data) return <Loading />;
  if (r.error) return <ErrorBanner message={r.error} onRetry={r.reload} />;
  const events = r.data?.events ?? [];
  return (
    <Card title="Audit history">
      <p className="mb-3 text-xs text-slate-500">
        Append-only and hash-chained. Authenticated application evidence of who did what; not a
        legally binding electronic signature.
      </p>
      {events.length === 0 ? (
        <Empty>No events yet.</Empty>
      ) : (
        <div className="overflow-x-auto">
          <table className="table">
            <caption className="sr-only">Audit events for this merchant, newest first</caption>
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">Action</th>
                <th scope="col">Object</th>
                <th scope="col">Actor</th>
                <th scope="col">Outcome</th>
                <th scope="col">Details</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e) => (
                <tr key={e.id}>
                  <td className="whitespace-nowrap">{dateTime(e.occurred_at)}</td>
                  <td className="font-mono text-xs">{e.action}</td>
                  <td className="font-mono text-xs">
                    {e.object_type}:{e.object_id}
                  </td>
                  <td className="font-mono text-xs">{e.actor_user_id ?? "system"}</td>
                  <td>
                    <Badge tone={e.outcome === "ok" ? "good" : "bad"}>{e.outcome}</Badge>
                  </td>
                  <td className="max-w-xs break-words text-xs text-slate-600">{JSON.stringify(e.detail)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
