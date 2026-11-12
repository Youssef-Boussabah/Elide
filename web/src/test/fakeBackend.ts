// A scripted fake of the workspace API for component/workflow tests. It records every
// request (method, path, headers, body) so tests can assert CSRF and idempotency use.

export type Call = { method: string; path: string; headers: Record<string, string>; body: unknown };
type Handler = (call: Call) => { status: number; body: unknown } | undefined;

export function installFakeBackend(handler: Handler): Call[] {
  const calls: Call[] = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input).replace(/^\/api/, "");
    const call: Call = {
      method: init?.method ?? "GET",
      path: url,
      headers: (init?.headers ?? {}) as Record<string, string>,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    };
    calls.push(call);
    const res = handler(call) ?? { status: 404, body: { detail: { code: "NOT_FOUND", message: "not found" } } };
    return {
      ok: res.status >= 200 && res.status < 300,
      status: res.status,
      json: async () => res.body,
    } as Response;
  }) as typeof fetch;
  return calls;
}

export const SESSION = {
  user: {
    user_id: "usr_1",
    login: "buyer-owner@elide.test",
    display_name: "Buyer owner (TEST)",
    is_test_identity: true,
    identity_label: "TEST IDENTITY",
  },
  session: { session_id: "ses_1", step_up_valid: true, absolute_expires_at: "2026-11-12T02:00:00Z" },
  csrf_token: "csrf-abc",
  memberships: [
    {
      merchant_id: "buyer",
      display_name: "Northside Caterer (synthetic)",
      is_synthetic: true,
      role: "owner",
      permissions: ["workspace:read", "offer:propose", "offer:decide", "payment:authorize", "policy:edit", "audit:read", "members:manage"],
    },
  ],
};
