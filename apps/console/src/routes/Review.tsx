import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../lib/api";
import {
  Section,
  Table,
  Row,
  Cell,
  Chip,
  Drawer,
  Btn,
  Select,
  Empty,
  truncate,
  ageOf,
} from "../components/primitives";
import type { DispositionListResponse, DispositionEntry } from "../lib/types";

const TIER_OPTS = [
  { value: "", label: "all tiers" },
  { value: "t0", label: "T0 auto" },
  { value: "t1", label: "T1 agent" },
  { value: "t2", label: "T2 human" },
];

const ACTOR = "console-operator";

function payloadContent(entry: DispositionEntry): string {
  try {
    const p = JSON.parse(entry.payload);
    return p.content ?? entry.payload;
  } catch {
    return entry.payload;
  }
}

interface TriageResponse {
  ok: boolean;
  entry: DispositionEntry;
  matched_rule: {
    tier: string;
    default_action: string;
    deadline_hours: number;
  } | null;
}

function TriagePanel({
  id,
  onClose,
  onResolved,
}: {
  id: number;
  onClose: () => void;
  onResolved: () => void;
}) {
  const { data, isLoading } = useQuery({
    queryKey: ["triage", id],
    queryFn: () => api.get<TriageResponse>(`/disposition/triage/${id}`),
  });
  const resolve = useMutation({
    mutationFn: (action: string) =>
      api.post<{ ok: boolean; verdict?: string; error?: string }>(
        "/disposition/resolve",
        {
          staging_id: id,
          action,
          actor: ACTOR,
          capability: "human_approved",
        },
      ),
    onSuccess: () => {
      onResolved();
      onClose();
    },
  });

  const e = data?.entry;
  return (
    <Drawer open onClose={onClose} title={<span className="num">staging #{id}</span>}>
      {isLoading && <div className="text-[var(--color-muted)]">loading…</div>}
      {e && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-2">
            <Chip fg="#22d3ee">{e.tier?.toUpperCase()}</Chip>
            <Chip>{e.source}</Chip>
            {e.project && <Chip>{e.project}</Chip>}
            {e.reason && <Chip fg="#f59e0b">{e.reason}</Chip>}
          </div>
          <p className="text-[13px] leading-relaxed">{payloadContent(e)}</p>

          {data?.matched_rule && (
            <div className="rounded-[7px] border border-[var(--color-border)] p-2 text-[12px]">
              <div className="mb-1 text-[11px] uppercase tracking-wide text-[var(--color-muted)]">
                matched policy rule
              </div>
              <span className="num">
                tier {data.matched_rule.tier} · default{" "}
                {data.matched_rule.default_action} · deadline{" "}
                {data.matched_rule.deadline_hours}h
              </span>
            </div>
          )}

          {resolve.data && !resolve.data.ok && (
            <div className="text-[12px] text-[var(--color-amber)]">
              {resolve.data.verdict ?? resolve.data.error}
            </div>
          )}

          <div className="flex flex-wrap gap-2">
            <Btn tone="brand" onClick={() => resolve.mutate("accept")} disabled={resolve.isPending}>
              accept
            </Btn>
            <Btn tone="danger" onClick={() => resolve.mutate("reject")} disabled={resolve.isPending}>
              reject
            </Btn>
            <Btn onClick={() => resolve.mutate("defer")} disabled={resolve.isPending}>
              defer
            </Btn>
          </div>
          <p className="text-[11px] text-[var(--color-muted)]">
            actor: {ACTOR} · capability: human_approved
          </p>
        </div>
      )}
    </Drawer>
  );
}

interface PolicyResponse {
  ok: boolean;
  rules: {
    source: string | null;
    type: string | null;
    reason_prefix: string | null;
    tier: string;
    default_action: string;
    deadline_hours: number;
  }[];
}

function PolicyTab() {
  const { data } = useQuery({
    queryKey: ["dispo_policy"],
    queryFn: () => api.get<PolicyResponse>("/disposition/policy"),
  });
  return (
    <Section title="Disposition policy">
      {!data ? (
        <Empty label="loading…" />
      ) : (
        <Table head={["reason prefix", "source", "type", "tier", "default action", "deadline"]}>
          {data.rules.map((r, i) => (
            <Row key={i}>
              <Cell mono>{r.reason_prefix ?? "* (wildcard)"}</Cell>
              <Cell>{r.source ?? "—"}</Cell>
              <Cell>{r.type ?? "—"}</Cell>
              <Cell>
                <Chip fg="#22d3ee">{r.tier.toUpperCase()}</Chip>
              </Cell>
              <Cell>{r.default_action}</Cell>
              <Cell mono>{r.deadline_hours}h</Cell>
            </Row>
          ))}
        </Table>
      )}
    </Section>
  );
}

function StagingTab() {
  const qc = useQueryClient();
  const [tier, setTier] = useState("");
  const [openId, setOpenId] = useState<number | null>(null);
  const { data, isLoading } = useQuery({
    queryKey: ["dispo_list", tier],
    queryFn: () =>
      api.get<DispositionListResponse>(
        `/disposition/list${api.qs({ tier, status: "pending", limit: 200 })}`,
      ),
    refetchInterval: 15000,
  });
  const drain = useMutation({
    mutationFn: () => api.post<{ ok: boolean }>("/disposition/drain", {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["dispo_list"] }),
  });

  const byTier = data?.by_tier ?? {};
  return (
    <Section
      title="Proposal ledger"
      right={
        <div className="flex items-center gap-2">
          <span className="num text-[11px] text-[var(--color-muted)]">
            T0 {byTier.t0 ?? 0} · T1 {byTier.t1 ?? 0} · T2 {byTier.t2 ?? 0}
          </span>
          <Select value={tier} onChange={setTier} options={TIER_OPTS} />
          <Btn onClick={() => drain.mutate()} disabled={drain.isPending}>
            drain due
          </Btn>
        </div>
      }
    >
      {isLoading && <Empty label="loading…" />}
      {data && data.entries.length === 0 && !isLoading && (
        <Empty label="ledger empty — nothing pending" />
      )}
      {data && data.entries.length > 0 && (
        <Table head={["id", "tier", "source", "project", "reason", "content", "age", "deadline"]}>
          {data.entries.map((e) => (
            <Row key={e.id} onClick={() => setOpenId(e.id)}>
              <Cell mono>{e.id}</Cell>
              <Cell>
                <Chip fg="#22d3ee">{e.tier?.toUpperCase()}</Chip>
              </Cell>
              <Cell>{e.source}</Cell>
              <Cell>{e.project ?? "—"}</Cell>
              <Cell>
                {e.reason ? <Chip fg="#f59e0b">{truncate(e.reason, 24)}</Chip> : "—"}
              </Cell>
              <Cell>{truncate(payloadContent(e), 80)}</Cell>
              <Cell mono>{ageOf(e.created_at)}</Cell>
              <Cell mono>{e.deadline ? ageOf(e.deadline) : "—"}</Cell>
            </Row>
          ))}
        </Table>
      )}
      {openId !== null && (
        <TriagePanel
          id={openId}
          onClose={() => setOpenId(null)}
          onResolved={() => qc.invalidateQueries({ queryKey: ["dispo_list"] })}
        />
      )}
    </Section>
  );
}

export default function Review() {
  const [tab, setTab] = useState<"ledger" | "policy">("ledger");
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <Btn tone={tab === "ledger" ? "brand" : "default"} onClick={() => setTab("ledger")}>
          Ledger
        </Btn>
        <Btn tone={tab === "policy" ? "brand" : "default"} onClick={() => setTab("policy")}>
          Policy
        </Btn>
      </div>
      {tab === "ledger" ? <StagingTab /> : <PolicyTab />}
    </div>
  );
}
