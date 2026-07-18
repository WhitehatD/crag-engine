import { defineConfig, devices } from "@playwright/test";
import { fileURLToPath } from "node:url";
import { dirname, resolve, join } from "node:path";
import { tmpdir } from "node:os";

// The browser-verification GATE (frontier plan §0, §6). Two prior consoles
// shipped with runtime render crashes because NO browser testing existed —
// endpoint curls + tsc cannot catch an `undefined.verdict` at render time.
// This suite loads EVERY route at 375px and 1280px against a real daemon
// (booted on a NON-default port over a temp fixture DB) and fails on any
// console.error. It NEVER touches the live daemon on :8786.

const __dirname = dirname(fileURLToPath(import.meta.url));
const BOOT = resolve(__dirname, "e2e", "boot-daemon.mjs");

const SEEDED_PORT = 8799;
const EMPTY_PORT = 8801;
const SEEDED_DB = join(tmpdir(), "crag-e2e-seeded.db");
const EMPTY_DB = join(tmpdir(), "crag-e2e-empty.db");
const SEEDED_LOGS = join(tmpdir(), "crag-e2e-seeded-logs");
const EMPTY_LOGS = join(tmpdir(), "crag-e2e-empty-logs");

const SEEDED_BASE = `http://127.0.0.1:${SEEDED_PORT}`;
const EMPTY_BASE = `http://127.0.0.1:${EMPTY_PORT}`;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  // The console build happens inside boot-daemon.mjs (the webServer command),
  // pre-spawn and behind a build lock — NOT in a globalSetup, because Playwright
  // starts webServers concurrently with globalSetup and the daemon mounts dist
  // at import time (it must exist before the daemon boots).
  globalSetup: "./e2e/global-setup.mjs",

  // Screenshots + traces on failure become CI artifacts.
  use: {
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    // console errors are asserted per-spec; keep viewport neutral here.
  },

  projects: [
    {
      name: "seeded",
      testMatch: /routes\.spec\.ts$/,
      use: { ...devices["Desktop Chrome"], baseURL: SEEDED_BASE },
    },
    {
      name: "empty",
      testMatch: /empty\.spec\.ts$/,
      use: { ...devices["Desktop Chrome"], baseURL: EMPTY_BASE },
    },
  ],

  // One daemon per project, each on its own non-default port + temp DB. Poll
  // /console/modules (pure, no embedding-model gate) for readiness — /health
  // is 503 until the model loads and the console never needs the model to render.
  webServer: [
    {
      command: `node "${BOOT}"`,
      url: `${SEEDED_BASE}/console/modules`,
      timeout: 180_000,
      reuseExistingServer: false,
      env: {
        E2E_DB_PATH: SEEDED_DB,
        E2E_PORT: String(SEEDED_PORT),
        E2E_LOG_DIR: SEEDED_LOGS,
        E2E_EMPTY: "0",
      },
    },
    {
      command: `node "${BOOT}"`,
      url: `${EMPTY_BASE}/console/modules`,
      timeout: 180_000,
      reuseExistingServer: false,
      env: {
        E2E_DB_PATH: EMPTY_DB,
        E2E_PORT: String(EMPTY_PORT),
        E2E_LOG_DIR: EMPTY_LOGS,
        E2E_EMPTY: "1",
      },
    },
  ],
});
