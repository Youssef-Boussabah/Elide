import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import App from "../App";
import { SESSION, installFakeBackend } from "../test/fakeBackend";
import type { Call } from "../test/fakeBackend";

const OVERVIEW = {
  merchant: { merchant_id: "buyer", display_name: "Northside Caterer (synthetic)", is_synthetic: true, my_role: "owner", my_permissions: [] },
  network_id: "n",
  as_of: "2026-11-11T18:00:00Z",
  labels: ["SYNTHETIC_NETWORK"],
  capacity: {
    basis: "SYNTHETIC",
    status: "AVAILABLE",
    observed_at: "2026-11-11T17:40:00Z",
    valid_until: "2026-11-18T17:40:00Z",
    verified_spendable_cents: 149000,
    protected_reserve_cents: 50000,
    committed_cents: 0,
    held_cents: 0,
    pending_cents: 0,
    deployable_cents: 99000,
    explanation: "deployable = max(0, verified spendable - protected reserve - committed)",
  },
  invoices: [],
  offers_awaiting_me: 1,
  policy: { confirmed_revision: 1 },
};

const OFFER = {
  offer_id: "off_1",
  obligation_id: "food_001",
  status: "proposed",
  my_role: "debtor",
  proposer: "food_supplier",
  counterparty: "buyer",
  debtor: "buyer",
  creditor: "food_supplier",
  face_cents: 100000,
  cash_cents: 99500,
  concession_cents: 500,
  currency: "USD",
  pay_by: "2026-11-14T17:30:00Z",
  expires_at: "2026-11-14T17:30:00Z",
  terms_digest: "a".repeat(64),
  parent_offer_id: null,
  message: null,
  created_at: "2026-11-11T17:35:00Z",
  decisions: [{ merchant_id: "food_supplier", decision: "accept", decided_at: "2026-11-11T17:35:00Z" }],
  my_decision: null,
  can_decide: true,
  can_withdraw: false,
  amendment_authoritative: false,
};

function leg(termsAgreed: boolean, status = "pending", planCurrent = true) {
  return {
    leg_id: termsAgreed ? "food_001:r4" : "food_001:r3:proposal",
    obligation_id: "food_001",
    revision_no: termsAgreed ? 4 : 3,
    terms: termsAgreed ? "in_force" : "negotiated_proposal",
    payer: "buyer",
    payee: "food_supplier",
    slot_start: "2026-11-13T18:00:00Z",
    amount_cents: 99000,
    concession_cents: 1000,
    payer_fee_cents: 0,
    on_time: true,
    depends_on: [],
    has_upstream_dependency: false,
    payment_authorization:
      status === "needs_reapproval"
        ? { status, reason: "the conditions this decision was given under have changed" }
        : { status },
    authorize: {
      binding_digest: "b".repeat(64),
      fee_ceiling_cents: 0,
      expires_at: "2026-11-12T18:00:00Z",
      terms_agreed: termsAgreed,
      plan_current: planCurrent,
    },
  };
}

function planView(termsAgreed: boolean, status = "pending", planCurrent = true) {
  return {
    plan_id: "plan_1",
    status: "SIMULATED",
    solver_status: "OPTIMAL",
    as_of: "2026-11-11T18:00:00Z",
    expires_at: null,
    durable: true,
    my_legs: [leg(termsAgreed, status, planCurrent)],
    my_benefit: {},
    aggregates: { status: "SHOWN", explanation: "" },
    execution: { may_execute: false, reason: "SIMULATED" },
  };
}

function opportunities(termsAgreed: boolean, withheld = false) {
  return {
    as_of: "2026-11-11T18:00:00Z",
    network_id: "n",
    labels: ["SYNTHETIC_NETWORK"],
    opportunity: {
      status: "SIMULATED",
      viewer: "buyer",
      recommendation: { action: "NEGOTIATE", reason: "a bounded discount counteroffer could unlock an invoice" },
      my_actions: [],
      my_bottlenecks: [],
      my_benefit: {},
      my_negotiation_hints: [],
      my_flags: [],
      conditions: [],
      aggregates: withheld
        ? { status: "WITHHELD_NO_CONSENT", explanation: "network figures are withheld: not every participant has agreed" }
        : { status: "SHOWN", explanation: "" },
      ...(withheld
        ? { decision: { my_part_in_plan: true, my_net_benefit_cents: 370, my_payments_to_approve: 1 } }
        : {
            comparison: {
              incremental_vs_B0: "170000",
              incremental_vs_B1: "0",
              incremental_vs_B3: "0",
              gross_flow_cents: "269000",
            },
          }),
    },
    plan_preview: { ...planView(termsAgreed), durable: false },
    explanation: { status: "TEMPLATE", text: "", facts: [{ id: "F1", text: "This analysis is a SIMULATION." }] },
  };
}

type Opts = {
  signedIn?: boolean;
  stepUpFirst?: boolean;
  termsAgreed?: boolean;
  role?: string;
  authStatus?: string;
  planCurrent?: boolean;
  hasPlan?: boolean;
  withheld?: boolean;
};

function backend(opts: Opts = {}) {
  let signedIn = opts.signedIn ?? true;
  let stepUpNeeded = opts.stepUpFirst ?? false;
  let hasPlan = opts.hasPlan ?? true;
  const session = {
    ...SESSION,
    memberships: SESSION.memberships.map((m) =>
      opts.role === "viewer" ? { ...m, role: "viewer", permissions: ["workspace:read"] } : m,
    ),
  };
  return installFakeBackend((c: Call) => {
    if (c.path === "/v1/auth/session") return signedIn ? { status: 200, body: session } : { status: 401, body: { detail: { code: "UNAUTHENTICATED", message: "" } } };
    if (c.path === "/v1/auth/login") {
      signedIn = true;
      return { status: 200, body: session };
    }
    if (c.path === "/v1/auth/reauth") {
      stepUpNeeded = false;
      return { status: 200, body: { step_up_valid: true } };
    }
    if (c.path === "/v1/m/buyer/overview") return { status: 200, body: OVERVIEW };
    if (c.path === "/v1/m/buyer/offers") return { status: 200, body: { offers: [OFFER] } };
    if (c.path === "/v1/m/buyer/offers/off_1/decision") {
      if (stepUpNeeded) return { status: 401, body: { detail: { code: "REAUTH_REQUIRED", message: "confirm" } } };
      return { status: 200, body: { status: "agreed" } };
    }
    if (c.path === "/v1/m/buyer/opportunities")
      return { status: 200, body: opportunities(opts.termsAgreed ?? true, opts.withheld ?? false) };
    if (c.path === "/v1/m/buyer/plans" && c.method === "POST") {
      const reused = hasPlan;
      hasPlan = true;
      return { status: 200, body: { plan: planView(opts.termsAgreed ?? true), reused, replayed: false } };
    }
    if (c.path === "/v1/m/buyer/plans")
      return {
        status: 200,
        body: { plans: hasPlan ? [{ plan_id: "plan_1", created_at: "", as_of: "", solver_status: "OPTIMAL" }] : [] },
      };
    if (c.path === "/v1/m/buyer/plans/plan_1")
      return {
        status: 200,
        body: planView(opts.termsAgreed ?? true, opts.authStatus ?? "pending", opts.planCurrent ?? true),
      };
    if (c.path === "/v1/m/buyer/payment-intents") return { status: 200, body: { intents: [] } };
    return undefined;
  });
}

beforeEach(() => {
  window.location.hash = "#/m/buyer/overview";
});
afterEach(() => cleanup());

describe("merchant workspace", () => {
  it("signs in and shows the labelled test identity and evidence-stamped liquidity", async () => {
    const calls = backend({ signedIn: false });
    render(<App />);
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Email"), "buyer-owner@elide.test");
    await user.type(screen.getByLabelText("Password"), "a-long-test-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("TEST IDENTITY")).toBeTruthy();
    const liquidity = await screen.findByRole("region", { name: "Liquidity" });
    expect(within(liquidity).getByText("USD 990.00")).toBeTruthy();
    expect(within(liquidity).getByText("SYNTHETIC")).toBeTruthy();
    expect(screen.getByText(/Synthetic network/)).toBeTruthy();
    const login = calls.find((c) => c.path === "/v1/auth/login");
    expect(login?.body).toEqual({ login: "buyer-owner@elide.test", password: "a-long-test-password" });
  });

  it("asks for the password before a decision and retries with the same operation key", async () => {
    window.location.hash = "#/m/buyer/negotiations";
    const calls = backend({ stepUpFirst: true });
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Accept these terms" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm it is you" });
    await user.type(within(dialog).getByLabelText("Password"), "a-long-test-password");
    await user.click(within(dialog).getByRole("button", { name: "Confirm" }));
    expect(await screen.findByText("Recorded: agreed.")).toBeTruthy();
    const decisions = calls.filter((c) => c.path === "/v1/m/buyer/offers/off_1/decision");
    expect(decisions).toHaveLength(2);
    const [first, second] = decisions;
    expect(first?.headers["Idempotency-Key"]).toBeTruthy();
    expect(second?.headers["Idempotency-Key"]).toBe(first?.headers["Idempotency-Key"]);
    expect(second?.headers["X-CSRF-Token"]).toBe("csrf-abc");
    expect(second?.body).toEqual({ decision: "accept", terms_digest: "a".repeat(64) });
  });

  it("states exact amount, payee and expiry before a payment authorization", async () => {
    window.location.hash = "#/m/buyer/plan";
    backend({ termsAgreed: true });
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Approve my payment…" }));
    const text = (await screen.findByText(/Authorize paying exactly/)).textContent ?? "";
    expect(text).toContain("USD 990.00");
    expect(text).toContain("food_supplier");
    expect(screen.getByText(/no PayPal payment is created or sent/)).toBeTruthy();
  });

  it("offers no payment button for terms that are not yet agreed", async () => {
    window.location.hash = "#/m/buyer/plan";
    backend({ termsAgreed: false });
    render(<App />);
    expect(await screen.findByText(/relies on terms that are not yet an agreed amendment/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Approve my payment…" })).toBeNull();
  });

  it("a viewer can read offers but gets no decision controls", async () => {
    window.location.hash = "#/m/buyer/negotiations";
    backend({ role: "viewer" });
    render(<App />);
    expect(await screen.findByText(/Your role can view but not decide offers/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Accept these terms" })).toBeNull();
  });

  it("shows a retryable error when the API is unreachable", async () => {
    installFakeBackend((c) => (c.path === "/v1/auth/session" ? { status: 200, body: SESSION } : { status: 503, body: null }));
    render(<App />);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("HTTP 503");
    await waitFor(() => expect(within(alert).getByRole("button", { name: "Retry" })).toBeTruthy());
  });

  it("never writes on viewing opportunities and creates plans only on an explicit POST", async () => {
    window.location.hash = "#/m/buyer/plan";
    const calls = backend({ hasPlan: false });
    render(<App />);
    const user = userEvent.setup();
    expect(await screen.findByText("No durable plan yet.")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Create plan for approval" }));
    expect(await screen.findByText(/new plan was computed, independently verified/)).toBeTruthy();
    const posts = calls.filter((c) => c.method === "POST" && c.path === "/v1/m/buyer/plans");
    expect(posts).toHaveLength(1);
    expect(posts[0]?.headers["Idempotency-Key"]).toBeTruthy();
    expect(posts[0]?.headers["X-CSRF-Token"]).toBe("csrf-abc");
    const reads = calls.filter((c) => c.path === "/v1/m/buyer/opportunities");
    expect(reads.every((c) => c.method === "GET")).toBe(true);
  });

  it.each([
    ["expired", "APPROVAL EXPIRED", /approval expired and is no longer valid/],
    ["needs_reapproval", "CONDITIONS CHANGED: RE-APPROVE", /conditions this decision was given under have changed/],
  ])("shows an %s approval as not active", async (status, label, text) => {
    window.location.hash = "#/m/buyer/plan";
    backend({ authStatus: status });
    render(<App />);
    expect(await screen.findByText(label)).toBeTruthy();
    expect(screen.getByText(text)).toBeTruthy();
    expect(screen.queryByText("APPROVED")).toBeNull();
    expect(screen.queryByRole("button", { name: "Prepare payment intent" })).toBeNull();
  });

  it("offers no decision on a plan whose economic inputs changed", async () => {
    window.location.hash = "#/m/buyer/plan";
    backend({ planCurrent: false });
    render(<App />);
    expect(await screen.findByText(/changed since this plan was computed/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Approve my payment…" })).toBeNull();
  });

  it("shows only the merchant's own figures when aggregates are withheld", async () => {
    window.location.hash = "#/m/buyer/opportunities";
    backend({ withheld: true });
    render(<App />);
    expect(await screen.findByText(/network figures are withheld/)).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Matched-baseline comparison" })).toBeNull();
    expect(screen.getByText("Your payments to approve")).toBeTruthy();
    expect(document.body.textContent).not.toContain("1,700");
  });
});
