// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PaymentIntent } from "../api/types";
import { installFakeBackend } from "../test/fakeBackend";
import { IntentRow, SettlementsCard } from "./Execution";

vi.mock("../auth/AuthContext", () => ({
  useAuth: () => ({ withStepUp: <T,>(fn: () => Promise<T>) => fn() }),
}));

afterEach(cleanup);

function intent(over: Partial<PaymentIntent>): PaymentIntent {
  return {
    intent_id: "pin_1",
    obligation_id: "pilot_001",
    payer_merchant: "pilot_buyer",
    payee_merchant: "pilot_seller",
    amount_cents: 2400,
    status: "approved",
    status_reason: "QUEUED_FOR_PROVIDER",
    created_at: "2026-11-11T18:00:00Z",
    execution: "enabled",
    money_moved: false,
    provider_confirmed: false,
    funds_usable: false,
    ...over,
  };
}

function row(i: PaymentIntent, merchant = "pilot_buyer", onChange = () => {}) {
  render(
    <table>
      <tbody>
        <IntentRow intent={i} merchant={merchant} canAuthorize onChange={onChange} />
      </tbody>
    </table>,
  );
}

describe("execution states (ADR-0012)", () => {
  it("explains why an approved intent is held and offers no PayPal action", () => {
    row(intent({ status_reason: "PROVIDER_NOT_CONNECTED", execution: "disabled" }));
    expect(screen.getByText("INTENT CREATED NOT SENT")).toBeTruthy();
    expect(screen.getByText("held: no PayPal call (no PayPal Sandbox account connected)")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Open PayPal payment page" })).toBeNull();
  });

  it("opens PayPal's hosted page fetched on demand, with noopener", async () => {
    const calls = installFakeBackend((c) =>
      c.path.endsWith("/payer-link")
        ? { status: 200, body: { url: "https://www.sandbox.paypal.com/invoice/p/#X" } }
        : undefined,
    );
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    row(intent({ status: "payer_action_pending" }));
    expect(screen.getByText("AWAITING PAYER AT PAYPAL")).toBeTruthy();
    expect(screen.getByText(/Elide cannot pay for you/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Open PayPal payment page" }));
    await waitFor(() => expect(open).toHaveBeenCalled());
    expect(open).toHaveBeenCalledWith(
      "https://www.sandbox.paypal.com/invoice/p/#X",
      "_blank",
      "noopener,noreferrer",
    );
    expect(calls[0]?.method).toBe("GET");
  });

  it("never calls a reconciled payment spendable", () => {
    row(intent({ status: "reconciled", provider_confirmed: true, money_moved: true }));
    expect(screen.getByText("PAYPAL CONFIRMED RECONCILED")).toBeTruthy();
    expect(screen.getByText("PayPal-confirmed")).toBeTruthy();
    expect(screen.getByText("funds usable: no")).toBeTruthy();
    expect(screen.getByText(/Not spendable funds for the payee/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Cancel payment" })).toBeNull();
  });

  it("keeps cash reserved while the provider outcome is unknown or under review", () => {
    row(intent({ status: "provider_unknown" }));
    expect(screen.getByText("PROVIDER OUTCOME UNKNOWN")).toBeTruthy();
    expect(screen.getByText(/checking PayPal before doing anything else/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Cancel payment" })).toBeNull();
  });

  it("offers the payee no payer actions", () => {
    row(intent({ status: "payer_action_pending" }), "pilot_seller");
    expect(screen.queryByRole("button", { name: "Open PayPal payment page" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Cancel payment" })).toBeNull();
  });

  it("cancels only after explicit confirmation, with an idempotency key", async () => {
    const calls = installFakeBackend((c) =>
      c.path.endsWith("/cancel") ? { status: 200, body: { status: "cancelled" } } : undefined,
    );
    const changed = vi.fn();
    row(intent({}), "pilot_buyer", changed);
    fireEvent.click(screen.getByRole("button", { name: "Cancel payment" }));
    expect(calls).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Confirm cancel" }));
    await waitFor(() => expect(changed).toHaveBeenCalled());
    expect(calls[0]?.method).toBe("POST");
    expect(calls[0]?.headers["Idempotency-Key"]).toMatch(/^op-/);
  });
});

describe("settlements (ADR-0012 §9)", () => {
  it("shows the payee its receipt as provider-confirmed and not spendable", async () => {
    installFakeBackend(() => ({
      status: 200,
      body: {
        settlements: [
          {
            role: "payee", intent_id: "pin_1", capture_id: "C1", gross_cents: 2400, fee_cents: 133,
            net_cents: 2267, credited_cents: 2267, credited_currency: "USD",
            receipt_status: "PROVIDER_CONFIRMED_NOT_SPENDABLE", usd_deployable_cents: 0,
          },
        ],
      },
    }));
    render(<SettlementsCard merchant="pilot_seller" />);
    await waitFor(() => expect(screen.getByText("PROVIDER CONFIRMED NOT SPENDABLE")).toBeTruthy());
    expect(screen.getByText(/PayPal fee USD 1.33/)).toBeTruthy();
    expect(screen.getByText("usable USD: USD 0.00")).toBeTruthy();
  });

  const verification = {
    status: "PASSED",
    attestations: [
      {
        purpose: "booking",
        verdict: "passed",
        checker_version: "elide-reconciliation-check/2",
        document_sha256: "ab".repeat(32),
        verified_at: "2026-11-12T18:26:13Z",
      },
    ],
    legacy_check_sha256_placeholder: false,
  };
  const payer = {
    role: "payer",
    intent_id: "pin_1",
    capture_id: "C1",
    settled_cents: 2400,
    currency: "USD",
    obligation_settlement: "PROVIDER_CONFIRMED",
    payer_authorization: "MERCHANT_APPROVED",
    payment_source: "NOT_VERIFIED",
    verification,
  };

  it("shows the payer a settled obligation without claiming which account paid", async () => {
    installFakeBackend(() => ({ status: 200, body: { settlements: [payer] } }));
    render(<SettlementsCard merchant="pilot_buyer" />);
    await waitFor(() => expect(screen.getByText("USD 24.00 received by the payee via PayPal")).toBeTruthy());
    expect(screen.getByText("obligation settled (PayPal-confirmed)")).toBeTruthy();
    expect(screen.getByText(/paying account: not verified/)).toBeTruthy();
    expect(screen.getByText("independent check: passed")).toBeTruthy();
    expect(screen.queryByText(/paid via PayPal/)).toBeNull();
    expect(screen.queryByText(/PayPal fee/)).toBeNull();
  });

  it("records the payer's attestation only after an explicit confirmation", async () => {
    const calls = installFakeBackend((c) =>
      c.path.endsWith("/payment-source")
        ? { status: 200, body: { payment_source: "MERCHANT_ATTESTED_THIRD_PARTY" } }
        : { status: 200, body: { settlements: [payer] } },
    );
    render(<SettlementsCard merchant="pilot_buyer" />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Someone else paid" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Someone else paid" }));
    expect(calls.some((c) => c.path.endsWith("/payment-source"))).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Confirm attestation" }));
    await waitFor(() => expect(calls.some((c) => c.path.endsWith("/payment-source"))).toBe(true));
    const post = calls.find((c) => c.path.endsWith("/payment-source"));
    expect(post?.method).toBe("POST");
    expect(post?.body).toEqual({ source: "third_party" });
    expect(post?.headers["Idempotency-Key"]).toMatch(/^op-/);
  });
});

describe("held intent release (owner-approved fix)", () => {
  it("offers release only when the server says it is available, and confirms first", async () => {
    const calls = installFakeBackend((c) =>
      c.path.endsWith("/release") ? { status: 200, body: { status: "approved", dispatch: "queued" } } : undefined,
    );
    const changed = vi.fn();
    row(intent({ status_reason: "EXECUTION_DISABLED", release_available: true }), "pilot_buyer", changed);
    fireEvent.click(screen.getByRole("button", { name: "Allow PayPal dispatch" }));
    expect(calls).toHaveLength(0);
    expect(screen.getByText(/one PayPal invoice for USD 24.00 to pilot_seller/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Confirm dispatch" }));
    await waitFor(() => expect(changed).toHaveBeenCalled());
    expect(calls[0]?.method).toBe("POST");
    expect(calls[0]?.path).toMatch(/payment-intents\/pin_1\/release$/);
    expect(calls[0]?.headers["Idempotency-Key"]).toMatch(/^op-/);
  });

  it("shows no release when it is not available or to the payee", () => {
    row(intent({ status_reason: "EXECUTION_DISABLED", release_available: false }));
    expect(screen.queryByRole("button", { name: "Allow PayPal dispatch" })).toBeNull();
    cleanup();
    row(intent({ status_reason: "EXECUTION_DISABLED", release_available: true }), "pilot_seller");
    expect(screen.queryByRole("button", { name: "Allow PayPal dispatch" })).toBeNull();
  });
});
