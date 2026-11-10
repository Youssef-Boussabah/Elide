// Typed client for the backend health endpoints. Never handles credentials.

export type Readiness = {
  status: "ok" | "unavailable";
  database: "ok" | "unreachable";
  postgres_version?: string;
  paypal_sandbox_configured: boolean;
  llm_configured: boolean;
};

export type HealthView =
  | { kind: "ok"; readiness: Readiness }
  | { kind: "degraded"; readiness: Readiness }
  | { kind: "api-unreachable"; message: string };

export function interpretReadiness(httpStatus: number, body: unknown): HealthView {
  if (!isReadiness(body)) {
    return { kind: "api-unreachable", message: `Unexpected response (HTTP ${httpStatus})` };
  }
  return httpStatus === 200 && body.status === "ok"
    ? { kind: "ok", readiness: body }
    : { kind: "degraded", readiness: body };
}

function isReadiness(v: unknown): v is Readiness {
  if (typeof v !== "object" || v === null) return false;
  const r = v as Record<string, unknown>;
  return (
    (r.status === "ok" || r.status === "unavailable") &&
    (r.database === "ok" || r.database === "unreachable") &&
    typeof r.paypal_sandbox_configured === "boolean" &&
    typeof r.llm_configured === "boolean"
  );
}

export async function fetchHealth(signal?: AbortSignal): Promise<HealthView> {
  try {
    const resp = await fetch("/api/health/ready", { signal });
    return interpretReadiness(resp.status, await resp.json().catch(() => null));
  } catch (err) {
    return { kind: "api-unreachable", message: err instanceof Error ? err.message : String(err) };
  }
}
