import { useState } from "react";
import { api, explain, newOperationKey } from "../api/client";
import { money } from "../api/format";
import type { PaymentIntent } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { Badge, StateLabel } from "../components/ui";

/** ADR-0012 §4.1: what each execution state means, in words a payer can act on. */
const EXECUTION_LABEL: Record<string, { state: string; detail: string }> = {
  approved: { state: "INTENT_CREATED_NOT_SENT", detail: "Approved; not yet sent to PayPal." },
  session_pending: { state: "PREPARING_PAYPAL_INVOICE", detail: "Elide is preparing the PayPal invoice." },
  payer_action_pending: {
    state: "AWAITING_PAYER_AT_PAYPAL",
    detail: "Waiting for the payer to pay in PayPal's own page. Elide cannot pay for you.",
  },
  provider_pending: { state: "PROVIDER_PENDING", detail: "PayPal reports the payment as pending." },
  provider_unknown: {
    state: "PROVIDER_OUTCOME_UNKNOWN",
    detail: "PayPal's answer was lost. Elide is checking PayPal before doing anything else.",
  },
  provider_confirmed: { state: "PAYPAL_CONFIRMED_NOT_BOOKED", detail: "PayPal confirmed; booking now." },
  reconciled: {
    state: "PAYPAL_CONFIRMED_RECONCILED",
    detail: "PayPal confirmed and the books reconcile. Not spendable funds for the payee (D006).",
  },
  failed: { state: "FAILED", detail: "PayPal did not accept a payment request; cash released." },
  expired: { state: "EXPIRED", detail: "Not paid in time; PayPal invoice cancelled; cash released." },
  cancelled: { state: "CANCELLED", detail: "Cancelled before payment; cash released." },
  exception: {
    state: "NEEDS_OPERATOR_REVIEW",
    detail: "Something did not match PayPal's records. Cash stays reserved until reviewed.",
  },
};

const CANCELLABLE = new Set(["approved", "session_pending", "payer_action_pending"]);

function heldReason(i: PaymentIntent): string | null {
  if (i.status !== "approved") return null;
  switch (i.status_reason) {
    case "SYNTHETIC_EVIDENCE_HELD":
      return "held: no PayPal call (synthetic data never executes)";
    case "EXECUTION_DISABLED":
      return "held: no PayPal call (payment execution is switched off)";
    case "PROVIDER_NOT_CONNECTED":
      return "held: no PayPal call (no PayPal Sandbox account connected)";
    case "QUEUED_FOR_PROVIDER":
      return "queued for PayPal";
    default:
      return i.execution === "disabled" ? "held: no PayPal call" : null;
  }
}

export function IntentRow({
  intent,
  merchant,
  canAuthorize,
  onChange,
}: {
  intent: PaymentIntent;
  merchant: string;
  canAuthorize: boolean;
  onChange: () => void;
}) {
  const { withStepUp } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const label = EXECUTION_LABEL[intent.status] ?? { state: intent.status.toUpperCase(), detail: "" };
  const held = heldReason(intent);
  const iPay = intent.payer_merchant === undefined || intent.payer_merchant === merchant;

  const openPayPal = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await withStepUp(() =>
        api<{ url: string }>(`/v1/m/${merchant}/payment-intents/${intent.intent_id}/payer-link`),
      );
      // PayPal's hosted page; the link is never stored by Elide.
      window.open(r.url, "_blank", "noopener,noreferrer");
    } catch (err) {
      setError(explain(err));
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    setBusy(true);
    setError(null);
    const key = newOperationKey();
    try {
      await withStepUp(() =>
        api(`/v1/m/${merchant}/payment-intents/${intent.intent_id}/cancel`, {
          method: "POST",
          body: {},
          operationKey: key,
        }),
      );
      setConfirmCancel(false);
      onChange();
    } catch (err) {
      setError(explain(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <tr>
      <td className="font-mono text-xs">{intent.intent_id}</td>
      <td className="font-mono text-xs">{intent.obligation_id}</td>
      <td>{intent.payee_merchant}</td>
      <td>{money(intent.amount_cents)}</td>
      <td>
        <StateLabel state={label.state} />
        <p className="mt-1 text-xs text-slate-600">{label.detail}</p>
        {error && (
          <p role="alert" className="mt-1 text-xs text-red-700">
            {error}
          </p>
        )}
      </td>
      <td>
        <span className="flex flex-col items-start gap-1">
          {held && <Badge tone="warn">{held}</Badge>}
          <Badge tone={intent.provider_confirmed ? "good" : "neutral"}>
            {intent.provider_confirmed ? "PayPal-confirmed" : "no money moved"}
          </Badge>
          <Badge tone="warn">funds usable: no</Badge>
          {iPay && canAuthorize && intent.status === "payer_action_pending" && (
            <button type="button" className="btn-primary" disabled={busy} onClick={openPayPal}>
              Open PayPal payment page
            </button>
          )}
          {iPay && canAuthorize && CANCELLABLE.has(intent.status) && !confirmCancel && (
            <button type="button" className="btn-secondary" disabled={busy} onClick={() => setConfirmCancel(true)}>
              Cancel payment
            </button>
          )}
          {confirmCancel && (
            <span role="group" aria-label="Confirm cancellation" className="flex gap-1">
              <button type="button" className="btn-danger" disabled={busy} onClick={cancel}>
                Confirm cancel
              </button>
              <button type="button" className="btn-secondary" disabled={busy} onClick={() => setConfirmCancel(false)}>
                Keep
              </button>
            </span>
          )}
        </span>
      </td>
    </tr>
  );
}
