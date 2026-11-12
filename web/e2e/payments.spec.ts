import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type APIRequestContext, type Browser, type Page } from "@playwright/test";

// P5 payment execution in the browser against the MOCK PayPal provider of
// scripts/e2e_app.py (ADR-0012; owner gate review). The pilot network is non-synthetic but
// every merchant is a TEST identity and the provider is an in-process double: no PayPal
// call is possible. Requests to the Sandbox host are aborted and window.open is captured,
// so the browser never leaves the test stack.

const here = dirname(fileURLToPath(import.meta.url));
const creds = (): Record<string, string> =>
  JSON.parse(readFileSync(resolve(here, "../../.local/e2e_credentials.json"), "utf-8")).users;

const API = "http://127.0.0.1:8001";
const BUYER = "pilot-buyer@elide.test";
const SELLER = "pilot-seller@elide.test";
const WRONG_PAYEE = "E2EMOCKSOMEONEELSE";

async function signIn(browser: Browser, login: string): Promise<Page> {
  const ctx = await browser.newContext();
  await ctx.route(/sandbox\.paypal\.com|paypal\.com/, (route) => route.abort());
  await ctx.addInitScript(() => {
    const w = window as unknown as { __opened: string[]; open: typeof window.open };
    w.__opened = [];
    w.open = ((url?: string | URL) => {
      w.__opened.push(String(url));
      return null;
    }) as typeof window.open;
  });
  const page = await ctx.newPage();
  await page.goto("/");
  await page.getByLabel("Email").fill(login);
  await page.getByLabel("Password").fill(creds()[login] ?? "");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByText("TEST IDENTITY", { exact: true })).toBeVisible();
  return page;
}

async function runWorker(request: APIRequestContext): Promise<void> {
  const r = await request.post(`${API}/__e2e__/worker/run`);
  expect(r.ok()).toBeTruthy();
}

async function mockCalls(request: APIRequestContext): Promise<string[]> {
  const r = await request.get(`${API}/__e2e__/mock/calls`);
  return ((await r.json()) as { calls: { operation: string }[] }).calls.map((c) => c.operation);
}

async function intentId(page: Page, obligation: string): Promise<string> {
  const r = await page.request.get("/api/v1/m/pilot_buyer/payment-intents");
  const all = ((await r.json()) as { intents: { intent_id: string; obligation_id: string }[] })
    .intents;
  const found = all.find((i) => i.obligation_id === obligation);
  if (!found) throw new Error(`no intent for ${obligation}`);
  return found.intent_id;
}

/** The buyer accepts the seller's USD 1.00 offer, re-plans, approves its own leg and
 * prepares the payment intent (queued for the worker). */
async function approveAndPrepare(buyer: Page, obligation: string): Promise<void> {
  await buyer.goto("/#/m/pilot_buyer/negotiations");
  const offer = buyer.getByRole("article", { name: `Offer on ${obligation}` });
  await offer.getByRole("button", { name: "Accept these terms" }).click();
  await expect(buyer.getByText("Recorded: agreed.")).toBeVisible();
  await buyer.goto("/#/m/pilot_buyer/plan");
  await buyer.getByRole("button", { name: /Create plan for approval|Re-check and update plan/ }).click();
  await expect(buyer.getByText(/independently verified and stored|was reused/)).toBeVisible();
  const leg = buyer.getByRole("article", { name: new RegExp(`Leg ${obligation}:r\\d+`) });
  await leg.getByRole("button", { name: "Approve my payment…" }).click();
  await expect(leg).toContainText("Authorize paying exactly USD 24.00 to pilot_seller");
  await leg.getByRole("button", { name: "Confirm approve" }).click();
  await expect(leg.getByText("APPROVED")).toBeVisible();
  await leg.getByRole("button", { name: "Prepare payment intent" }).click();
  await expect(buyer.getByRole("row").filter({ hasText: obligation })).toContainText(
    "queued for PayPal",
  );
}

test.describe.serial("payment execution (mock PayPal)", () => {
  test("payer prepares an intent; the worker creates and sends the invoice; only the payer gets the hosted link", async ({
    browser,
    request,
  }) => {
    const buyer = await signIn(browser, BUYER);
    await approveAndPrepare(buyer, "pilot_001");
    expect(await mockCalls(request)).toEqual([]); // nothing is sent before the worker runs

    await runWorker(request);
    await buyer.reload();
    const row = buyer.getByRole("row").filter({ hasText: "pilot_001" });
    await expect(row).toContainText("AWAITING PAYER AT PAYPAL");
    await expect(row).toContainText("no money moved");
    await row.getByRole("button", { name: "Open PayPal payment page" }).click();
    await expect
      .poll(() => buyer.evaluate(() => (window as unknown as { __opened: string[] }).__opened))
      .toHaveLength(1);
    const [url] = await buyer.evaluate(() => (window as unknown as { __opened: string[] }).__opened);
    expect(url).toMatch(/^https:\/\/www\.sandbox\.paypal\.com\/.*MOCK/);
    const calls = await mockCalls(request);
    // Provider mutations: exactly one create, then one send (each preceded by a GET).
    expect(calls.filter((c) => c !== "get_invoice")).toEqual(["create_invoice", "send_invoice"]);
    expect(calls.slice(0, 4)).toEqual(["create_invoice", "get_invoice", "send_invoice", "get_invoice"]);

    const seller = await signIn(browser, SELLER);
    await seller.goto("/#/m/pilot_seller/plan");
    await expect(seller.getByRole("button", { name: "Open PayPal payment page" })).toHaveCount(0);
    const id = await intentId(buyer, "pilot_001");
    const own = await seller.request.get(`/api/v1/m/pilot_seller/payment-intents/${id}/payer-link`);
    expect(own.status()).toBe(404);
    const foreign = await seller.request.get(`/api/v1/m/pilot_buyer/payment-intents/${id}/payer-link`);
    expect(foreign.status()).toBe(404);
  });

  test("mock PayPal reports PAID: the payment reconciles and the receipt is not spendable", async ({
    browser,
    request,
  }) => {
    const paid = await request.post(`${API}/__e2e__/mock/pay`, { data: { obligation_id: "pilot_001" } });
    expect(paid.ok()).toBeTruthy();
    await runWorker(request);

    const buyer = await signIn(browser, BUYER);
    await buyer.goto("/#/m/pilot_buyer/plan");
    const row = buyer.getByRole("row").filter({ hasText: "pilot_001" }).first();
    await expect(row).toContainText("PAYPAL CONFIRMED RECONCILED");
    await expect(row).toContainText("funds usable: no");
    const mine = buyer.getByRole("region", { name: "Settlements" });
    await expect(mine).toContainText("You paid");
    await expect(mine).toContainText("USD 24.00 paid via PayPal");
    await expect(mine).not.toContainText("PayPal fee"); // the payee's private figures

    const seller = await signIn(browser, SELLER);
    await seller.goto("/#/m/pilot_seller/plan");
    const theirs = seller.getByRole("region", { name: "Settlements" });
    await expect(theirs).toContainText("You received");
    await expect(theirs).toContainText("gross USD 24.00");
    await expect(theirs).toContainText("PROVIDER CONFIRMED NOT SPENDABLE");
    await expect(theirs).toContainText("usable USD: USD 0.00");
    await expect(theirs).not.toContainText("spendable: yes");

    const calls = await mockCalls(request);
    expect(calls.filter((c) => c === "create_invoice")).toHaveLength(1);
    expect(calls.filter((c) => c === "send_invoice")).toHaveLength(1);
    expect(calls).not.toContain("cancel_invoice");
  });

  test("a capture paid to the wrong payee account is refused and goes to operator review", async ({
    browser,
    request,
  }) => {
    const buyer = await signIn(browser, BUYER);
    await approveAndPrepare(buyer, "pilot_002");
    await runWorker(request);
    const wrong = await request.post(`${API}/__e2e__/mock/pay`, {
      data: { obligation_id: "pilot_002", payee_merchant_id: WRONG_PAYEE },
    });
    expect(wrong.ok()).toBeTruthy();
    await runWorker(request);
    await buyer.reload();
    const row = buyer.getByRole("row").filter({ hasText: "pilot_002" });
    await expect(row).toContainText("NEEDS OPERATOR REVIEW");
    await expect(row).toContainText("reason: CAPTURE_MISMATCH");
    await expect(row).toContainText("funds usable: no");
    await expect(buyer.getByRole("region", { name: "Settlements" }).getByRole("row")).toHaveCount(2); // header + pilot_001 only
  });

  test("a stale authorization never reaches PayPal", async ({ browser, request }) => {
    const buyer = await signIn(browser, BUYER);
    await approveAndPrepare(buyer, "pilot_003");
    const before = (await mockCalls(request)).filter((c) => c === "create_invoice").length;

    // A newer confirmed policy raises the reserve: the approved conditions no longer hold.
    await buyer.goto("/#/m/pilot_buyer/policy");
    await buyer
      .getByLabel("Your rule, in your own words")
      .fill("Keep $70 protected at all times. Only pay early for at least $1 off.");
    await buyer.getByLabel("Reserve floor (as written above)").fill("70");
    await buyer.getByLabel("Minimum early-payment discount (as written above)").fill("1");
    await buyer.getByRole("button", { name: "Validate draft" }).click();
    const draft = buyer.getByRole("article").filter({ hasText: "Keep $70 protected" });
    await expect(draft).toContainText("READY FOR CONFIRMATION");
    await draft.getByRole("button", { name: "Confirm this policy" }).click();
    await expect(buyer.getByText("revision 2 in force")).toBeVisible();

    await runWorker(request);
    await buyer.goto("/#/m/pilot_buyer/plan");
    const row = buyer.getByRole("row").filter({ hasText: "pilot_003" });
    await expect(row).toContainText("CANCELLED");
    await expect(row).toContainText("reason: CONDITIONS_CHANGED");
    expect((await mockCalls(request)).filter((c) => c === "create_invoice")).toHaveLength(before);
  });

  test("a held intent shows IN EXECUTION, and only the payer's confirmed release queues it", async ({
    browser,
    request,
  }) => {
    const off = await request.post(`${API}/__e2e__/execution`, { data: { enabled: false } });
    expect(off.ok()).toBeTruthy();
    const buyer = await signIn(browser, BUYER);
    await buyer.goto("/#/m/pilot_buyer/negotiations");
    const offer = buyer.getByRole("article", { name: "Offer on pilot_004" });
    await offer.getByRole("button", { name: "Accept these terms" }).click();
    await expect(buyer.getByText("Recorded: agreed.")).toBeVisible();
    await buyer.goto("/#/m/pilot_buyer/plan");
    await buyer.getByRole("button", { name: /Create plan for approval|Re-check and update plan/ }).click();
    await expect(buyer.getByText(/independently verified and stored|was reused/)).toBeVisible();
    const leg = buyer.getByRole("article", { name: /Leg pilot_004:r\d+/ });
    await leg.getByRole("button", { name: "Approve my payment…" }).click();
    await leg.getByRole("button", { name: "Confirm approve" }).click();
    await expect(leg.getByText("APPROVED")).toBeVisible();
    await leg.getByRole("button", { name: "Prepare payment intent" }).click();
    const row = buyer.getByRole("row").filter({ hasText: "pilot_004" });
    await expect(row).toContainText("held: no PayPal call (payment execution is switched off)");
    await expect(row.getByRole("button", { name: "Allow PayPal dispatch" })).toHaveCount(0);

    // The plan must not ask for a second approval of an invoice that is in execution.
    await buyer.reload();
    await expect(leg.getByText("IN EXECUTION", { exact: true })).toBeVisible();
    await expect(leg.getByText("CONDITIONS CHANGED: RE-APPROVE")).toHaveCount(0);
    await expect(leg.getByRole("button", { name: "Approve my payment…" })).toHaveCount(0);

    // Switching execution on releases nothing by itself.
    const before = (await mockCalls(request)).length;
    await request.post(`${API}/__e2e__/execution`, { data: { enabled: true } });
    await runWorker(request);
    expect((await mockCalls(request)).length).toBe(before);

    // The payee sees the payment in execution but cannot release it.
    const seller = await signIn(browser, SELLER);
    await seller.goto("/#/m/pilot_seller/plan");
    await expect(seller.getByRole("button", { name: "Allow PayPal dispatch" })).toHaveCount(0);

    await buyer.reload();
    await row.getByRole("button", { name: "Allow PayPal dispatch" }).click();
    await expect(row).toContainText("one PayPal invoice for USD 24.00 to pilot_seller");
    await row.getByRole("button", { name: "Confirm dispatch" }).click();
    await expect(row).toContainText("queued for PayPal");
    await runWorker(request);
    await buyer.reload();
    await expect(row).toContainText("AWAITING PAYER AT PAYPAL");
    const calls = (await mockCalls(request)).slice(before);
    expect(calls.filter((c) => c !== "get_invoice")).toEqual(["create_invoice", "send_invoice"]);
  });
});

