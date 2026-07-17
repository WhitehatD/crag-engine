import {
  createRootRoute,
  createRoute,
  createRouter,
  lazyRouteComponent,
} from "@tanstack/react-router";
import { RootLayout } from "./root";

const rootRoute = createRootRoute({ component: RootLayout });

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
