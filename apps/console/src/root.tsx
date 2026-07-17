import { Link, Outlet, useRouterState } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { api } from "./lib/api";
import type { HealthCheck } from "./lib/types";

const NAV = [
  { to: "/", label: "Loop" },
  { to: "/claims", label: "Claims" },
  { to: "/review", label: "Review" },
  { to: "/grounding", label: "Grounding" },
  { to: "/corpus", label: "Corpus" },
  { to: "/sessions", label: "Sessions" },
] as const;

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
    <div className="flex items-center gap-1.5 text-[11px] text-[var(--color-muted)]">
      <span
        className="inline-block h-2 w-2 rounded-full"
        style={{ background: tone }}
      />
      {label}
    </div>
  );
}

export function RootLayout() {
  const path = useRouterState({ select: (s) => s.location.pathname });
  const active = (to: string) =>
    to === "/console/" || to === "/console"
      ? path === "/console" || path === "/console/"
      : path.startsWith(to);

  return (
    <div className="flex min-h-full flex-col">
      <header className="flex items-center gap-6 border-b border-[var(--color-border)] bg-[var(--color-surface)] px-5 py-2.5">
        <div className="flex items-center gap-2">
          <span
            className="inline-block h-3 w-3 rounded-[3px]"
            style={{ background: "var(--color-brand)" }}
          />
          <span className="text-[13px] font-semibold tracking-tight">
            crag engine
          </span>
          <span className="text-[11px] text-[var(--color-muted)]">console</span>
        </div>
        <nav className="flex items-center gap-1">
          {NAV.map((n) => {
            const to = n.to === "/" ? "/console/" : `/console${n.to}`;
            return (
              <Link
                key={n.to}
                to={n.to}
                className="rounded-[7px] px-2.5 py-1 text-[13px] transition-colors"
                style={{
                  color: active(to)
                    ? "var(--color-text)"
                    : "var(--color-muted)",
                  background: active(to) ? "var(--color-surface-2)" : "transparent",
                }}
              >
                {n.label}
              </Link>
            );
          })}
        </nav>
        <div className="ml-auto">
          <HealthDot />
        </div>
      </header>
      <main className="mx-auto w-full max-w-[1400px] flex-1 px-5 py-5">
        <Outlet />
      </main>
    </div>
  );
}
