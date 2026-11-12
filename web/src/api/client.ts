// Typed HTTP client for the authenticated workspace API (via the Vite /api proxy).
// The session lives in an HttpOnly cookie the page cannot read; the CSRF token is kept
// in memory only and sent on state-changing requests. No credentials are stored here.

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

let csrfToken: string | null = null;

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

export function newOperationKey(): string {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === "function") return `op-${c.randomUUID()}`;
  return `op-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

type Options = { method?: "GET" | "POST"; body?: unknown; operationKey?: string };

export async function api<T>(path: string, opts: Options = {}): Promise<T> {
  const method = opts.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json" };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  if (opts.operationKey) headers["Idempotency-Key"] = opts.operationKey;
  let resp: Response;
  try {
    resp = await fetch(`/api${path}`, {
      method,
      headers,
      credentials: "same-origin",
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
    });
  } catch (err) {
    throw new ApiError(0, "NETWORK", err instanceof Error ? err.message : "network error");
  }
  const data: unknown = await resp.json().catch(() => null);
  if (!resp.ok) throw toError(resp.status, data);
  return data as T;
}

export function toError(status: number, data: unknown): ApiError {
  const detail = (data as { detail?: unknown } | null)?.detail;
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const d = detail as { code?: unknown; message?: unknown };
    return new ApiError(status, String(d.code ?? "ERROR"), String(d.message ?? "Request failed"));
  }
  if (Array.isArray(detail)) {
    const first = detail[0] as { msg?: unknown; loc?: unknown[] } | undefined;
    const where = first?.loc?.slice(1).join(".") ?? "request";
    return new ApiError(status, "VALIDATION", `${where}: ${String(first?.msg ?? "invalid")}`);
  }
  return new ApiError(status, "HTTP_" + status, `Request failed (HTTP ${status})`);
}

// Human-readable explanations for the error codes a merchant can act on.
const HINTS: Record<string, string> = {
  STALE_DIGEST: "These terms changed since you reviewed them. Reload and review again.",
  OFFER_EXPIRED: "This offer has expired. No decision can be recorded.",
  OFFER_CLOSED: "This offer was already decided.",
  STALE_REVISION: "The invoice terms changed. This offer no longer applies.",
  ALREADY_DECIDED: "Your merchant already recorded a decision here.",
  NOT_COUNTERPARTY: "Only the other party can accept, reject or counter your own offer.",
  OPEN_OFFER_EXISTS: "An offer is already open on this invoice. Respond to it instead.",
  TERMS_NOT_AGREED: "Agree the amended terms in Negotiations first, then re-plan.",
  UPSTREAM_NOT_CONFIRMED:
    "This payment depends on incoming money that is not confirmed and spendable yet.",
  INSUFFICIENT_CAPACITY: "Not enough verified deployable cash after your protected reserve.",
  AUTHORIZATION_REVOKED: "The approving user no longer holds payment authority.",
  RATE_LIMITED: "Too many attempts. Wait a few minutes and try again.",
  CONDITIONS_CHANGED:
    "The conditions of this approval changed (policy, reserve, terms, fees or evidence). Approve again on a current plan.",
  VERIFIER_REJECTED: "The independent verifier rejected this payment under current data.",
  STALE_PLAN: "Terms, evidence or policies changed since this plan. Re-check and update the plan.",
  AUTHORIZATION_EXPIRED: "This approval expired. Approve again on a current plan.",
  ALREADY_IN_EXECUTION: "This invoice already has a payment in execution.",
  SYNTHETIC_EVIDENCE: "Synthetic evidence can never authorize a payment here.",
  UNAUTHENTICATED: "Your session ended. Sign in again.",
};

export function explain(err: unknown): string {
  if (err instanceof ApiError) return HINTS[err.code] ?? err.message;
  return err instanceof Error ? err.message : String(err);
}
