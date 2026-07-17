import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../lib/api";
import {
  Section,
  Table,
  Row,
  Cell,
  ClassChip,
  VerdictChip,
  Chip,
  Drawer,
  Input,
  Select,
  Pager,
  Empty,
  Btn,
  truncate,
  ageOf,
} from "../components/primitives";
import type {
  ClaimsResponse,
  ClaimDetail,
  ContradictionsResponse,
} from "../lib/types";

const CLASS_OPTS = [
  { value: "", label: "all classes" },
  { value: "P1", label: "P1 mechanical" },
  { value: "P2", label: "P2 documentary" },
  { value: "P3", label: "P3 temporal" },
  { value: "P4", label: "P4 semantic" },
  { value: "P5", label: "P5 axiomatic" },
];
const VERDICT_OPTS = [
  { value: "", label: "all verdicts" },
  { value: "fresh", label: "fresh" },
  { value: "aging", label: "aging" },
  { value: "unverified", label: "unverified" },
  { value: "revalidating", label: "revalidating" },
  { value: "stale", label: "stale" },
  { value: "axiomatic", label: "axiomatic" },
];

const LIMIT = 50;

function ClaimDrawer({ id, onClose }: { id: number; onClose: () => void }) {
  const { data, isLoading } = useQuery({
    queryKey: ["claim", id],
    queryFn: () => api.get<ClaimDetail>(`/claims/${id}`),
  });
  return (
    <Drawer open onClose={onClose} title={<span className="num">claim #{id}</span>}>
      {isLoading && <div className="text-[var(--color-muted)]">loading…</div>}
      {data?.ok && (
        <div className="space-y-4">
          <div className="flex items-center gap-2">
            <ClassChip pclass={(data.claim.predicate_class as string) ?? null} />
            <VerdictChip verdict={(data.claim.verdict as string) ?? "unverified"} />
            {(data.claim.primary_entity as string) && (
              <Chip>{data.claim.primary_entity as string}</Chip>
            )}
          </div>
          <p className="text-[13px] leading-relaxed">{data.claim.text as string}</p>

          {data.predicate_spec != null && (
            <div>
              <div className="mb-1 text-[11px] uppercase tracking-wide text-[var(--color-muted)]">
                predicate spec
              </div>
              <pre className="num overflow-auto rounded-[7px] border border-[var(--color-border)] bg-[var(--color-bg)] p-3 text-[12px]">
                {JSON.stringify(data.predicate_spec, null, 2)}
              </pre>
            </div>
          )}

          {data.entities.length > 0 && (
            <div>
              <div className="mb-1 text-[11px] uppercase tracking-wide text-[var(--color-muted)]">
                entities
              </div>
              <div className="flex flex-wrap gap-1.5">
                {data.entities.map((e, i) => (
                  <Chip key={i} title={e.entity_type}>
                    {e.entity}
                    <span className="ml-1 text-[var(--color-muted)]">
                      {e.entity_type}
                    </span>
                  </Chip>
                ))}
              </div>
            </div>
          )}

          <div>
            <div className="mb-1 text-[11px] uppercase tracking-wide text-[var(--color-muted)]">
              parents
            </div>
            {data.parents.insights.length === 0 &&
            data.parents.principles.length === 0 ? (
              <div className="text-[12px] text-[var(--color-muted)]">none</div>
            ) : (
              <ul className="space-y-1.5">
                {data.parents.insights.map((p) => (
                  <li key={`i${p.id}`} className="text-[12px]">
                    <Chip>insight #{p.id}</Chip>{" "}
                    <span className="text-[var(--color-muted)]">{p.role}</span>{" "}
                    {truncate(p.preview ?? "", 100)}
                  </li>
                ))}
                {data.parents.principles.map((p) => (
                  <li key={`p${p.id}`} className="text-[12px]">
                    <Chip fg="#22d3ee">principle #{p.id}</Chip>{" "}
                    <span className="text-[var(--color-muted)]">{p.role}</span>{" "}
                    {truncate(p.preview ?? "", 100)}
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div>
            <div className="mb-1 text-[11px] uppercase tracking-wide text-[var(--color-muted)]">
              grounding history
            </div>
            {data.grounding_history.length === 0 ? (
              <div className="text-[12px] text-[var(--color-muted)]">
                no grounding runs yet
              </div>
            ) : (
              <ul className="space-y-2">
                {data.grounding_history.map((h, i) => (
                  <li
                    key={i}
                    className="rounded-[7px] border border-[var(--color-border)] p-2 text-[12px]"
                  >
                    <div className="flex items-center gap-2">
                      <span className="num text-[var(--color-muted)]">
                        {ageOf(h.ts)}
                      </span>
                      <Chip>{h.job_type}</Chip>
                      {h.verdict && (
                        <VerdictChip
                          verdict={h.verdict === "pass" ? "fresh" : h.verdict === "fail" ? "stale" : "unverified"}
                        />
                      )}
                    </div>
                    {h.reasoning && (
                      <div className="mt-1 text-[var(--color-muted)]">
                        {truncate(h.reasoning, 200)}
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
      {data && !data.ok && (
        <div className="text-[var(--color-red)]">{data.error}</div>
      )}
    </Drawer>
  );
}

function ClaimsTab() {
  const [pclass, setPclass] = useState("");
  const [verdict, setVerdict] = useState("");
  const [entity, setEntity] = useState("");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const [openId, setOpenId] = useState<number | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["claims", pclass, verdict, entity, q, offset],
    queryFn: () =>
      api.get<ClaimsResponse>(
        `/claims${api.qs({
          predicate_class: pclass,
          verdict,
          entity,
          q,
          limit: LIMIT,
          offset,
        })}`,
      ),
    refetchInterval: 15000,
  });

  const resetPage = <T,>(setter: (v: T) => void) => (v: T) => {
    setOffset(0);
    setter(v);
  };

  return (
    <Section
      title="Claims"
      right={
        data && (
          <Pager
            offset={offset}
            limit={LIMIT}
            total={data.total}
            onPage={setOffset}
          />
        )
      }
    >
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Select value={pclass} onChange={resetPage(setPclass)} options={CLASS_OPTS} />
        <Select value={verdict} onChange={resetPage(setVerdict)} options={VERDICT_OPTS} />
        <Input value={entity} onChange={resetPage(setEntity)} placeholder="entity" />
        <Input value={q} onChange={resetPage(setQ)} placeholder="search text" />
      </div>

      {isLoading && <Empty label="loading…" />}
      {error && <Empty label={`error: ${(error as Error).message}`} />}
      {data && data.claims.length === 0 && !isLoading && (
        <Empty label="no claims match" />
      )}
      {data && data.claims.length > 0 && (
        <Table head={["id", "class", "verdict", "claim", "entity", "parents", "grounded"]}>
          {data.claims.map((c) => (
            <Row key={c.id} onClick={() => setOpenId(c.id)}>
              <Cell mono>{c.id}</Cell>
              <Cell>
                <ClassChip pclass={c.predicate_class} />
              </Cell>
              <Cell>
                <VerdictChip verdict={c.verdict} />
              </Cell>
              <Cell>{truncate(c.text, 88)}</Cell>
              <Cell>
                {c.primary_entity ? (
                  <Chip title={c.primary_entity_type ?? ""}>{c.primary_entity}</Chip>
                ) : (
                  <span className="text-[var(--color-muted)]">—</span>
                )}
              </Cell>
              <Cell mono>
                {c.insight_parents}i / {c.principle_parents}p
              </Cell>
              <Cell mono>{ageOf(c.grounded_at)}</Cell>
            </Row>
          ))}
        </Table>
      )}
      {openId !== null && (
        <ClaimDrawer id={openId} onClose={() => setOpenId(null)} />
      )}
    </Section>
  );
}

function ContradictionsTab() {
  const [offset, setOffset] = useState(0);
  const { data, isLoading } = useQuery({
    queryKey: ["claim_contradictions", offset],
    queryFn: () =>
      api.get<ContradictionsResponse>(
        `/claims/contradictions${api.qs({ status: "open", limit: 25, offset })}`,
      ),
    refetchInterval: 20000,
  });
  return (
    <Section
      title="Open claim contradictions"
      right={
        data && (
          <Pager offset={offset} limit={25} total={data.total} onPage={setOffset} />
        )
      }
    >
      {isLoading && <Empty label="loading…" />}
      {data && data.pairs.length === 0 && !isLoading && (
        <Empty label="no open contradictions" />
      )}
      <div className="space-y-3">
        {data?.pairs.map((p) => (
          <div
            key={p.id}
            className="rounded-[12px] border border-[var(--color-border)] p-3"
          >
            <div className="mb-2 flex items-center gap-2 text-[12px]">
              <Chip fg="#ef4444">pair #{p.id}</Chip>
              {p.shared_entity && <Chip>entity: {p.shared_entity}</Chip>}
              <span className="num text-[var(--color-muted)]">
                {ageOf(p.detected_at)}
              </span>
              {p.reason && (
                <span className="text-[var(--color-muted)]">{truncate(p.reason, 60)}</span>
              )}
            </div>
            <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
              {[p.claim_a, p.claim_b].map((c, i) =>
                c ? (
                  <div
                    key={i}
                    className="rounded-[7px] border border-[var(--color-border)] bg-[var(--color-bg)] p-2"
                  >
                    <div className="mb-1 flex items-center gap-1.5">
                      <span className="num text-[11px] text-[var(--color-muted)]">
                        #{c.id}
                      </span>
                      <ClassChip pclass={c.predicate_class} />
                      <VerdictChip verdict={c.verdict} />
                    </div>
                    <div className="text-[12px]">{c.text}</div>
                  </div>
                ) : (
                  <div key={i} className="text-[12px] text-[var(--color-muted)]">
                    claim missing
                  </div>
                ),
              )}
            </div>
          </div>
        ))}
      </div>
    </Section>
  );
}

export default function Claims() {
  const [tab, setTab] = useState<"claims" | "contradictions">("claims");
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <Btn tone={tab === "claims" ? "brand" : "default"} onClick={() => setTab("claims")}>
          Claims
        </Btn>
        <Btn
          tone={tab === "contradictions" ? "brand" : "default"}
          onClick={() => setTab("contradictions")}
        >
          Contradictions
        </Btn>
      </div>
      {tab === "claims" ? <ClaimsTab /> : <ContradictionsTab />}
    </div>
  );
}
