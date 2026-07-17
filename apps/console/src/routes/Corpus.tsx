import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { api } from "../lib/api";
import { useUrlState, useUrlNumber } from "../lib/urlstate";
import {
  Section,
  Table,
  Row,
  Cell,
  Chip,
  VerdictChip,
  Drawer,
  Input,
  Pager,
  Btn,
  RefreshBar,
  truncate,
  ageOf,
} from "../components/primitives";
import { ViewHeader, TeachingEmpty, Skeleton } from "../components/explain";
import type { InsightRow, PrincipleRow } from "../lib/types";

const LIMIT = 50;

interface InsightDetail {
  ok: boolean;
  insight: {
    id: number;
    content: string;
    type: string;
    tags: string | null;
    project: string | null;
    confidence: number;
  };
  entities?: { entity: string; entity_type: string }[];
  claims_summary?: {
    total: number;
    fresh: number;
    stale: number;
    claim_verdict?: string;
  } | null;
  contradictions?: unknown[];
}

interface Neighbors {
  found: boolean;
  canonical: string;
  claims_count: number;
  relations_out: {
    relation_type: string;
    target_type: string;
    target_canonical: string;
  }[];
  relations_in: {
    relation_type: string;
    source_type?: string;
    source_canonical?: string;
  }[];
}

function EntityMiniGraph({ entity, entityType }: { entity: string; entityType: string }) {
  const { data } = useQuery({
    queryKey: ["neighbors", entity, entityType],
    queryFn: () =>
      api.get<Neighbors>(
        `/graph/neighbors${api.qs({ entity, entity_type: entityType, limit: 8 })}`,
      ),
  });
  if (!data?.found) return null;
  return (
    <div className="rounded-[7px] border border-[var(--color-border)] p-2 text-[12px]">
      <div className="mb-1 flex items-center gap-2">
        <Chip fg="#22d3ee">{data.canonical}</Chip>
        <span className="num text-[var(--color-muted)]">
          {data.claims_count} claims
        </span>
      </div>
      {data.relations_out.length > 0 && (
        <ul className="space-y-0.5">
          {data.relations_out.map((r, i) => (
            <li key={i} className="num text-[var(--color-muted)]">
              —{r.relation_type}→ {r.target_canonical}{" "}
              <span className="opacity-60">({r.target_type})</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function InsightDrawer({ id, onClose }: { id: number; onClose: () => void }) {
  const navigate = useNavigate();
  const { data, isLoading } = useQuery({
    queryKey: ["insight_detail", id],
    queryFn: () => api.get<InsightDetail>(`/query/insights/${id}`),
  });
  const recalls = useQuery({
    queryKey: ["insight_recalls", id],
    queryFn: () =>
      api.get<{ rows: { query: string; created_at: string; hits: number }[] }>(
        `/query/recall_events?insight_id=${id}&limit=8`,
      ),
  });
  const i = data?.insight;
  const cs = data?.claims_summary;
  return (
    <Drawer open onClose={onClose} title={<span className="num">insight #{id}</span>}>
      {isLoading && <Skeleton rows={4} />}
      {i && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-2">
            <Chip>{i.type}</Chip>
            {i.project && <Chip>{i.project}</Chip>}
            <Chip fg="#22d3ee">conf {i.confidence.toFixed(2)}</Chip>
            {cs?.claim_verdict && <VerdictChip verdict={cs.claim_verdict} />}
          </div>
          <p className="text-[13px] leading-relaxed whitespace-pre-wrap">{i.content}</p>
          {i.tags && (
            <div className="flex flex-wrap gap-1.5">
              {i.tags.split(",").map((t, k) => (
                <Chip key={k}>{t.trim()}</Chip>
              ))}
            </div>
          )}
          {cs && (
            <button
              onClick={() =>
                navigate({
                  to: "/claims",
                  search: (prev: Record<string, string | number | undefined>) => ({
                    ...prev,
                    tab: "claims",
                    q: undefined,
                    // Land on this insight's project's claims (a filter the
                    // /claims endpoint natively supports via parent-insight
                    // project); the operator sees the claim graph this memory
                    // contributes to.
                    project: i.project ?? undefined,
                  }),
                })
              }
              className="w-full rounded-[7px] border border-[var(--color-border)] p-2 text-left text-[12px] transition-colors hover:border-[var(--color-focus)]"
            >
              <span className="num">
                claims: {cs.total} total · {cs.fresh} fresh · {cs.stale} stale
              </span>
              <span className="ml-2 text-[var(--color-muted)]">
                open in Claims ↗
              </span>
            </button>
          )}
          {data?.entities && (data.entities?.length ?? 0) > 0 && (
            <div>
              <div className="mb-1 text-[11px] uppercase tracking-wide text-[var(--color-muted)]">
                entity graph
              </div>
              <div className="mb-2 flex flex-wrap gap-1.5">
                {(data.entities ?? []).map((e, k) => (
                  <Chip key={k} title={e.entity_type}>
                    {e.entity}
                  </Chip>
                ))}
              </div>
              <EntityMiniGraph
                entity={data.entities[0].entity}
                entityType={data.entities[0].entity_type}
              />
            </div>
          )}
          <div>
            <div className="mb-1 text-[11px] uppercase tracking-wide text-[var(--color-muted)]">
              recent recalls
            </div>
            {recalls.data && recalls.data.rows.length > 0 ? (
              <ul className="space-y-1">
                {recalls.data.rows.map((r, k) => (
                  <li key={k} className="text-[12px]">
                    <span className="num text-[var(--color-muted)]">
                      {ageOf(r.created_at)}
                    </span>{" "}
                    {truncate(r.query, 70)}
                  </li>
                ))}
              </ul>
            ) : (
              <div className="text-[12px] text-[var(--color-muted)]">none</div>
            )}
          </div>
        </div>
      )}
    </Drawer>
  );
}

function InsightsTab() {
  const [q, setQ] = useUrlState("q");
  const [offset, setOffset] = useUrlNumber("offset");
  const [openId, setOpenId] = useUrlNumber("insight", -1);
  const query = useQuery({
    queryKey: ["insights", q, offset],
    queryFn: () =>
      api.get<{ rows: InsightRow[]; total: number }>(
        `/query/insights${api.qs({ q, limit: LIMIT, offset })}`,
      ),
    refetchInterval: 20000,
  });
  const { data, isLoading } = query;
  return (
    <Section
      title="Insights"
      right={
        <div className="flex items-center gap-3">
          <RefreshBar
            updatedAt={query.dataUpdatedAt}
            isFetching={query.isFetching}
            onRefresh={() => query.refetch()}
          />
          {data && (
            <Pager offset={offset} limit={LIMIT} total={data.total} onPage={setOffset} />
          )}
        </div>
      }
    >
      <div className="mb-3">
        <Input
          value={q}
          onChange={(v) => {
            setOffset(0);
            setQ(v);
          }}
          placeholder="search insights"
        />
      </div>
      {isLoading && <Skeleton />}
      {data && data.rows.length === 0 && !isLoading && (
        <TeachingEmpty title="No insights match">
          Insights are the engine's raw, confidence-scored memory — accepted
          proposals from the Review ledger. None match this search. Insights
          arrive here once captured items are triaged and accepted.
        </TeachingEmpty>
      )}
      {data && data.rows.length > 0 && (
        <Table head={["id", "verdict", "type", "project", "conf", "recalls", "content"]}>
          {data.rows.map((r) => (
            <Row key={r.id} onClick={() => setOpenId(r.id)}>
              <Cell mono>{r.id}</Cell>
              <Cell>
                <VerdictChip verdict={r.liveness?.verdict} />
              </Cell>
              <Cell>{r.type}</Cell>
              <Cell>{r.project ?? "—"}</Cell>
              <Cell mono>{r.confidence.toFixed(2)}</Cell>
              <Cell mono>{r.recall_count}</Cell>
              <Cell>{truncate(r.content, 80)}</Cell>
            </Row>
          ))}
        </Table>
      )}
      {openId >= 0 && <InsightDrawer id={openId} onClose={() => setOpenId(-1)} />}
    </Section>
  );
}

function PrinciplesTab() {
  const [q, setQ] = useUrlState("q");
  const [offset, setOffset] = useUrlNumber("offset");
  const query = useQuery({
    queryKey: ["principles", q, offset],
    queryFn: () =>
      api.get<{ rows: PrincipleRow[]; total: number }>(
        `/query/principles${api.qs({ q, limit: LIMIT, offset })}`,
      ),
    refetchInterval: 30000,
  });
  const { data, isLoading } = query;
  return (
    <Section
      title="Principles"
      right={
        <div className="flex items-center gap-3">
          <RefreshBar
            updatedAt={query.dataUpdatedAt}
            isFetching={query.isFetching}
            onRefresh={() => query.refetch()}
          />
          {data && (
            <Pager offset={offset} limit={LIMIT} total={data.total} onPage={setOffset} />
          )}
        </div>
      }
    >
      <div className="mb-3">
        <Input
          value={q}
          onChange={(v) => {
            setOffset(0);
            setQ(v);
          }}
          placeholder="search principles"
        />
      </div>
      {isLoading && <Skeleton />}
      {data && data.rows.length === 0 && !isLoading && (
        <TeachingEmpty title="No principles match">
          Principles are the highest-trust layer — verified recurring patterns
          promoted from insights. When all of a principle's core claims stay
          fresh, crag distills it into an enforced governance rule. None match
          this search.
        </TeachingEmpty>
      )}
      {data && data.rows.length > 0 && (
        <Table head={["id", "verdict", "project", "conf", "content"]}>
          {data.rows.map((r) => (
            <Row key={r.id}>
              <Cell mono>{r.id}</Cell>
              <Cell>
                <VerdictChip verdict={r.liveness?.verdict} />
              </Cell>
              <Cell>{r.project ?? "—"}</Cell>
              <Cell mono>{r.confidence.toFixed(2)}</Cell>
              <Cell>{truncate(r.content, 110)}</Cell>
            </Row>
          ))}
        </Table>
      )}
    </Section>
  );
}

interface AuditContra {
  ok: boolean;
  count: number;
  contradictions: {
    id: number;
    project: string;
    snippet: string;
    suspect_of: number;
    suspect_score: number;
  }[];
}

function InsightContraBadge() {
  const { data } = useQuery({
    queryKey: ["audit_contradictions"],
    queryFn: () => api.get<AuditContra>("/audit_contradictions?limit=20"),
    refetchInterval: 30000,
  });
  if (!data || data.count === 0) return null;
  return (
    <Section title={`Insight-level contradiction queue (${data.count})`}>
      <Table head={["id", "project", "suspect of", "score", "snippet"]}>
        {data.contradictions.map((c) => (
          <Row key={c.id}>
            <Cell mono>{c.id}</Cell>
            <Cell>{c.project}</Cell>
            <Cell mono>#{c.suspect_of}</Cell>
            <Cell mono>{c.suspect_score?.toFixed(2)}</Cell>
            <Cell>{truncate(c.snippet, 80)}</Cell>
          </Row>
        ))}
      </Table>
    </Section>
  );
}

export default function Corpus() {
  const [tab, setTab] = useUrlState("tab", "insights");
  return (
    <div className="space-y-4">
      <ViewHeader
        id="corpus"
        title="Corpus"
        subtitle="The memory itself — raw insights and the principles distilled from them."
        about={
          <>
            The corpus is what the engine remembers. <b>Insights</b> are raw,
            confidence-scored memory accepted from the Review ledger. Verified,
            recurring insights are promoted to <b>principles</b> — the highest-trust
            layer, each with its own confidence lifecycle. Principles whose core
            claims all stay fresh become compile-eligible and crag distills them
            into enforced governance. Open any insight to see its claims and jump
            into the claim graph.
          </>
        }
      />
      <div className="flex items-center gap-2">
        <Btn tone={tab === "insights" ? "brand" : "default"} onClick={() => setTab("insights")}>
          Insights
        </Btn>
        <Btn
          tone={tab === "principles" ? "brand" : "default"}
          onClick={() => setTab("principles")}
        >
          Principles
        </Btn>
      </div>
      {tab === "insights" ? <InsightsTab /> : <PrinciplesTab />}
      <InsightContraBadge />
    </div>
  );
}
