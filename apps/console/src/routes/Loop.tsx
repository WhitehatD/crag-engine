import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import {
  Section,
  StatCard,
  Chip,
  Sparkline,
  RefreshBar,
  ageOf,
} from "../components/primitives";
import { ViewHeader, TeachingEmpty, Defined } from "../components/explain";
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
// its view with a filter preset so a click lands on exactly what the card counts.
function Pipeline() {
  const ground = useQuery({
    queryKey: ["ground_stats"],
    queryFn: () => api.get<GroundStats>("/ground/stats"),
    refetchInterval: 10000,
  });
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
        to="/review"
        hint="Captured items awaiting triage. Click to open the proposal ledger."
      />
      <StatCard
        label="Claims (classified)"
        value={claimTotal.toLocaleString()}
        sub={
          <span className="num">
            P1 {cov.P1 ?? 0} · P4 {cov.P4 ?? 0} · P5 {cov.P5 ?? 0}
          </span>
        }
        to="/claims"
        hint="Atomic claims decomposed from insights. Click to browse them."
      />
      <StatCard
        label="Grounded 24h"
        value={(ground.data?.jobs_done_last_24h ?? 0).toLocaleString()}
        sub={
          <span className="num">
            pass {verdicts.pass ?? 0} · fail {verdicts.fail ?? 0}
          </span>
        }
        to="/grounding"
        tone="brand"
        hint="Grounding runs completed in the last 24h. Click for the grounding dashboard."
      />
      <StatCard
        label="Flagged claims"
        value={
          (ground.data?.flagged_claims?.insights ?? 0) +
          (ground.data?.flagged_claims?.principles ?? 0)
        }
        sub="drifted, awaiting resolution"
        to="/grounding"
        search={{ tab: "audit" }}
        tone="warn"
        hint="Claims whose falsifier failed or that aged out. Click to open the audit queue."
      />
      <StatCard
        label="Compile-eligible principles"
        value={(principles.data?.principles?.length ?? 0).toLocaleString()}
        sub="roll up fresh"
        to="/corpus"
        search={{ tab: "principles" }}
        tone="brand"
        hint="Principles whose core claims are all fresh — these distill into governance."
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
  if (!data)
    return (
      <div className="text-[12px] text-[var(--color-muted)]">checking…</div>
    );
  return (
    <div className="flex flex-wrap gap-2">
      {data.checks.map((c) => {
        const ok = c.status === "ok";
        const na = c.status === "not_applicable";
        const fg = ok
          ? "#22c55e"
          : na
            ? "#a1a1aa"
            : c.severity === "critical"
              ? "#ef4444"
              : "#f59e0b";
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
  const { data } = useQuery({
    queryKey: ["events_since"],
    queryFn: () =>
      api.get<{ events: EngineEvent[] }>(`/events/since${api.qs({ limit: 50 })}`),
    refetchInterval: 8000,
  });
  const events = (data?.events ?? []).slice(-30).reverse();

  // Cheap 24h activity sparkline — bucket event timestamps into hourly counts.
  const buckets = new Array(24).fill(0);
  const now = Date.now();
  for (const e of data?.events ?? []) {
    const ts = e.ts ? Date.parse(String(e.ts)) : NaN;
    if (!Number.isNaN(ts)) {
      const hoursAgo = Math.floor((now - ts) / 3_600_000);
      if (hoursAgo >= 0 && hoursAgo < 24) buckets[23 - hoursAgo] += 1;
    }
  }
  const hasActivity = buckets.some((b) => b > 0);

  return (
    <Section
      title="Live events"
      right={
        hasActivity ? (
          <Sparkline values={buckets} color="#22d3ee" width={120} height={24} />
        ) : undefined
      }
    >
      {events.length === 0 ? (
        <TeachingEmpty title="No events yet">
          This rail is the engine's live journal. It fills as the loop runs —
          insights saved, arena verdicts, supersedes, and session ends all appear
          here newest-first.
        </TeachingEmpty>
      ) : (
        <ul className="space-y-1.5">
          {events.map((e, i) => (
            <li key={i} className="flex items-start gap-2 text-[12px]">
              <span className="num shrink-0 text-[var(--color-muted)]">
                {ageOf((e.ts as string) ?? null)}
              </span>
              <Defined token={(e.type as string) ?? ""}>
                <Chip>{(e.type as string) ?? "event"}</Chip>
              </Defined>
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
      <ViewHeader
        id="loop"
        title="Loop"
        subtitle="The closed loop, live — every stage from capture to distilled governance."
        right={
          <RefreshBar
            updatedAt={stats.dataUpdatedAt}
            isFetching={stats.isFetching}
            onRefresh={() => stats.refetch()}
          />
        }
        about={
          <>
            This is the engine's flywheel at a glance. Failures and lessons are{" "}
            <b>captured</b>, <b>triaged</b> into tiers, become <b>insights</b>,
            decompose into <b>claims</b>, get <b>grounded</b> against reality, and
            the verified patterns become <b>principles</b> that crag <b>distills</b>{" "}
            into enforced rules. Each stat card below counts one stage and clicks
            through to the view that owns it. The rail on the left is the live
            journal; the strip on the right is the engine's self-check.
          </>
        }
      />

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
