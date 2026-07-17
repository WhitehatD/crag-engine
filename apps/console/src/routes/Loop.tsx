import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { Section, StatCard, Chip, ageOf } from "../components/primitives";
import type {
  Stats,
  GroundStats,
  DispositionListResponse,
  HealthCheck,
  EngineEvent,
} from "../lib/types";

function useStats() {
  return useQuery({
    queryKey: ["stats"],
    queryFn: () => api.get<Stats>("/stats"),
    refetchInterval: 10000,
  });
}

// The flywheel, live: capture staged -> dispositions pending -> claims by verdict
// -> compile-eligible principles -> distilled rules adopted. Each stage links to
// its view.
function Pipeline() {
  const ground = useQuery({
    queryKey: ["ground_stats"],
    queryFn: () => api.get<GroundStats>("/ground/stats"),
    refetchInterval: 10000,
  });
  // NOTE: `count` reflects the returned page, not the queue total — fetch with
  // the max limit so the pending number is the real ledger depth.
  const dispo = useQuery({
    queryKey: ["dispo_all"],
    queryFn: () =>
      api.get<DispositionListResponse>("/disposition/list?status=pending&limit=500"),
    refetchInterval: 10000,
  });
  const principles = useQuery({
    queryKey: ["compile_eligible"],
    queryFn: () =>
      api.get<{ principles: unknown[] }>(
        "/principles/export?compile_eligible=true",
      ),
    refetchInterval: 30000,
  });

  const cov = ground.data?.coverage_by_class ?? {};
  const verdicts = ground.data?.verdict_dist_last_24h ?? {};
  const pendingByTier = dispo.data?.by_tier ?? {};
  const pendingTotal = dispo.data?.count ?? 0;
  const claimTotal = cov._total ?? 0;

  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
      <StatCard
        label="Dispositions pending"
        value={pendingTotal.toLocaleString()}
        sub={
          <span className="num">
            {["t0", "t1", "t2"]
              .filter((t) => pendingByTier[t])
              .map((t) => `${t.toUpperCase()} ${pendingByTier[t]}`)
              .join("  ") || "none"}
          </span>
        }
        href="/console/review"
      />
      <StatCard
        label="Claims (classified)"
        value={claimTotal.toLocaleString()}
        sub={
          <span className="num">
            P1 {cov.P1 ?? 0} · P4 {cov.P4 ?? 0} · P5 {cov.P5 ?? 0}
          </span>
        }
        href="/console/claims"
      />
      <StatCard
        label="Grounded 24h"
        value={(ground.data?.jobs_done_last_24h ?? 0).toLocaleString()}
        sub={
          <span className="num">
            pass {verdicts.pass ?? 0} · fail {verdicts.fail ?? 0}
          </span>
        }
        href="/console/grounding"
        tone="brand"
      />
      <StatCard
        label="Flagged claims"
        value={
          (ground.data?.flagged_claims?.insights ?? 0) +
          (ground.data?.flagged_claims?.principles ?? 0)
        }
        sub="drifted, awaiting resolution"
        href="/console/grounding"
        tone="warn"
      />
      <StatCard
        label="Compile-eligible principles"
        value={(principles.data?.principles?.length ?? 0).toLocaleString()}
        sub="roll up fresh"
        href="/console/corpus"
        tone="brand"
      />
    </div>
  );
}

function HealthStrip() {
  const { data } = useQuery({
    queryKey: ["fail_mode_check"],
    queryFn: () => api.get<HealthCheck>("/fail_mode_check"),
    refetchInterval: 15000,
  });
  if (!data) return null;
  return (
    <div className="flex flex-wrap gap-2">
      {data.checks.map((c) => {
        const ok = c.status === "ok";
        const na = c.status === "not_applicable";
        const fg = ok ? "#22c55e" : na ? "#a1a1aa" : c.severity === "critical" ? "#ef4444" : "#f59e0b";
        return (
          <Chip key={c.class} fg={fg} title={c.detail}>
            {c.class}: {c.status}
          </Chip>
        );
      })}
    </div>
  );
}

function EventRail() {
  // Journal events: {type, ts, id?, project?, preview?}. Poll the full ring
  // (bounded server-side) and render newest first — the rail is a live feed,
  // populated as saves/arena/supersede/session events occur.
  const { data } = useQuery({
    queryKey: ["events_since"],
    queryFn: () =>
      api.get<{ events: EngineEvent[] }>(`/events/since${api.qs({ limit: 50 })}`),
    refetchInterval: 8000,
  });
  const events = (data?.events ?? []).slice(-30).reverse();
  return (
    <Section title="Live events">
      {events.length === 0 ? (
        <div className="text-[12px] text-[var(--color-muted)]">
          No events yet — the rail fills as the loop runs (saves, arena verdicts,
          supersedes, session ends).
        </div>
      ) : (
        <ul className="space-y-1.5">
          {events.map((e, i) => (
            <li key={i} className="flex items-start gap-2 text-[12px]">
              <span className="num shrink-0 text-[var(--color-muted)]">
                {ageOf((e.ts as string) ?? null)}
              </span>
              <Chip>{(e.type as string) ?? "event"}</Chip>
              <span className="text-[var(--color-text)]">
                {(e.preview as string) ??
                  (e.summary as string) ??
                  (e.id !== undefined ? `#${e.id}` : "")}
              </span>
              {e.project != null && <Chip>{String(e.project)}</Chip>}
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

export default function Loop() {
  const stats = useStats();
  return (
    <div className="space-y-5">
      <div className="grid grid-cols-3 gap-3">
        <StatCard
          label="Active insights"
          value={(stats.data?.insight_counts.active ?? 0).toLocaleString()}
          sub={`${stats.data?.insight_counts.total ?? 0} total`}
        />
        <StatCard
          label="Principles"
          value={(stats.data?.insight_counts.principles ?? 0).toLocaleString()}
        />
        <StatCard
          label="Uptime"
          value={`${Math.floor((stats.data?.uptime_seconds ?? 0) / 3600)}h`}
          sub={`db ${(((stats.data?.db_size_bytes ?? 0) / 1_048_576) | 0).toLocaleString()} MB`}
        />
      </div>

      <Section title="The loop">
        <Pipeline />
      </Section>

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
        <EventRail />
        <Section title="Fail-mode check">
          <HealthStrip />
        </Section>
      </div>
    </div>
  );
}
