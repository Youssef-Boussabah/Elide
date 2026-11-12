import { describe, expect, it } from "vitest";
import { installFakeBackend } from "../test/fakeBackend";
import { ApiError, api, explain, setCsrfToken, toError } from "./client";
import { money, parseDollarsToCents } from "./format";
import { parseHash } from "../workspace/route";

describe("api client", () => {
  it("sends CSRF only on state-changing requests and the operation key when given", async () => {
    const calls = installFakeBackend(() => ({ status: 200, body: {} }));
    setCsrfToken("tok");
    await api("/v1/m/buyer/overview");
    await api("/v1/m/buyer/offers", { method: "POST", body: { a: 1 }, operationKey: "op-1234" });
    expect(calls[0]?.headers["X-CSRF-Token"]).toBeUndefined();
    expect(calls[1]?.headers["X-CSRF-Token"]).toBe("tok");
    expect(calls[1]?.headers["Idempotency-Key"]).toBe("op-1234");
  });

  it("maps structured and validation errors", () => {
    const e = toError(409, { detail: { code: "STALE_DIGEST", message: "x" } });
    expect(e).toBeInstanceOf(ApiError);
    expect(e.code).toBe("STALE_DIGEST");
    expect(explain(e)).toMatch(/changed since you reviewed/);
    const v = toError(422, { detail: [{ loc: ["body", "concession_cents"], msg: "too small" }] });
    expect(v.message).toBe("concession_cents: too small");
  });
});

describe("format and routes", () => {
  it("formats integer cents without float arithmetic", () => {
    expect(money(99000)).toBe("USD 990.00");
    expect(money(-1000)).toBe("−USD 10.00");
    expect(money(null)).toBe("—");
    expect(parseDollarsToCents("$1,234.5")).toBe(123450);
    expect(parseDollarsToCents("10.001")).toBeNull();
  });

  it("parses merchant routes and rejects unknown sections", () => {
    expect(parseHash("#/m/buyer/plan")).toEqual({ merchant: "buyer", section: "plan" });
    expect(parseHash("#/m/buyer/evil")).toEqual({ merchant: "buyer", section: "overview" });
    expect(parseHash("")).toEqual({ merchant: null, section: "overview" });
  });
});
