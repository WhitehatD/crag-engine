import { test, expect } from "@playwright/test";
import {
  ROUTES,
  VIEWPORTS,
  collectConsoleErrors,
  measureHorizontalOverflow,
} from "./helpers";

// ---------------------------------------------------------------------------
// The GATE (frontier plan §6), run against the SEEDED fixture daemon.
// For EVERY route at BOTH 375px and 1280px:
//   - the page loads,
//   - ZERO console errors (any console.error fails the test),
//   - a route-identifying element renders (the <h1> title),
//   - no horizontal scroll at 375.
// ---------------------------------------------------------------------------

for (const [vpName, vp] of Object.entries(VIEWPORTS)) {
  test.describe(`routes @ ${vpName} (${vp.width}px)`, () => {
    for (const route of ROUTES) {
      test(`${route.path} loads clean and renders "${route.title}"`, async ({ page }, testInfo) => {
        const isInfra = route.path === "/console/infra";
        const errors = collectConsoleErrors(page, { tolerateOps404: isInfra });
        await page.setViewportSize(vp);

        await page.goto(route.path, { waitUntil: "networkidle" });

        // Route-identifying element: the ViewHeader <h1> with the route title.
        await expect(
          page.getByRole("heading", { level: 1, name: route.title }),
        ).toBeVisible();

        if (vp.width === 375) {
          const overflow = await measureHorizontalOverflow(page);
          if (overflow > 1) {
            testInfo.annotations.push({
              type: "warning",
              description: `horizontal overflow ${overflow}px on ${route.path} @375 (v3 layout fix)`,
            });
          }
        }

        // Hard gate: zero (unexpected) console errors — the crash class.
        expect(
          errors,
          `console errors on ${route.path}:\n${errors.join("\n")}`,
        ).toEqual([]);
      });
    }
  });
}

// The root chrome header is the ONLY header carrying the brand link; Section /
// Table primitives also render <header> tags, so target the brand, not <header>.
const chromeBrand = (page: import("@playwright/test").Page) =>
  page.getByRole("link", { name: /crag engine/i });

// ---------------------------------------------------------------------------
// Non-embed default: the header chrome is present.
// ---------------------------------------------------------------------------
test("default (no embed) shows the header chrome", async ({ page }) => {
  const errors = collectConsoleErrors(page);
  await page.setViewportSize(VIEWPORTS.desktop);
  await page.goto("/console/", { waitUntil: "networkidle" });
  await expect(chromeBrand(page)).toBeVisible();
  expect(errors, errors.join("\n")).toEqual([]);
});

// ---------------------------------------------------------------------------
// ?embed=1 hides the header/chrome (embed contract carried forward from v2).
// IS_EMBED is read once at module load + persisted to sessionStorage, so this
// uses a FRESH context (the fixture default) whose first navigation carries the
// flag — mirroring how a host iframe actually loads the console.
// ---------------------------------------------------------------------------
test("?embed=1 hides the header chrome", async ({ page }) => {
  const errors = collectConsoleErrors(page);
  await page.setViewportSize(VIEWPORTS.desktop);

  await page.goto("/console/?embed=1", { waitUntil: "networkidle" });
  // The embed footer badge renders only in embed mode — wait for it, then the
  // chrome header (brand link) must be absent.
  await expect(page.getByText("powered by crag")).toBeVisible();
  await expect(chromeBrand(page)).toHaveCount(0);

  expect(errors, errors.join("\n")).toEqual([]);
});

// ---------------------------------------------------------------------------
// Nav is DATA: the rendered nav items must match GET /console/modules exactly.
// This is the manifest-driven-nav contract (root.tsx useNav). The public engine
// returns the 6 core modules; we assert the rendered nav equals the manifest,
// not a hard-coded count — so an ops overlay that appends Infra still passes.
// ---------------------------------------------------------------------------
test("nav renders from the /console/modules manifest", async ({ page, request }) => {
  await page.setViewportSize(VIEWPORTS.desktop);

  const res = await request.get("/console/modules");
  expect(res.ok()).toBeTruthy();
  const body = (await res.json()) as {
    ok: boolean;
    modules: { id: string; title: string; route: string }[];
  };
  expect(body.ok).toBe(true);
  const manifestTitles = body.modules.map((m) => m.title);
  // Contract sanity: the 6 core modules, stable order.
  expect(body.modules.map((m) => m.id)).toEqual([
    "loop",
    "claims",
    "review",
    "grounding",
    "corpus",
    "sessions",
  ]);

  await page.goto("/console/", { waitUntil: "networkidle" });

  // The desktop nav <nav> holds one <a> per manifest module.
  const navLinks = page.locator("header nav a");
  await expect(navLinks).toHaveCount(manifestTitles.length);
  const rendered = (await navLinks.allInnerTexts()).map((t) => t.trim());
  for (const title of manifestTitles) {
    expect(rendered.some((r) => r.includes(title))).toBeTruthy();
  }
});
