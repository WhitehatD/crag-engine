// Infra — the ops overlay's page shell. This file ships in the PUBLIC console
// (harmless: the /infra/* endpoints simply don't exist there, so every query
// below fails soft to an empty state). On the private ops daemon
// (brain/apps/daemon/ops_infra.py) the same three endpoints are live and this
// page renders real data. That split — nav from the manifest, page shell in
// core, data from the overlay — is the module seam described in
// infra-playbook docs/system-integration-map.md §3.
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { Section, Table, Row, Cell, RefreshBar, truncate } from "../components/primitives";
import { ViewHeader, TeachingEmpty, Skeleton } from "../components/explain";

interface StackService {
  id: string;
  label: string;
  status: string;
  detail?: string;
  stats?: unknown;
}

interface StackResponse {
  ok: boolean;
  services: StackService[];
  generated_at: string;
}

interface CostByProject {
  project: string;
  tokens_in: number;
  tokens_out: number;
  sessions: number;
}

interface CostsResponse {
  ok: boolean;
  days: number;
  by_project: CostByProject[];
  generated_at: string;
}

interface InfraSessionRow {
  id: number;
  project: string | null;
  date: string | null;
  accomplished: string | null;
  next_steps: string | null;
  created_at: string | null;
}

interface SessionsResponse {
  ok: boolean;
  sessions: InfraSessionRow[];
  generated_at: string;
}

function StackDot({ status }: { status: string }) {
  const up = status === "up";
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className="inline-block h-2 w-2 rounded-full"
        style={{ background: up ? "#22c55e" : "#ef4444" }}
        aria-hidden
      />
      <span className="num text-[12px]" style={{ color: up ? "#22c55e" : "#ef4444" }}>
        {status}
      </span>
    </span>
  );
}

function StackSection() {
  const query = useQuery({
    queryKey: ["infra_stack"],
    queryFn: () => api.get<StackResponse>("/infra/stack"),
    retry: false,
    refetchInterval: 30000,
  });
  const { data, isLoading, isError } = query;
  return (
    <Section
      title="Stack services"
      right={
        <RefreshBar
          updatedAt={query.dataUpdatedAt}
          isFetching={query.isFetching}
          onRefresh={() => query.refetch()}
        />
      }
    >
      {isLoading && <Skeleton rows={3} />}
      {(isError || (data && data.services.length === 0)) && !isLoading && (
        <TeachingEmpty title="No stack services reported">
          This panel probes the laptop-local stack (watchdog, headroom proxy,
          model router). It stays empty when the `/infra/stack` endpoint isn't
          mounted on this daemon.
        </TeachingEmpty>
      )}
      {data && data.services.length > 0 && (
        <Table head={["service", "status", "detail"]}>
          {data.services.map((s) => (
            <Row key={s.id}>
              <Cell>{s.label}</Cell>
              <Cell>
                <StackDot status={s.status} />
              </Cell>
              <Cell>{s.detail ? truncate(s.detail, 70) : "—"}</Cell>
            </Row>
          ))}
        </Table>
      )}
    </Section>
  );
}

function CostsSection() {
  const query = useQuery({
    queryKey: ["infra_costs"],
    queryFn: () => api.get<CostsResponse>("/infra/costs?days=7"),
    retry: false,
    refetchInterval: 60000,
  });
  const { data, isLoading, isError } = query;
  return (
    <Section title="Costs by project (7d)">
      {isLoading && <Skeleton rows={3} />}
      {(isError || (data && data.by_project.length === 0)) && !isLoading && (
        <TeachingEmpty title="No cost data reported">
          Aggregates the token ledger by project over the last 7 days. Empty
          when `/infra/costs` isn't mounted on this daemon, or no sessions have
          been logged yet.
        </TeachingEmpty>
      )}
      {data && data.by_project.length > 0 && (
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
      )}
    </Section>
  );
}

function SessionsSection() {
  const query = useQuery({
    queryKey: ["infra_sessions"],
    queryFn: () => api.get<SessionsResponse>("/infra/sessions?limit=10"),
    retry: false,
    refetchInterval: 60000,
  });
  const { data, isLoading, isError } = query;
  return (
    <Section title="Recent sessions">
      {isLoading && <Skeleton rows={4} />}
      {(isError || (data && data.sessions.length === 0)) && !isLoading && (
        <TeachingEmpty title="No sessions reported">
          The last 10 session-diary rows across all projects. Empty when
          `/infra/sessions` isn't mounted on this daemon, or nothing has been
          logged yet.
        </TeachingEmpty>
      )}
      {data && data.sessions.length > 0 && (
        <Table head={["date", "project", "accomplished"]}>
          {data.sessions.map((s) => (
            <Row key={s.id}>
              <Cell mono>{s.date ?? "—"}</Cell>
              <Cell>{s.project ?? "—"}</Cell>
              <Cell>{truncate(s.accomplished ?? "", 90)}</Cell>
            </Row>
          ))}
        </Table>
      )}
    </Section>
  );
}

export default function Infra() {
  // If ALL three probes fail, this is almost certainly the public engine (the
  // ops routes simply aren't mounted) — collapse to one calm, explanatory
  // empty state instead of three near-identical "not found" panels.
  const stack = useQuery({
    queryKey: ["infra_stack_gate"],
    queryFn: () => api.get<StackResponse>("/infra/stack"),
    retry: false,
  });
  const costs = useQuery({
    queryKey: ["infra_costs_gate"],
    queryFn: () => api.get<CostsResponse>("/infra/costs?days=7"),
    retry: false,
  });
  const sessions = useQuery({
    queryKey: ["infra_sessions_gate"],
    queryFn: () => api.get<SessionsResponse>("/infra/sessions?limit=10"),
    retry: false,
  });
  const settled = !stack.isLoading && !costs.isLoading && !sessions.isLoading;
  const allFailed = stack.isError && costs.isError && sessions.isError;

  return (
    <div className="space-y-4">
      <ViewHeader
        id="infra"
        title="Infra"
        subtitle="Operator-only overlay — laptop stack health, spend by project, recent sessions."
        about={
          <>
            Infra is an <b>ops overlay module</b>: the nav entry and this page
            shell live in the public console, but the data behind it (
            <code className="num">/infra/stack</code>,{" "}
            <code className="num">/infra/costs</code>,{" "}
            <code className="num">/infra/sessions</code>) is only mounted by
            the private operator daemon. On the public engine this page is
            intentionally empty.
          </>
        }
      />
      {settled && allFailed ? (
        <Section>
          <TeachingEmpty title="Infra module is available on the operator instance">
            This build of crag isn't running the ops overlay, so the{" "}
            <code className="num">/infra/*</code> endpoints aren't mounted.
            Nothing is broken — this page renders real stack health, cost, and
            session data on an instance that has the private overlay enabled.
          </TeachingEmpty>
        </Section>
      ) : (
        <>
          <StackSection />
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <CostsSection />
            <SessionsSection />
          </div>
        </>
      )}
    </div>
  );
}
