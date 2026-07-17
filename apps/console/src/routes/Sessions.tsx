import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../lib/api";
import {
  Section,
  StatCard,
  Table,
  Row,
  Cell,
  Chip,
  Sparkline,
  Select,
  RefreshBar,
  truncate,
} from "../components/primitives";
import { ViewHeader, TeachingEmpty, Skeleton } from "../components/explain";
import type { CostReport, Slo } from "../lib/types";

interface SessionRow {
  id: number;
  project: string;
  date: string;
  accomplished: string | null;
  commits_count: number | null;
  files_changed_count: number | null;
  wall_time_sec: number | null;
}

function CostTiles() {
  const { data } = useQuery({
    queryKey: ["cost_report"],
    queryFn: () => api.get<CostReport>("/lifecycle/cost_report?days=7"),
    refetchInterval: 30000,
  });
  const t = data?.totals;
  const trend = (data?.trend ?? []).slice().reverse();
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatCard label="Sessions 7d" value={(t?.sessions ?? 0).toLocaleString()} />
        <StatCard
          label="Tokens in 7d"
          value={`${(((t?.total_in ?? 0) / 1_000_000)).toFixed(1)}M`}
          tone="brand"
        />
        <StatCard
          label="Tokens out 7d"
          value={`${(((t?.total_out ?? 0) / 1_000)).toFixed(0)}K`}
        />
        <StatCard
          label="Wall time 7d"
          value={`${(((t?.total_wall_sec ?? 0) / 3600)).toFixed(1)}h`}
        />
      </div>
      <Section title="Token trend (7d)">
        <div className="flex items-center gap-4">
          <Sparkline
            values={trend.map((d) => d.tokens_in)}
            color="#22c55e"
            width={220}
            height={40}
          />
          <div className="num text-[12px] text-[var(--color-muted)]">
            {trend.map((d) => (
              <span key={d.day} className="mr-3">
                {d.day.slice(5)}: {(d.tokens_in / 1_000_000).toFixed(1)}M
              </span>
            ))}
          </div>
        </div>
      </Section>
      <Section title="By project (7d)">
        {data ? (
          <Table head={["project", "sessions", "tokens in", "tokens out"]}>
            {data.by_project.map((p) => (
              <Row key={p.project}>
                <Cell>{p.project}</Cell>
                <Cell mono>{p.sessions}</Cell>
                <Cell mono>{(p.tokens_in / 1_000_000).toFixed(2)}M</Cell>
                <Cell mono>{(p.tokens_out / 1_000).toFixed(0)}K</Cell>
              </Row>
            ))}
          </Table>
        ) : (
          <Skeleton rows={4} />
        )}
      </Section>
    </div>
  );
}

function SloTile() {
  const { data } = useQuery({
    queryKey: ["slo"],
    queryFn: () => api.get<Slo>("/query/slo"),
    refetchInterval: 30000,
  });
  if (!data) return null;
  return (
    <Section title="SLOs">
      <Table head={["sli", "value", "target", "status"]}>
        {data.slis.map((s) => (
          <Row key={s.name}>
            <Cell>{s.name}</Cell>
            <Cell mono>
              {s.value}
              {s.unit ? ` ${s.unit}` : ""}
            </Cell>
            <Cell mono>{s.target ?? "—"}</Cell>
            <Cell>
              {s.status && (
                <Chip fg={s.status === "OK" ? "#22c55e" : "#ef4444"}>{s.status}</Chip>
              )}
            </Cell>
          </Row>
        ))}
      </Table>
    </Section>
  );
}

function RecallStatsTile() {
  const { data } = useQuery({
    queryKey: ["recall_stats"],
    queryFn: () =>
      api.get<{
        hot_insights: { id: number; content: string; hits: number }[];
      }>("/recall_stats?days=7"),
    refetchInterval: 60000,
  });
  return (
    <Section title="Hot insights (7d recall)">
      {!data ? (
        <Skeleton rows={4} />
      ) : data.hot_insights.length === 0 ? (
        <TeachingEmpty title="No recalls in window">
          This ranks the insights recalled most in the last 7 days — the engine's
          hot working set. It fills as agents query memory during sessions.
        </TeachingEmpty>
      ) : (
        <Table head={["id", "hits", "content"]}>
          {data.hot_insights.slice(0, 10).map((h) => (
            <Row key={h.id}>
              <Cell mono>{h.id}</Cell>
              <Cell mono>{h.hits}</Cell>
              <Cell>{truncate(h.content, 90)}</Cell>
            </Row>
          ))}
        </Table>
      )}
    </Section>
  );
}

function SessionsTable() {
  // Project list is DATA-DERIVED (never a hardcoded roster — a public repo must
  // not bake in an operator's project/client names). Sourced from the token
  // ledger's per-project breakdown; the console only ever knows the projects
  // that actually have recorded sessions in the connected backend.
  const { data: projectsData } = useQuery({
    queryKey: ["cost_report_projects"],
    queryFn: () =>
      api.get<{ by_project: { project: string }[] }>(
        "/lifecycle/cost_report?days=90",
      ),
    refetchInterval: 300000,
  });
  const projects = (projectsData?.by_project ?? [])
    .map((p) => p.project)
    .filter(Boolean);
  const [project, setProject] = useState<string>("");
  const active = project || projects[0] || "";
  const { data, isLoading } = useQuery({
    queryKey: ["sessions", active],
    enabled: active !== "",
    queryFn: () =>
      api.get<{ sessions: SessionRow[] }>(
        `/lifecycle/session/get${api.qs({ project: active, limit: 20 })}`,
      ),
    refetchInterval: 30000,
  });
  return (
    <Section
      title="Recent sessions"
      right={
        <Select
          value={active}
          onChange={setProject}
          options={projects.map((p) => ({ value: p, label: p }))}
        />
      }
    >
      {isLoading && <Skeleton rows={4} />}
      {data && data.sessions.length === 0 && !isLoading && (
        <TeachingEmpty title="No sessions for this project">
          Sessions are logged at the end of an agent's work — what was
          accomplished, files changed, commits, and duration. Pick another project
          or run a session to populate this.
        </TeachingEmpty>
      )}
      {data && data.sessions.length > 0 && (
        <Table head={["date", "commits", "files", "wall", "accomplished"]}>
          {data.sessions.map((s) => (
            <Row key={s.id}>
              <Cell mono>{s.date}</Cell>
              <Cell mono>{s.commits_count ?? "—"}</Cell>
              <Cell mono>{s.files_changed_count ?? "—"}</Cell>
              <Cell mono>
                {s.wall_time_sec ? `${Math.round(s.wall_time_sec / 60)}m` : "—"}
              </Cell>
              <Cell>{truncate(s.accomplished ?? "", 100)}</Cell>
            </Row>
          ))}
        </Table>
      )}
    </Section>
  );
}

export default function Sessions() {
  const cost = useQuery({
    queryKey: ["cost_report"],
    queryFn: () => api.get<CostReport>("/lifecycle/cost_report?days=7"),
    refetchInterval: 30000,
  });
  return (
    <div className="space-y-5">
      <ViewHeader
        id="sessions"
        title="Sessions"
        subtitle="Operational telemetry — cost, SLOs, recall efficiency, and session history."
        right={
          <RefreshBar
            updatedAt={cost.dataUpdatedAt}
            isFetching={cost.isFetching}
            onRefresh={() => cost.refetch()}
          />
        }
        about={
          <>
            This view is the operator's cockpit for the engine's running cost and
            health. It reports token spend and session counts, service-level
            objectives, how often memory recalls actually change an agent's
            approach, and a per-project log of what recent sessions accomplished.
            Use it to see whether the loop is paying for itself.
          </>
        }
      />
      <CostTiles />
      <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
        <SloTile />
        <RecallStatsTile />
      </div>
      <SessionsTable />
    </div>
  );
}
