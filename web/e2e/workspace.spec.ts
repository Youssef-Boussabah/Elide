import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Browser, type Page } from "@playwright/test";

// End-to-end regression over the real stack (FastAPI + PostgreSQL + Vite) with SYNTHETIC
// data and TEST identities. Separate browser contexts = separate merchant sessions.
// No PayPal call, no model call and no money movement can happen in this flow.

const here = dirname(fileURLToPath(import.meta.url));
const creds = (): Record<string, string> =>
  JSON.parse(readFileSync(resolve(here, "../../.local/e2e_credentials.json"), "utf-8")).users;

const BUYER = "buyer-owner@elide.test";
const SUPPLIER = "supplier-owner@elide.test";

async function signIn(browser: Browser, login: string): Promise<Page> {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  await page.goto("/");
  await page.getByLabel("Email").fill(login);
  await page.getByLabel("Password").fill(creds()[login] ?? "");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByText("TEST IDENTITY")).toBeVisible();
  return page;
}

async function planCount(page: Page): Promise<number> {
  const r = await page.request.get("/api/v1/m/buyer/plans");
  expect(r.ok()).toBeTruthy();
  return ((await r.json()) as { plans: unknown[] }).plans.length;
}

test.describe.serial("merchant workspace", () => {
  test("rejects bad credentials with a generic message", async ({ page }) => {
    await page.goto("/");
    await page.getByLabel("Email").fill(BUYER);
    await page.getByLabel("Password").fill("definitely-not-the-password");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("alert")).toContainText(/invalid login or password/i);
  });

  test("viewing recommendations is read-only", async ({ browser }) => {
    const buyer = await signIn(browser, BUYER);
    const before = await planCount(buyer);
    for (let i = 0; i < 3; i++) {
      await buyer.goto("/#/m/buyer/opportunities");
      await expect(buyer.getByRole("region", { name: "Recommendation" })).toBeVisible();
      await buyer.reload();
    }
    expect(await planCount(buyer)).toBe(before);
    await expect(buyer.getByText("A negotiated change could help")).toBeVisible();
  });

  test("buyer counters, supplier accepts independently, buyer approves and prepares a held intent", async ({
    browser,
  }) => {
    const buyer = await signIn(browser, BUYER);
    await buyer.goto("/#/m/buyer/negotiations");
    await buyer.getByRole("button", { name: "Counter" }).click();
    await buyer.getByRole("button", { name: "Suggest a bounded discount" }).click();
    await expect(buyer.getByLabel("Discount (USD)")).toHaveValue("10.00");
    await buyer.getByRole("button", { name: "Send counteroffer" }).click();
    await expect(buyer.getByText("Recorded: countered.")).toBeVisible();

    const supplier = await signIn(browser, SUPPLIER);
    await supplier.goto("/#/m/food_supplier/negotiations");
    const offer = supplier.getByRole("article", { name: "Offer on food_001" });
    await expect(offer).toContainText("You give up");
    await expect(offer).toContainText("USD 10.00");
    await offer.getByRole("button", { name: "Accept these terms" }).click();
    await expect(supplier.getByText("Recorded: agreed.")).toBeVisible();

    // privacy: the supplier session cannot reach the buyer's workspace
    const blocked = await supplier.request.get("/api/v1/m/buyer/overview");
    expect(blocked.status()).toBe(404);
    await supplier.goto("/#/m/buyer/overview");
    await expect(supplier).toHaveURL(/#\/m\/food_supplier\//);
    await expect(supplier.getByText("1,490.00")).toHaveCount(0);

    await buyer.goto("/#/m/buyer/plan");
    await buyer.getByRole("button", { name: /Create plan for approval|Re-check and update plan/ }).click();
    await expect(buyer.getByText(/independently verified and stored|was reused/)).toBeVisible();
    const leg = buyer.getByRole("article", { name: /Leg food_001:r\d+/ });
    await leg.getByRole("button", { name: "Approve my payment…" }).click();
    await expect(leg).toContainText("Authorize paying exactly USD 990.00 to food_supplier");
    await leg.getByRole("button", { name: "Confirm approve" }).click();
    await expect(leg.getByText("APPROVED")).toBeVisible();
    await leg.getByRole("button", { name: "Prepare payment intent" }).click();
    await expect(buyer.getByText("held: no PayPal call")).toBeVisible();
    await expect(leg).toContainText("Money moved: no");
  });

  test("a downstream payment is never executable before its upstream payment", async ({ browser }) => {
    const supplier = await signIn(browser, SUPPLIER);
    await supplier.goto("/#/m/food_supplier/plan");
    await supplier.getByRole("button", { name: /Create plan for approval|Re-check and update plan/ }).click();
    await expect(supplier.getByText(/independently verified and stored|was reused/)).toBeVisible();
    // food_001 is in execution (intent held), so nothing funds freight_001 yet: the supplier
    // has no executable leg and no "Prepare payment intent" action anywhere.
    await expect(supplier.getByRole("button", { name: "Prepare payment intent" })).toHaveCount(0);
  });

  test("a newer confirmed reserve marks the earlier approval as changed", async ({ browser }) => {
    const buyer = await signIn(browser, BUYER);
    await buyer.goto("/#/m/buyer/policy");
    await buyer
      .getByLabel("Your rule, in your own words")
      .fill("Keep $700 protected at all times. Only pay early for at least $10 off.");
    await buyer.getByLabel("Reserve floor (as written above)").fill("700");
    await buyer.getByLabel("Minimum early-payment discount (as written above)").fill("10");
    await buyer.getByRole("button", { name: "Validate draft" }).click();
    const draft = buyer.getByRole("article").filter({ hasText: "Keep $700 protected" });
    await expect(draft).toContainText("READY FOR CONFIRMATION");
    await draft.getByRole("button", { name: "Confirm this policy" }).click();
    await expect(buyer.getByText("revision 2 in force")).toBeVisible();

    await buyer.goto("/#/m/buyer/plan");
    const leg = buyer.getByRole("article", { name: /Leg food_001:r\d+/ });
    // food_001 is in execution (its intent was prepared above). Since the P5 gate fix
    // (evidence/phase5/pilot_intent_held_and_label_2026-11-12.md) the change is reported as
    // IN EXECUTION: CONDITIONS CHANGED, and no second approval is offered for that invoice.
    await expect(leg.getByText("IN EXECUTION: CONDITIONS CHANGED")).toBeVisible();
    await expect(leg).toContainText("CONDITIONS_CHANGED");
    await expect(leg.getByText("APPROVED", { exact: true })).toHaveCount(0);
    await expect(leg.getByRole("button", { name: "Approve my payment…" })).toHaveCount(0);
  });
});
