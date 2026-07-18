// Playwright globalSetup: defensive cleanup only. The console BUILD lives in
// boot-daemon.mjs (the webServer command), because Playwright starts webServers
// concurrently with globalSetup and the daemon mounts apps/console/dist at
// import time — so the build must complete inside the webServer boot, before
// the daemon process spawns, behind a cross-process lock.
//
// Here we only clear a stale build lock left by a crashed prior run, so a fresh
// run never deadlocks waiting on a lock nobody holds.
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { rmSync } from "node:fs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const CONSOLE_DIR = resolve(__dirname, "..");

export default async function globalSetup() {
  try {
    rmSync(resolve(CONSOLE_DIR, "dist.build.lock"), { force: true });
  } catch {
    /* best effort */
  }
}
