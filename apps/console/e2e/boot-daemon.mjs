// Boot one engine-daemon subprocess against a TEMP fixture DB on a NON-default
// port, for the Playwright gate. Used as a Playwright `webServer.command`.
//
// It NEVER touches a daemon on :8786. It:
//   1. seeds a temp fixture DB via e2e/fixtures/seed.py (seeded or --empty),
//   2. spawns the daemon (python apps/daemon/engine_daemon.py) with
//      CRAG_ANCHOR_DB_PATH=<temp> and CRAG_ANCHOR_DAEMON_PORT=<port>,
//   3. inherits stdio so Playwright's webServer readiness sees the boot log and
//      polls the health/modules URL itself.
//
// Env knobs (set per-project in playwright.config.ts):
//   E2E_DB_PATH   absolute path for the temp fixture DB
//   E2E_PORT      daemon port (e.g. 8799 seeded, 8801 empty)
//   E2E_EMPTY     "1" => seed schema only (empty-DB spec)
//   E2E_LOG_DIR   daemon log dir (temp, isolated from the real logs/)
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import {
  existsSync,
  mkdirSync,
  statSync,
  readdirSync,
  openSync,
  closeSync,
  rmSync,
} from "node:fs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const CONSOLE_DIR = resolve(__dirname, "..");
const REPO_ROOT = resolve(CONSOLE_DIR, "..", "..");
const SEED = resolve(__dirname, "fixtures", "seed.py");
const DAEMON = resolve(REPO_ROOT, "apps", "daemon", "engine_daemon.py");
const DIST = resolve(CONSOLE_DIR, "dist");
const SRC = resolve(CONSOLE_DIR, "src");

// The daemon mounts apps/console/dist at import time, so the console MUST be
// built BEFORE the daemon spawns. Playwright starts webServer processes
// concurrently with globalSetup, so building here (in the webServer command,
// pre-spawn) — not in globalSetup — is the only race-free place. A file lock
// makes the two parallel webServers cooperate: the first builds, the second
// waits then reuses.
function newestMtime(dir) {
  let newest = 0;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const p = resolve(dir, entry.name);
    if (entry.isDirectory()) newest = Math.max(newest, newestMtime(p));
    else newest = Math.max(newest, statSync(p).mtimeMs);
  }
  return newest;
}

function distStale() {
  const indexHtml = resolve(DIST, "index.html");
  if (!existsSync(indexHtml)) return true;
  try {
    return newestMtime(SRC) > statSync(indexHtml).mtimeMs;
  } catch {
    return true;
  }
}

function sleepSync(ms) {
  // Block without a busy-loop: Atomics.wait on a throwaway buffer.
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

function ensureBuilt() {
  const lock = resolve(CONSOLE_DIR, "dist.build.lock");
  // Cross-process build lock: the two parallel webServers must not run vite
  // into the same dist/ at once. First to create the lock builds; the other
  // waits for a fresh dist then proceeds.
  let haveLock = false;
  try {
    // wx = exclusive create; throws if it already exists.
    const fd = openSync(lock, "wx");
    closeSync(fd);
    haveLock = true;
  } catch {
    haveLock = false;
  }

  if (!haveLock) {
    console.log("boot-daemon: waiting for peer build…");
    for (let i = 0; i < 300 && distStale(); i++) sleepSync(1000);
    if (distStale()) {
      console.error("boot-daemon: peer build did not finish in time");
      process.exit(1);
    }
    console.log("boot-daemon: peer build done — reusing dist");
    return;
  }

  try {
    if (!distStale()) {
      console.log("boot-daemon: console dist fresh — skipping build");
      return;
    }
    console.log("boot-daemon: building console (vite build)…");
    const r = spawnSync("npm", ["run", "build"], {
      cwd: CONSOLE_DIR,
      stdio: "inherit",
      shell: process.platform === "win32",
    });
    if (r.status !== 0) {
      console.error("boot-daemon: console build failed");
      process.exit(r.status ?? 1);
    }
  } finally {
    try {
      rmSync(lock, { force: true });
    } catch {
      /* best effort */
    }
  }
}

const PY = process.env.E2E_PYTHON || (process.platform === "win32" ? "python" : "python3");
const DB_PATH = process.env.E2E_DB_PATH;
const PORT = process.env.E2E_PORT;
const EMPTY = process.env.E2E_EMPTY === "1";
const LOG_DIR = process.env.E2E_LOG_DIR || resolve(CONSOLE_DIR, "e2e", ".daemon-logs");

if (!DB_PATH || !PORT) {
  console.error("boot-daemon: E2E_DB_PATH and E2E_PORT are required");
  process.exit(2);
}
if (Number(PORT) === 8786) {
  console.error("boot-daemon: refusing to bind :8786 (the live daemon's port)");
  process.exit(2);
}

mkdirSync(LOG_DIR, { recursive: true });

// 0) Ensure the console is built BEFORE the daemon (which mounts dist at import
//    time) spawns. Race-free vs the parallel webServers via a build lock.
ensureBuilt();

// 1) Seed the temp fixture DB (deterministic; stdlib sqlite only).
const seedArgs = EMPTY ? [SEED, DB_PATH, "--empty"] : [SEED, DB_PATH];
const seeded = spawnSync(PY, seedArgs, { stdio: "inherit" });
if (seeded.status !== 0) {
  console.error("boot-daemon: seed failed");
  process.exit(seeded.status ?? 1);
}

// 2) Spawn the daemon against the temp DB + non-default port. Its own fresh-DB
//    bootstrap is a no-op here (schema_version already populated by the seeder),
//    so seeded rows survive.
const env = {
  ...process.env,
  CRAG_ANCHOR_DB_PATH: DB_PATH,
  CRAG_ANCHOR_DAEMON_PORT: String(PORT),
  CRAG_ANCHOR_DAEMON_HOST: "127.0.0.1",
  CRAG_ANCHOR_LOG_DIR: LOG_DIR,
};

console.log(
  `boot-daemon: starting engine on 127.0.0.1:${PORT} db=${DB_PATH} ` +
    `empty=${EMPTY} (live :8786 untouched)`,
);

const child = spawn(PY, [DAEMON], { env, stdio: "inherit", cwd: REPO_ROOT });

const forward = (sig) => () => {
  if (!child.killed) child.kill(sig);
};
process.on("SIGINT", forward("SIGINT"));
process.on("SIGTERM", forward("SIGTERM"));
process.on("exit", () => {
  if (!child.killed) child.kill("SIGTERM");
});
child.on("exit", (code) => process.exit(code ?? 0));
