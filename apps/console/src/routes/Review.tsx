import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { api } from "../lib/api";
import { useUrlState, useUrlNumber } from "../lib/urlstate";
import {
  Section,
  Table,
  Row,
  Cell,
  Chip,
  TierChip,
  Drawer,
  Btn,
  Select,
  RefreshBar,
  truncate,
  ageOf,
} from "../components/primitives";
import { ViewHeader, TeachingEmpty, Skeleton, Defined } from "../components/explain";
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
      {isLoading && <Skeleton rows={3} />}
      {e && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-2">
            <TierChip tier={e.tier} />
            <Chip>{e.source}</Chip>
            {e.project && <Chip>{e.project}</Chip>}
            {e.reason && <Chip fg="#f59e0b">{e.reason}</Chip>}
          </div>
          <p className="text-[13px] leading-relaxed">{payloadContent(e)}</p>

          {data?.matched_rule && (
            <div className="rounded-[7px] border border-[var(--color-border)] p-2 text-[12px]">
              <div className="mb-1 text-[11px] uppercase tracking-wide text-[var(--color-muted)]">
                matched policy rule — why this landed in its tier
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
            <b>accept</b> promotes this into the corpus as an insight · <b>reject</b>{" "}
            drops it (memory-only) · <b>defer</b> leaves it pending. actor: {ACTOR}
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
  const { data, isLoading } = useQuery({
    queryKey: ["dispo_policy"],
    queryFn: () => api.get<PolicyResponse>("/disposition/policy"),
  });
  return (
    <Section title="Disposition policy">
      {isLoading && <Skeleton rows={4} />}
      {data && data.rules.length === 0 && !isLoading && (
        <TeachingEmpty title="No policy rules">
          The disposition policy maps captured items to tiers. With no rules
          defined, everything falls through to the default tier. Rules are seeded
          server-side.
        </TeachingEmpty>
      )}
      {data && data.rules.length > 0 && (
        <Table head={["reason prefix", "source", "type", "tier", "default action", "deadline"]}>
          {data.rules.map((r, i) => (
            <Row key={i}>
              <Cell mono>{r.reason_prefix ?? "* (wildcard)"}</Cell>
              <Cell>{r.source ?? "—"}</Cell>
              <Cell>{r.type ?? "—"}</Cell>
              <Cell>
                <TierChip tier={r.tier} />
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
  const [tier, setTier] = useUrlState("tier");
  const [openId, setOpenId] = useUrlNumber("staging", -1);
  const query = useQuery({
    queryKey: ["dispo_list", tier],
    queryFn: () =>
      api.get<DispositionListResponse>(
        `/disposition/list${api.qs({ tier, status: "pending", limit: 200 })}`,
      ),
    refetchInterval: 15000,
  });
  const { data, isLoading } = query;
  const drain = useMutation({
    mutationFn: () => api.post<{ ok: boolean }>("/disposition/drain", {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["dispo_list"] }),
  });

  // Keyboard navigation: j/k move a highlight through the rows, Enter opens the
  // triage panel for the highlighted entry. Focus lives in a ref cursor.
  const cursor = useRef(0);
  const entries = data?.entries ?? [];
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      if (openId >= 0) return; // panel captures keys itself (Escape closes)
      if (ev.target && (ev.target as HTMLElement).tagName === "INPUT") return;
      if (entries.length === 0) return;
      if (ev.key === "j") {
        cursor.current = Math.min(cursor.current + 1, entries.length - 1);
        highlight();
      } else if (ev.key === "k") {
        cursor.current = Math.max(cursor.current - 1, 0);
        highlight();
      } else if (ev.key === "Enter") {
        setOpenId(entries[cursor.current].id);
      }
    };
    const highlight = () => {
      document
        .querySelectorAll("[data-triage-row]")
        .forEach((el, i) =>
          el.setAttribute("data-cursor", i === cursor.current ? "1" : "0"),
        );
    };
    window.addEventListener("keydown", onKey);
    highlight();
    return () => window.removeEventListener("keydown", onKey);
  }, [entries, openId, setOpenId]);

  const byTier = data?.by_tier ?? {};
  return (
    <Section
      title="Proposal ledger"
      right={
        <div className="flex flex-wrap items-center gap-2">
          <RefreshBar
            updatedAt={query.dataUpdatedAt}
            isFetching={query.isFetching}
            onRefresh={() => query.refetch()}
          />
          <span className="num text-[11px] text-[var(--color-muted)]">
            <Defined token="t0">T0 {byTier.t0 ?? 0}</Defined> ·{" "}
            <Defined token="t1">T1 {byTier.t1 ?? 0}</Defined> ·{" "}
            <Defined token="t2">T2 {byTier.t2 ?? 0}</Defined>
          </span>
          <Select value={tier} onChange={setTier} options={TIER_OPTS} />
          <Btn onClick={() => drain.mutate()} disabled={drain.isPending}>
            drain due
          </Btn>
        </div>
      }
    >
      <p className="mb-2 text-[11px] text-[var(--color-muted)]">
        Keyboard: <span className="num">j</span>/<span className="num">k</span> to
        move · <span className="num">Enter</span> to triage · <span className="num">Esc</span>{" "}
        to close.
      </p>
      {isLoading && <Skeleton />}
      {data && entries.length === 0 && !isLoading && (
        <TeachingEmpty title="Ledger empty — nothing pending">
          This is the capture → disposition stage of the loop. Agent failures and
          lessons land here as proposals, sorted into tiers (T0 auto, T1 agent, T2
          human). An empty ledger means every captured item has been triaged.
        </TeachingEmpty>
      )}
      {data && entries.length > 0 && (
        <Table head={["id", "tier", "source", "project", "reason", "content", "age", "deadline"]}>
          {entries.map((e) => (
            <Row key={e.id} onClick={() => setOpenId(e.id)}>
              <Cell mono>
                <span data-triage-row data-cursor="0">
                  {e.id}
                </span>
              </Cell>
              <Cell>
                <TierChip tier={e.tier} />
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
      {openId >= 0 && (
        <TriagePanel
          id={openId}
          onClose={() => setOpenId(-1)}
          onResolved={() => qc.invalidateQueries({ queryKey: ["dispo_list"] })}
        />
      )}
    </Section>
  );
}

export default function Review() {
  const [tab, setTab] = useUrlState("tab", "ledger");
  return (
    <div className="space-y-4">
      <ViewHeader
        id="review"
        title="Review"
        subtitle="Triage captured proposals — the disposition stage of the loop."
        about={
          <>
            When an agent hits a failure or learns a lesson, it is <b>captured</b>{" "}
            into this staging ledger. The disposition engine sorts each into a{" "}
            <b>tier</b>: <b>T0</b> auto-accepted, <b>T1</b> an agent may accept,{" "}
            <b>T2</b> needs a human. Accepting promotes the proposal into the
            Corpus as an insight; rejecting keeps it only as a memory record. Open
            a row to see its matched policy rule and act on it.
          </>
        }
      />
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
