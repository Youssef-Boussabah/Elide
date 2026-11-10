import { describe, expect, it } from "vitest";
import { interpretReadiness } from "./health";

const base = { paypal_sandbox_configured: false, llm_configured: false };

describe("interpretReadiness", () => {
  it("reports ok only for HTTP 200 with status ok", () => {
    const v = interpretReadiness(200, { ...base, status: "ok", database: "ok" });
    expect(v.kind).toBe("ok");
  });

  it("reports degraded when the database is unreachable", () => {
    const v = interpretReadiness(503, { ...base, status: "unavailable", database: "unreachable" });
    expect(v.kind).toBe("degraded");
  });

  it("rejects malformed bodies instead of assuming health", () => {
    expect(interpretReadiness(200, { status: "ok" }).kind).toBe("api-unreachable");
    expect(interpretReadiness(200, null).kind).toBe("api-unreachable");
  });
});
