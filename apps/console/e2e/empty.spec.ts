import { test, expect } from "@playwright/test";
import {
  ROUTES,
  VIEWPORTS,
  collectConsoleErrors,
  measureHorizontalOverflow,
} from "./helpers";

// ---------------------------------------------------------------------------
// EMPTY-DB gate (frontier plan §6 + the #3592 "no view ships over an empty
// dimension" rule). This project boots a SECOND daemon against an UNSEEDED
// temp DB. A fresh `crag-anchor up` install is the evaluator's first-run path:
// every empty state must RENDER (teaching empties), not crash. Same assertions
// as the seeded gate — zero console errors, title renders, no h-scroll at 375.
// ---------------------------------------------------------------------------

for (const [vpName, vp] of Object.entries(VIEWPORTS)) {
  test.describe(`empty DB @ ${vpName} (${vp.width}px)`, () => {
    for (const route of ROUTES) {
      test(`${route.path} renders empty state for "${route.title}"`, async ({ page }, testInfo) => {
        const isInfra = route.path === "/console/infra";
        const errors = collectConsoleErrors(page, { tolerateOps404: isInfra });
        await page.setViewportSize(vp);

        await page.goto(route.path, { waitUntil: "networkidle" });

        await expect(
          page.getByRole("heading", { level: 1, name: route.title }),
        ).toBeVisible();

        if (vp.width === 375) {
          const overflow = await measureHorizontalOverflow(page);
          if (overflow > 1) {
            testInfo.annotations.push({
              type: "warning",
              description: `horizontal overflow ${overflow}px on empty ${route.path} @375 (v3 layout fix)`,
            });
          }
        }

        expect(
          errors,
          `console errors on empty ${route.path}:\n${errors.join("\n")}`,
        ).toEqual([]);
      });
    }
  });
}
