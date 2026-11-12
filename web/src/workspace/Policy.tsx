import { useState } from "react";
import type { FormEvent } from "react";
import { api, explain } from "../api/client";
import { dateTime, money, parseDollarsToCents } from "../api/format";
import type { PolicyDraft, PolicyRules, PolicyView } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { Badge, Card, Empty, ErrorBanner, Field, Loading, Notice } from "../components/ui";
import { useResource } from "../components/useResource";
import type { PageProps } from "./Shell";

function Rules({ rules }: { rules: PolicyRules }) {
  return (
    <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <Field label="Protected reserve floor">{money(rules.reserve_floor_cents)}</Field>
      <Field label="Minimum early-payment discount">{money(rules.min_early_discount_cents)}</Field>
      <Field label="Max early commitment">{money(rules.max_early_commitment_cents)}</Field>
      <Field label="Fee ceiling per payment">{money(rules.fee_ceiling_cents)}</Field>
    </dl>
  );
}

/** Finds the amount as written in the text, so the validator can ground it (source span). */
function quoteFor(text: string, amount: string): string | null {
  const plain = amount.trim();
  if (!plain) return null;
  for (const cand of [`$${plain}`, `${plain} USD`, `USD ${plain}`, plain]) {
    const i = text.indexOf(cand);
    if (i >= 0) {
      const start = Math.max(0, text.lastIndexOf(" ", Math.max(0, i - 2)) + 1);
      const end = Math.min(text.length, i + cand.length);
      return text.slice(start, end);
    }
  }
  return plain; // not found: the validator will report it as ungrounded
}

export function PolicyPage({ merchant, membership }: PageProps) {
  const r = useResource<PolicyView>(`/v1/m/${merchant}/policy`);
  const canEdit = membership.permissions.includes("policy:edit");
  if (r.loading && !r.data) return <Loading />;
  if (r.error) return <ErrorBanner message={r.error} onRetry={r.reload} />;
  const v = r.data;
  if (!v) return null;
  return (
    <>
      <Card
        title="Confirmed policy"
        actions={v.confirmed ? <Badge tone="good">revision {v.confirmed.revision_no} in force</Badge> : <Badge tone="warn">none</Badge>}
      >
        {v.confirmed ? (
          <>
            <Rules rules={v.confirmed.rules} />
            <p className="mt-2 text-xs text-slate-500">
              Confirmed {dateTime(v.confirmed.confirmed_at)} · digest{" "}
              <span className="font-mono">{v.confirmed.digest.slice(0, 12)}…</span>
              {Object.keys(v.confirmed.planning_assumptions).length > 0 &&
                ` · planning assumptions: ${JSON.stringify(v.confirmed.planning_assumptions)}`}
            </p>
          </>
        ) : (
          <Empty>No confirmed policy. Thresholds are unknown, so no change is assumed acceptable.</Empty>
        )}
      </Card>
      {canEdit ? <DraftForm merchant={merchant} onChange={r.reload} /> : <Notice tone="neutral">Your role can view the policy but not edit it.</Notice>}
      <Card title="Recent drafts (never active until confirmed)">
        {v.drafts.length === 0 ? (
          <Empty>No drafts.</Empty>
        ) : (
          <div className="space-y-3">
            {v.drafts.map((d) => (
              <DraftCard key={d.draft_id} draft={d} merchant={merchant} canEdit={canEdit} onChange={r.reload} />
            ))}
          </div>
        )}
      </Card>
    </>
  );
}

function DraftForm({ merchant, onChange }: { merchant: string; onChange: () => void }) {
  const [text, setText] = useState("");
  const [reserve, setReserve] = useState("");
  const [minDiscount, setMinDiscount] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const money_ = (amount: string) =>
    amount.trim() ? { amount: amount.trim(), currency: "$", quote: quoteFor(text, amount) ?? amount } : null;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    for (const a of [reserve, minDiscount]) {
      if (a.trim() && parseDollarsToCents(a) === null) {
        setError("Amounts must be dollars, for example 500 or 10.00.");
        return;
      }
    }
    setBusy(true);
    setError(null);
    try {
      await api(`/v1/m/${merchant}/policy/drafts`, {
        method: "POST",
        body: {
          text,
          extraction: {
            reserve_floor: money_(reserve),
            max_early_commitment: null,
            min_early_discount: money_(minDiscount),
            min_net_benefit: null,
            fee_ceiling: null,
            allowed_counterparties: [],
            prohibited_counterparties: [],
            policy_valid_until: null,
            offer_response_deadline: null,
            participation: "unspecified",
            approval_preference: "unspecified",
            ambiguities: [],
            refused_requests: [],
            summary: "Merchant-entered policy fields.",
          },
        },
      });
      onChange();
    } catch (err) {
      setError(explain(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="Draft a policy change">
      <form onSubmit={submit} className="space-y-3">
        <label className="block text-sm">
          Your rule, in your own words
          <textarea className="input mt-1" rows={2} maxLength={2000} value={text} onChange={(e) => setText(e.target.value)} placeholder="Keep $500 protected at all times. Only pay early for at least $10 off." required />
        </label>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="text-sm">
            Reserve floor (as written above)
            <input className="input mt-1" inputMode="decimal" value={reserve} onChange={(e) => setReserve(e.target.value)} placeholder="500" />
          </label>
          <label className="text-sm">
            Minimum early-payment discount (as written above)
            <input className="input mt-1" inputMode="decimal" value={minDiscount} onChange={(e) => setMinDiscount(e.target.value)} placeholder="10" />
          </label>
        </div>
        {error && (
          <p role="alert" className="text-sm text-rose-700">
            {error}
          </p>
        )}
        <button type="submit" className="btn-primary" disabled={busy}>
          Validate draft
        </button>
        <p className="text-xs text-slate-500">
          Every value must appear in your text. Deterministic validation checks grounding and
          refuses weakening your protected reserve silently. Nothing changes until you confirm.
        </p>
      </form>
    </Card>
  );
}

function DraftCard({ draft: d, merchant, canEdit, onChange }: { draft: PolicyDraft; merchant: string; canEdit: boolean; onChange: () => void }) {
  const { withStepUp } = useAuth();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const ready = d.status === "READY_FOR_CONFIRMATION" && d.digest !== null && !d.confirmed_revision;
  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      await withStepUp(() => api(`/v1/m/${merchant}/policy/drafts/${d.draft_id}/confirm`, { method: "POST", body: { digest: d.digest } }));
      onChange();
    } catch (err) {
      setError(explain(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <article aria-label={`Policy draft ${d.draft_id}`} className="rounded-md border border-slate-200 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm italic">“{d.text}”</p>
        {d.confirmed_revision ? (
          <Badge tone="good">confirmed as revision {d.confirmed_revision}</Badge>
        ) : (
          <Badge tone={ready ? "good" : "warn"}>{d.status.replaceAll("_", " ")}</Badge>
        )}
      </div>
      {d.proposed && (
        <div className="mt-2">
          <Rules rules={d.proposed} />
        </div>
      )}
      {d.report.issues.length > 0 && (
        <ul className="mt-2 list-disc pl-5 text-sm text-slate-700">
          {d.report.issues.map((i) => (
            <li key={i.code + i.field}>
              <span className="font-medium">{i.code}</span> ({i.field}): {i.message}
            </li>
          ))}
        </ul>
      )}
      {error && <ErrorBanner message={error} />}
      {ready && canEdit && (
        <button type="button" className="btn-primary mt-3" disabled={busy} onClick={() => void confirm()}>
          Confirm this policy
        </button>
      )}
    </article>
  );
}
