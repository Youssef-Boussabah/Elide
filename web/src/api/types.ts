// Response shapes of the P4 workspace API (only the fields the UI uses).

export type Membership = {
  merchant_id: string;
  display_name: string;
  is_synthetic: boolean;
  role: string;
  permissions: string[];
};

export type SessionBody = {
  user: {
    user_id: string;
    login: string;
    display_name: string;
    is_test_identity: boolean;
    identity_label: string | null;
  };
  session: { session_id: string; step_up_valid: boolean; absolute_expires_at: string };
  csrf_token: string;
  memberships: Membership[];
};

export type Capacity = {
  basis: string;
  status: string;
  observed_at?: string;
  valid_until?: string;
  verified_spendable_cents?: number;
  protected_reserve_cents?: number;
  committed_cents?: number;
  held_cents?: number;
  pending_cents?: number;
  deployable_cents: number;
  explanation: string;
};

export type Invoice = {
  obligation_id: string;
  direction: "payable" | "receivable";
  counterparty: string;
  counterparty_name?: string | null;
  face_cents: number;
  open_cents: number;
  concession_cents: number;
  currency: string;
  due_at: string;
  revision_no: number;
  settlement_status: string;
  open_offer_id: string | null;
  provenance: string;
  payment_state: string;
};

export type Overview = {
  merchant: {
    merchant_id: string;
    display_name: string;
    is_synthetic: boolean;
    my_role: string;
    my_permissions: string[];
  };
  network_id: string;
  as_of: string;
  labels: string[];
  capacity: Capacity;
  invoices: Invoice[];
  offers_awaiting_me: number;
  policy: { confirmed_revision: number | null };
};

export type Flag = { code: string; merchant_id: string | null; detail: string };

export type Bottleneck = {
  obligation_id: string;
  debtor: string;
  creditor: string;
  face_cents: number;
  open_cents: number;
  due_at: string;
  reason_code: string;
  reason: string;
  unlocked_by_plan: boolean;
};

export type Hint = {
  kind: string;
  obligation_id: string;
  payer: string;
  current_offer_cents?: number;
  minimum_needed_cents?: number;
  detail?: string;
};

export type OpportunityView = {
  status: string;
  viewer: string;
  recommendation: { action: string; reason: string };
  my_actions: Array<{
    action: "pay" | "receive";
    obligation_id: string;
    payer: string;
    payee: string;
    amount_cents: number;
    slot_start: string;
    on_time: boolean;
  }>;
  my_bottlenecks: Bottleneck[];
  my_benefit: Record<string, number>;
  my_negotiation_hints: Hint[];
  my_flags: Flag[];
  conditions: string[];
  // Network-level decision (only when aggregates are disclosed) or the merchant's own.
  decision?: {
    feasible?: boolean;
    advantage?: Record<string, unknown>;
    buyer_net_benefit_cents?: number;
    participants_with_undeclared_losses?: number;
    consent?: { approvals_required: number; approvals_recorded: number };
    my_part_in_plan?: boolean;
    my_net_benefit_cents?: number | null;
    my_payments_to_approve?: number;
  };
  comparison?: Record<string, string>;
  aggregates?: { status: string; explanation?: string };
};

export type PlanLeg = {
  leg_id: string;
  obligation_id: string;
  revision_no: number;
  terms: string;
  payer: string;
  payee: string;
  slot_start: string;
  amount_cents: number;
  concession_cents: number;
  payer_fee_cents?: number;
  on_time: boolean;
  depends_on: string[];
  has_upstream_dependency?: boolean;
  payment_authorization?: {
    status: "pending" | "approved" | "declined" | "expired" | "needs_reapproval" | string;
    authorization_id?: string | null;
    expires_at?: string;
    reason?: string;
  };
  authorize?: {
    binding_digest: string;
    fee_ceiling_cents: number;
    expires_at: string;
    terms_agreed: boolean;
    plan_current: boolean;
  };
};

export type PlanView = {
  plan_id: string;
  status: string;
  solver_status: string;
  as_of: string;
  expires_at: string | null;
  durable: boolean;
  my_legs: PlanLeg[];
  my_benefit: Record<string, number>;
  other_legs_in_plan?: number;
  aggregates: { status: string; explanation: string };
  execution: { may_execute: boolean; reason: string };
};

export type PlanSummary = { plan_id: string; created_at: string; as_of: string; solver_status: string };

export type Opportunities = {
  as_of: string;
  network_id: string;
  labels: string[];
  opportunity: OpportunityView;
  plan_preview: PlanView;
  explanation: { status: string; text: string; facts: Array<{ id: string; text: string }> };
};

export type Offer = {
  offer_id: string;
  obligation_id: string;
  status: string;
  my_role: "debtor" | "creditor";
  proposer: string;
  counterparty: string;
  debtor: string;
  creditor: string;
  face_cents: number;
  cash_cents: number;
  concession_cents: number;
  currency: string;
  pay_by: string;
  expires_at: string;
  terms_digest: string;
  parent_offer_id: string | null;
  message: string | null;
  created_at: string;
  decisions: Array<{ merchant_id: string; decision: string; decided_at: string }>;
  my_decision: string | null;
  can_decide: boolean;
  can_withdraw: boolean;
  amendment_authoritative: boolean;
};

export type NetworkView = {
  network_id: string;
  scope: string;
  nodes: Array<{ merchant_id: string; display_name: string | null; is_me: boolean }>;
  edges: Array<{
    obligation_id: string;
    from: string;
    to: string;
    open_cents: number;
    due_at: string;
    terms_state: string;
    payment_state: string;
    funds_state: string;
  }>;
  legend: string[];
};

export type PolicyRules = {
  reserve_floor_cents: number | null;
  max_early_commitment_cents: number | null;
  min_early_discount_cents: number | null;
  min_net_benefit_cents: number | null;
  fee_ceiling_cents: number | null;
  allowed: string[];
  prohibited: string[];
  participation: string;
};

export type PolicyDraft = {
  draft_id: string;
  created_at: string;
  text: string;
  status: string;
  report: {
    status: string;
    issues: Array<{ code: string; severity: string; field: string; message: string }>;
    explanation?: string;
  };
  proposed: PolicyRules | null;
  digest: string | null;
  confirmed_revision: number | null;
  active: false;
};

export type PolicyView = {
  confirmed: null | {
    revision_no: number;
    digest: string;
    confirmed_at: string;
    rules: PolicyRules;
    planning_assumptions: Record<string, unknown>;
  };
  drafts: PolicyDraft[];
};

export type AuditEvent = {
  id: number;
  occurred_at: string;
  actor_user_id: string | null;
  action: string;
  object_type: string;
  object_id: string;
  outcome: string;
  detail: Record<string, unknown>;
  event_sha256: string;
};

/** GET /settlements: each merchant sees only its own side of a reconciled payment. */
export type Settlement = {
  role: "payer" | "payee";
  intent_id: string;
  capture_id: string;
  currency?: string;
  paid_cents?: number;
  gross_cents?: number;
  fee_cents?: number;
  net_cents?: number;
  credited_cents?: number;
  credited_currency?: string;
  receipt_status?: string;
  usd_deployable_cents?: number;
  reasons?: string[];
};

export type PaymentIntent = {
  intent_id: string;
  obligation_id: string;
  payer_merchant?: string;
  payee_merchant: string;
  amount_cents: number;
  status: string;
  status_reason?: string | null;
  created_at: string;
  execution: string;
  money_moved: boolean;
  provider_confirmed?: boolean;
  funds_usable?: boolean;
};
