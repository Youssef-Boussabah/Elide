import { defineConfig } from "@playwright/test";

// Browser end-to-end tests against a real backend on a fresh, seeded database of their
// own (`scripts/e2e_setup.py` recreates `elide_e2e`). Uses the installed Google Chrome
// (`channel: "chrome"`), so no browser binary is downloaded.
const API = 8001;
const WEB = 5174;
const server = process.env.ELIDE_TEST_DATABASE_URL ?? "postgresql+psycopg://elide:elide_local_dev_only@127.0.0.1:5433/elide";
const e2eDb = server.replace(/\/[^/]*$/, "/elide_e2e");

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${WEB}`,
    channel: "chrome",
    trace: "retain-on-failure",
  },
  webServer: [
    {
      // The harness app = the real API + an in-process MOCK PayPal + loopback-only test
      // controls (scripts/e2e_app.py). No real PayPal call can be made from these tests.
      command: `uv run python scripts/e2e_setup.py && uv run uvicorn --app-dir scripts e2e_app:app --host 127.0.0.1 --port ${API}`,
      cwd: "..",
      url: `http://127.0.0.1:${API}/health`,
      reuseExistingServer: false,
      timeout: 180_000,
      env: {
        DATABASE_URL: e2eDb,
        APP_ORIGINS: JSON.stringify([`http://127.0.0.1:${WEB}`]),
        APP_ENV: process.env.APP_ENV ?? "local",
        ELIDE_E2E_HARNESS: "1",
        PAYMENT_PROVIDER: "none",
        PAYMENTS_EXECUTION_ENABLED: "false",
      },
    },
    {
      command: `pnpm exec vite --host 127.0.0.1 --port ${WEB} --strictPort`,
      url: `http://127.0.0.1:${WEB}`,
      reuseExistingServer: false,
      timeout: 120_000,
      env: { ELIDE_API_TARGET: `http://127.0.0.1:${API}` },
    },
  ],
});
