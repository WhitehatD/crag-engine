import {
  createRootRoute,
  createRoute,
  createRouter,
  lazyRouteComponent,
} from "@tanstack/react-router";
import { RootLayout } from "./root";

// Permissive root search: preserve every query key across navigation so
// ?embed=1 and ?glossary=1 survive client-side route changes, and per-view
// filter params (predicate_class, verdict, tab, offset, …) round-trip through
// the URL for shareable deep links.
type RootSearch = Record<string, string | number | undefined>;

const rootRoute = createRootRoute({
  component: RootLayout,
  validateSearch: (search: Record<string, unknown>): RootSearch => {
    const out: RootSearch = {};
    for (const [k, v] of Object.entries(search)) {
      if (v === undefined || v === null || v === "") continue;
      out[k] = typeof v === "number" ? v : String(v);
    }
    return out;
  },
});

// Each view is lazy-loaded so the initial payload only carries the shell + the
// landing route. Keeps the gzip budget honest.
const loopRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: lazyRouteComponent(() => import("./routes/Loop")),
});

const claimsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/claims",
  component: lazyRouteComponent(() => import("./routes/Claims")),
});

const reviewRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/review",
  component: lazyRouteComponent(() => import("./routes/Review")),
});

const groundingRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/grounding",
  component: lazyRouteComponent(() => import("./routes/Grounding")),
});

const corpusRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/corpus",
  component: lazyRouteComponent(() => import("./routes/Corpus")),
});

const sessionsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/sessions",
  component: lazyRouteComponent(() => import("./routes/Sessions")),
});

const routeTree = rootRoute.addChildren([
  loopRoute,
  claimsRoute,
  reviewRoute,
  groundingRoute,
  corpusRoute,
  sessionsRoute,
]);

export const router = createRouter({
  routeTree,
  // Served under /console — the router owns everything beneath it.
  basepath: "/console",
  defaultPreload: "intent",
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
