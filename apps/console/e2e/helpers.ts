import { type Page, type ConsoleMessage, expect } from "@playwright/test";

// Every route in the current console + the <h1> title each renders via
// ViewHeader/AboutPanel (the route-identifying element the gate asserts).
// /infra is the ops-overlay page shell — harmless + present on the public
// engine, so it is part of the render gate even though the PUBLIC manifest
// (/console/modules) lists only the 6 core modules.
export const ROUTES: { path: string; title: string }[] = [
  { path: "/console/", title: "Loop" },
  { path: "/console/claims", title: "Claims" },
  { path: "/console/review", title: "Review" },
  { path: "/console/grounding", title: "Grounding" },
  { path: "/console/corpus", title: "Corpus" },
  { path: "/console/sessions", title: "Sessions" },
  { path: "/console/infra", title: "Infra" },
];

export const VIEWPORTS = {
  mobile: { width: 375, height: 812 },
  desktop: { width: 1280, height: 900 },
};

// Attach a console.error collector. A route that logs console.error is a
// render bug (unguarded API access, boundary trip) — the failure class this
// gate exists to catch. Returns the live array; assert it stays empty.
//
// /infra is the ops-overlay page shell: on the PUBLIC engine its /infra/*
// endpoints don't exist BY DESIGN (router.tsx + Infra.tsx docstring), so its
// react-query calls 404 and fail soft to empty states. Those 404 network
// "Failed to load resource" console entries are architectural, not crashes, so
// the /infra render gate tolerates THAT ONE pattern. A real render error
// (pageerror / any other console.error) still fails the test.
const OPS_404 = /Failed to load resource: the server responded with a status of 404/;

export function collectConsoleErrors(
  page: Page,
  opts: { tolerateOps404?: boolean } = {},
): string[] {
  const errors: string[] = [];
  page.on("console", (msg: ConsoleMessage) => {
    if (msg.type() !== "error") return;
    const text = msg.text();
    if (opts.tolerateOps404 && OPS_404.test(text)) return;
    errors.push(text);
  });
  page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));
  return errors;
}

// Horizontal-scroll probe at 375px — the mobile-breakage class (§6 bar).
// Layout fixes are the v3 rebuild's charter ("do NOT redesign any console page"
// in this gate wave), so a pre-existing overflow is recorded as a SOFT
// annotation on the test rather than a hard fail. The hard teeth of this gate
// are: renders + ZERO console errors (the crash class that shipped twice).
// Returns the overflow px so callers can annotate.
export async function measureHorizontalOverflow(page: Page): Promise<number> {
  return page.evaluate(() => {
    const el = document.documentElement;
    return el.scrollWidth - el.clientWidth; // >1 == real horizontal scroll
  });
}
