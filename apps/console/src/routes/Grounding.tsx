import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useUrlState } from "../lib/urlstate";
import {
  Section,
  StatCard,
  Table,
  Row,
  Cell,
  Chip,
  ClassChip,
  Sparkline,
  Select,
  Btn,
  Empty,
  RefreshBar,
  truncate,
  ageOf,
} from "../components/primitives";
import { ViewHeader, TeachingEmpty, Skeleton } from "../components/explain";
import type { GroundStats, GroundJob } from "../lib/types";

interface Economics {
  ok: boolean;
  budget: {
    calls_today: number;
    tokens_today: number;
    daily_budget_calls: number;
    calls_remaining: number;
  };
  today: { calls: number; est_cost_usd: number };
  spend_7d: { usd: number; calls: number };
  per_stage_7d: { stage: string; calls: number; est_cost_usd: number }[];
}

interface AuditResponse {
  ok: boolean;
  count: number;
  queue: {
    queue_id: number;
    claim_kind: string;
    claim_id: number;
    reason: string;
    project: string | null;
    snippet: string;
    falsifier: { kind: string | null; last_result: string | null };
  }[];
}

const JOB_STATUS_OPTS = [
  { value: "", label: "all statuses" },
  { value: "pending", label: "pending" },
  { value: "running", label: "running" },
  { value: "done", label: "done" },
  { value: "failed", label: "failed" },
];

function StatsRow() {
  const { data } = useQuery({
    queryKey: ["ground_stats"],
    queryFn: () => api.get<GroundStats>("/ground/stats"),
    refetchInterval: 10000,
  });
  const eco = useQuery({
    queryKey: ["ground_eco"],
    queryFn: () => api.get<Economics>("/ground/economics"),
    refetchInterval: 20000,
  });
  const qs = data?.queue_by_status ?? {};
  const cov = data?.coverage_by_class ?? {};
  const pass = data?.pass_rate_by_class ?? {};
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatCard
          label="Queue pending"
          value={(qs.pending ?? 0).toLocaleString()}
          sub={
            <span className="num">
              running {qs.running ?? 0} · done {qs.done ?? 0}
            </span>
          }
        />
        <StatCard
          label="Done 24h"
          value={(data?.jobs_done_last_24h ?? 0).toLocaleString()}
          tone="brand"
          sub={`oldest pending ${ageOf(
            data ? new Date(Date.now() - data.oldest_pending_age_sec * 1000).toISOString() : null,
          )}`}
        />
        <StatCard
          label="Flagged claims"
          value={
            (data?.flagged_claims?.insights ?? 0) +
            (data?.flagged_claims?.principles ?? 0)
          }
          tone="warn"
        />
        <StatCard
          label="LLM spend 7d"
          value={`$${(eco.data?.spend_7d?.usd ?? 0).toFixed(2)}`}
          sub={
            <span className="num">
              {eco.data?.budget.calls_today ?? 0} calls today
            </span>
          }
        />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Section title="Coverage by class">
          <Table head={["class", "claims", "pass rate"]}>
            {["P1", "P2", "P3", "P4", "P5"].map((k) => (
              <Row key={k}>
                <Cell>
                  <ClassChip pclass={k} />
                </Cell>
                <Cell mono>{cov[k] ?? 0}</Cell>
                <Cell mono>
                  {pass[k] !== undefined ? `${Math.round(pass[k] * 100)}%` : "—"}
                </Cell>
              </Row>
            ))}
          </Table>
        </Section>
        <Section title="Spend by stage (7d)">
          {eco.data ? (
            <Table head={["stage", "calls", "cost"]}>
              {eco.data.per_stage_7d.map((s) => (
                <Row key={s.stage}>
                  <Cell>{s.stage}</Cell>
                  <Cell mono>{s.calls}</Cell>
                  <Cell mono>${s.est_cost_usd.toFixed(3)}</Cell>
                </Row>
              ))}
            </Table>
          ) : (
            <Empty label="loading…" />
          )}
        </Section>
      </div>
    </div>
  );
}

function VerdictSplit() {
  const { data } = useQuery({
    queryKey: ["ground_stats"],
    queryFn: () => api.get<GroundStats>("/ground/stats"),
    refetchInterval: 10000,
  });
  const v = data?.verdict_dist_last_24h ?? {};
  const order = ["pass", "fail", "uncertain", "null"];
  const vals = order.map((k) => v[k] ?? 0);
  return (
    <Section title="Verdict split (24h)">
      <div className="flex items-center gap-4">
        <Sparkline values={vals} color="#22d3ee" width={160} height={32} />
        <div className="flex flex-wrap gap-2">
          {order.map((k) => (
            <Chip
              key={k}
              fg={k === "pass" ? "#22c55e" : k === "fail" ? "#ef4444" : "#a1a1aa"}
            >
              {k} {v[k] ?? 0}
            </Chip>
          ))}
        </div>
      </div>
    </Section>
  );
}

function JobsTab() {
  const [status, setStatus] = useUrlState("jobstatus");
  const { data, isLoading } = useQuery({
    queryKey: ["ground_jobs", status],
    queryFn: () =>
      api.get<{ ok: boolean; jobs: GroundJob[] }>(
        `/ground/jobs${api.qs({ status, limit: 60 })}`,
      ),
    refetchInterval: 10000,
  });
  return (
    <Section
      title="Jobs"
      right={<Select value={status} onChange={setStatus} options={JOB_STATUS_OPTS} />}
    >
      {isLoading && <Skeleton rows={4} />}
      {data && data.jobs.length === 0 && !isLoading && (
        <TeachingEmpty title="No grounding jobs">
          Grounding jobs are queued when a claim is created or recalled. Each job
          runs the claim's falsifier against live reality and records a verdict.
          None match this status filter.
        </TeachingEmpty>
      )}
      {data && data.jobs.length > 0 && (
        <Table head={["id", "claim", "type", "status", "attempts", "enqueued", "error"]}>
          {data.jobs.map((j) => (
            <Row key={j.id}>
              <Cell mono>{j.id}</Cell>
              <Cell mono>
                {j.claim_kind}#{j.claim_id}
              </Cell>
              <Cell>{j.job_type}</Cell>
              <Cell>
                <Chip
                  fg={
                    j.status === "done"
                      ? "#22c55e"
                      : j.status === "failed"
                        ? "#ef4444"
                        : j.status === "running"
                          ? "#f59e0b"
                          : "#a1a1aa"
                  }
                >
                  {j.status}
                </Chip>
              </Cell>
              <Cell mono>{j.attempts}</Cell>
              <Cell mono>{ageOf(j.enqueued_at)}</Cell>
              <Cell>{j.last_error ? truncate(j.last_error, 40) : "—"}</Cell>
            </Row>
          ))}
        </Table>
      )}
    </Section>
  );
}

function AuditTab() {
  const { data, isLoading } = useQuery({
    queryKey: ["ground_audit"],
    queryFn: () => api.get<AuditResponse>("/ground/audit?limit=60"),
    refetchInterval: 20000,
  });
  return (
    <Section title="Audit queue — drifted claims">
      {isLoading && <Skeleton rows={3} />}
      {data && data.queue.length === 0 && !isLoading && (
        <TeachingEmpty title="Queue clear — no drifted claims">
          The groundskeeper flags claims whose falsifier failed or that aged past
          their freshness window. Nothing is drifting right now, so every claim's
          trust is current.
        </TeachingEmpty>
      )}
      {data && data.queue.length > 0 && (
        <Table head={["claim", "project", "reason", "falsifier", "snippet"]}>
          {data.queue.map((r) => (
            <Row key={r.queue_id}>
              <Cell mono>
                {r.claim_kind}#{r.claim_id}
              </Cell>
              <Cell>{r.project ?? "—"}</Cell>
              <Cell>
                <Chip fg="#f59e0b">{r.reason}</Chip>
              </Cell>
              <Cell mono>
                {r.falsifier?.kind ?? "none"}
                {r.falsifier?.last_result ? ` (${r.falsifier.last_result})` : ""}
              </Cell>
              <Cell>{truncate(r.snippet, 80)}</Cell>
            </Row>
          ))}
        </Table>
      )}
    </Section>
  );
}

interface Proposal {
  id: number;
  claim_kind: string;
  claim_id: number;
  verdict: string;
  proposed_action: string;
  prior_content: string | null;
  proposed_content: string | null;
}

function ProposalsTab() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["ground_proposals"],
    queryFn: () =>
      api.get<{ ok: boolean; proposals: Proposal[] }>(
        "/ground/proposals?status=pending&limit=30",
      ),
    refetchInterval: 20000,
  });
  const decide = useMutation({
    mutationFn: ({ id, decision }: { id: number; decision: string }) =>
      api.post<{ ok: boolean }>(`/ground/proposals/${id}/decide`, {
        decision,
        decided_by: "console-operator",
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ground_proposals"] }),
  });
  return (
    <Section title="Resolution proposals — human-gated">
      {isLoading && <Skeleton rows={3} />}
      {data && data.proposals.length === 0 && !isLoading && (
        <TeachingEmpty title="No pending proposals">
          When grounding detects a claim has drifted, it can propose a correction
          for review. An empty list means no automated corrections are waiting.
        </TeachingEmpty>
      )}
      <div className="space-y-3">
        {data?.proposals.map((p) => (
          <div
            key={p.id}
            className="rounded-[12px] border border-[var(--color-border)] p-3"
          >
            <div className="mb-1.5 flex items-center gap-2 text-[12px]">
              <Chip fg="#f59e0b">proposal #{p.id}</Chip>
              <span className="num">
                {p.claim_kind}#{p.claim_id}
              </span>
              <Chip fg={p.verdict === "fail" ? "#ef4444" : "#a1a1aa"}>
                verdict: {p.verdict}
              </Chip>
              <Chip>{p.proposed_action}</Chip>
              <div className="ml-auto flex gap-2">
                <Btn
                  tone="brand"
                  onClick={() => decide.mutate({ id: p.id, decision: "approve" })}
                  disabled={decide.isPending}
                >
                  approve
                </Btn>
                <Btn
                  tone="danger"
                  onClick={() => decide.mutate({ id: p.id, decision: "reject" })}
                  disabled={decide.isPending}
                >
                  reject
                </Btn>
              </div>
            </div>
            {p.prior_content && (
              <div className="text-[12px] text-[var(--color-muted)]">
                {truncate(p.prior_content, 180)}
              </div>
            )}
          </div>
        ))}
      </div>
    </Section>
  );
}

export default function Grounding() {
  // When arriving from the Loop's "flagged claims" card (?tab=audit) the drifted
  // audit queue floats to the top so the operator lands on exactly what they
  // clicked; otherwise the dashboard reads top-down (throughput first).
  const [tab] = useUrlState("tab");
  const auditFirst = tab === "audit";
  const stats = useQuery({
    queryKey: ["ground_stats"],
    queryFn: () => api.get<GroundStats>("/ground/stats"),
    refetchInterval: 10000,
  });
  return (
    <div className="space-y-5">
      <ViewHeader
        id="grounding"
        title="Grounding"
        subtitle="Machine-verifying claims against live reality — the trust engine."
        right={
          <RefreshBar
            updatedAt={stats.dataUpdatedAt}
            isFetching={stats.isFetching}
            onRefresh={() => stats.refetch()}
          />
        }
        about={
          <>
            Grounding is where trust is earned. Each claim's falsifier is run
            against the real world and the result sets its <b>verdict</b>:{" "}
            <b>fresh</b> (just verified), <b>aging</b>, <b>unverified</b>,{" "}
            <b>revalidating</b>, or <b>stale</b> (failed or aged out). P5 axiomatic
            claims are terminal and never probed. The stats show throughput and
            pass rates; the audit queue lists claims that drifted and need
            re-checking. Trust is how recently a claim was re-verified — not a
            number that only rises.
          </>
        }
      />
      <StatsRow />
      {auditFirst ? (
        <>
          <AuditTab />
          <VerdictSplit />
          <JobsTab />
          <ProposalsTab />
        </>
      ) : (
        <>
          <VerdictSplit />
          <div className="grid grid-cols-1 gap-5 xl:grid-cols-2">
            <JobsTab />
            <AuditTab />
          </div>
          <ProposalsTab />
        </>
      )}
    </div>
  );
}
