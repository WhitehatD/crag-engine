import { Suspense, lazy, useEffect } from "react";
import { Link, Outlet, useNavigate, useRouterState } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { api } from "./lib/api";
import type { HealthCheck } from "./lib/types";
import { IS_EMBED, postReady, installHeightReporter } from "./lib/embed";
import { Tooltip } from "./components/explain";
import {
  IconLoop,
  IconClaims,
  IconReview,
  IconGrounding,
  IconCorpus,
  IconSessions,
  IconInfra,
  IconHelp,
  IconExternal,
} from "./components/icons";

const Glossary = lazy(() => import("./components/Glossary"));

type IconCmp = (p: { size?: number; className?: string }) => React.ReactNode;
type NavItem = { to: string; label: string; Icon: IconCmp };

// Fallback nav — used when GET /console/modules fails, is empty, or hasn't
// resolved yet. The console MUST always render a nav, even against an old
// daemon that predates the manifest endpoint (fail-soft is mandatory).
const FALLBACK_NAV: NavItem[] = [
  { to: "/", label: "Loop", Icon: IconLoop },
  { to: "/claims", label: "Claims", Icon: IconClaims },
  { to: "/review", label: "Review", Icon: IconReview },
  { to: "/grounding", label: "Grounding", Icon: IconGrounding },
  { to: "/corpus", label: "Corpus", Icon: IconCorpus },
  { to: "/sessions", label: "Sessions", Icon: IconSessions },
];

// Manifest icon key -> component. Unknown keys fall back to IconLoop so a
// forward-compatible daemon (new module, old console build) never renders a
// blank nav item.
const ICON_MAP: Record<string, IconCmp> = {
  loop: IconLoop,
  claims: IconClaims,
  review: IconReview,
  grounding: IconGrounding,
  corpus: IconCorpus,
  sessions: IconSessions,
  infra: IconInfra,
};

interface ConsoleModule {
  id: string;
  title: string;
  icon: string;
  route: string;
  panels: string[];
}

function useNav(): NavItem[] {
  const { data } = useQuery({
    queryKey: ["console_modules"],
    queryFn: () => api.get<{ ok: boolean; modules: ConsoleModule[] }>("/console/modules"),
    staleTime: 5 * 60 * 1000,
    retry: 1,
  });
  const modules = data?.modules;
  if (!modules || modules.length === 0) return FALLBACK_NAV;
  return modules.map((m) => ({
    to: m.route,
    label: m.title,
    Icon: ICON_MAP[m.icon] ?? IconLoop,
  }));
}

function HealthDot() {
  const { data } = useQuery({
    queryKey: ["fail_mode_check"],
    queryFn: () => api.get<HealthCheck>("/fail_mode_check"),
    refetchInterval: 15000,
  });
  let tone = "#a1a1aa";
  let label = "…";
  if (data) {
    const bad = data.checks.filter(
      (c) => c.status !== "ok" && c.status !== "not_applicable",
    );
    const crit = bad.some((c) => c.severity === "critical");
    tone = crit ? "#ef4444" : bad.length ? "#f59e0b" : "#22c55e";
    label = bad.length ? `${bad.length} flag${bad.length > 1 ? "s" : ""}` : "healthy";
  }
  return (
    <Tooltip label="Engine fail-mode self-check. Green = all classes healthy.">
      <div className="flex items-center gap-1.5 text-[11px] text-[var(--color-muted)]">
        <span className="inline-block h-2 w-2 rounded-full" style={{ background: tone }} />
        {label}
      </div>
    </Tooltip>
  );
}

// Read/write the ?glossary=1 flag so the drawer is deep-linkable and shareable.
function useGlossary(): [boolean, (open: boolean) => void] {
  const navigate = useNavigate();
  const search = useRouterState({ select: (s) => s.location.search }) as Record<
    string,
    unknown
  >;
  const open = search?.glossary === "1" || search?.glossary === 1;
  const set = (v: boolean) =>
    navigate({
      to: ".",
      search: (prev: Record<string, string | number | undefined>) => {
        const next: Record<string, string | number | undefined> = { ...prev };
        if (v) next.glossary = "1";
        else delete next.glossary;
        return next;
      },
      replace: true,
    });
  return [open, set];
}

function isActive(path: string, to: string) {
  const target = to === "/" ? "/console/" : `/console${to}`;
  if (to === "/") return path === "/console" || path === "/console/";
  return path.startsWith(target);
}

export function RootLayout() {
  const path = useRouterState({ select: (s) => s.location.pathname });
  const [glossaryOpen, setGlossary] = useGlossary();
  const nav = useNav();

  // Embed protocol: announce ready + start height reporting once mounted.
  useEffect(() => {
    if (IS_EMBED) {
      const view = nav.find((n) => isActive(path, n.to))?.to ?? path;
      postReady(view);
      installHeightReporter();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="flex min-h-full flex-col">
      {!IS_EMBED && (
        <header className="sticky top-0 z-30 flex items-center gap-4 border-b border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-2.5 sm:gap-6 sm:px-5">
          <Link to="/" className="flex items-center gap-2">
            <span
              className="inline-block h-3 w-3 rounded-[3px]"
              style={{ background: "var(--color-brand)" }}
            />
            <span className="text-[13px] font-semibold tracking-tight">crag Anchor</span>
            <span className="hidden text-[11px] text-[var(--color-muted)] sm:inline">
              console
            </span>
          </Link>
          {/* Desktop nav — hidden on mobile (bottom tab bar takes over). */}
          <nav className="hidden items-center gap-1 md:flex">
            {nav.map((n) => {
              const act = isActive(path, n.to);
              return (
                <Link
                  key={n.to}
                  to={n.to}
                  className="flex items-center gap-1.5 rounded-[7px] px-2.5 py-1 text-[13px] transition-colors"
                  style={{
                    color: act ? "var(--color-text)" : "var(--color-muted)",
                    background: act ? "var(--color-surface-2)" : "transparent",
                  }}
                >
                  <n.Icon size={14} />
                  {n.label}
                </Link>
              );
            })}
          </nav>
          <div className="ml-auto flex items-center gap-3">
            <HealthDot />
            <button
              onClick={() => setGlossary(true)}
              aria-label="open glossary"
              className="flex h-8 w-8 items-center justify-center rounded-[7px] border border-[var(--color-border)] text-[var(--color-muted)] transition-colors hover:text-[var(--color-text)]"
            >
              <IconHelp size={15} />
            </button>
          </div>
        </header>
      )}

      <main
        className={
          "mx-auto w-full max-w-[1400px] flex-1 px-4 py-5 sm:px-5 " +
          (IS_EMBED ? "" : "pb-24 md:pb-5")
        }
      >
        <Outlet />
      </main>

      {/* Mobile bottom tab bar — 6 icons + labels, safe-area padded. */}
      {!IS_EMBED && (
        <nav className="fixed inset-x-0 bottom-0 z-30 flex border-t border-[var(--color-border)] bg-[var(--color-surface)] pb-safe md:hidden">
          {nav.map((n) => {
            const act = isActive(path, n.to);
            return (
              <Link
                key={n.to}
                to={n.to}
                className="flex flex-1 flex-col items-center gap-0.5 py-2 text-[10px]"
                style={{ color: act ? "var(--color-focus)" : "var(--color-muted)" }}
              >
                <n.Icon size={18} />
                {n.label}
              </Link>
            );
          })}
        </nav>
      )}

      {/* Embed footer badge. */}
      {IS_EMBED && (
        <footer className="flex items-center justify-center gap-1.5 border-t border-[var(--color-border)] bg-[var(--color-surface)] py-1.5 text-[10px] text-[var(--color-muted)]">
          <a
            href="https://crag.sh"
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1 hover:text-[var(--color-text)]"
          >
            powered by crag
            <IconExternal size={10} />
          </a>
        </footer>
      )}

      {glossaryOpen && (
        <Suspense fallback={null}>
          <Glossary onClose={() => setGlossary(false)} />
        </Suspense>
      )}
    </div>
  );
}
