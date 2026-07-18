# crag Anchor architecture

An honest, high-level map of the system. Everything here is derived from the
code in this repo; file paths are given so you can verify any claim.

> Historical note: comments in the code reference design revisions ("REV 6",
> "Phase 25", "WS2", insight/principle `#NNNN` ids). Those refer to the
> internal design history of the engine; the numbered documents themselves are
> not part of this repo. This file is the current authoritative overview.

---

## 1. Process topology

Three processes, one datastore:

```
agent session (Claude Code / Cursor)
   │ stdio (MCP protocol)
   ▼
apps/mcp/mcp-server.py        ← 30 MCP tools; a THIN HTTP client. No SQLite
   │ HTTP, localhost             fallback: if the daemon is down it returns a
   ▼                             loud structured error, never stale data.
apps/daemon/engine_daemon.py  ← FastAPI daemon, default bind 127.0.0.1:8786.
   │                             Holds the embedding model in RAM; owns ALL
   ▼                             DB writes and background loops.
db/engine.db                  ← SQLite in WAL mode. Single file, plus
                                 sidecar state DBs for capture + token events.
```

- **Console entry points** (`pyproject.toml [project.scripts]`): `crag-anchor`
  (daemon), `crag-anchor-mcp` (MCP server), `crag-anchor-cli` (operator
  lifecycle tooling: migrate / backfill / decay). The `crag_anchor/` package
  contains thin runpy shims that execute the single-file app scripts from the
  checkout.
- **Path/bind resolution** (`db/engine_paths.py`): every path and the daemon
  bind resolve as *env var → `db/stack.toml` → repo-relative default*. Env
  always wins; deleting `stack.toml` breaks nothing.
- **Migrations** (`db/migrations/*.sql`, applied by `crag-anchor-cli migrate`
  and auto-applied by the daemon on boot against an empty DB): numbered,
  append-only, idempotent (version-checked against `schema_version`).

## 2. Memory model

Two trust layers plus a claims substrate:

| Layer | Table | Trust semantics |
|---|---|---|
| **Insights** | `insights` | Raw memories (gotchas, patterns, decisions). Confidence starts ~0.5, moves with `verify` (+0.1 / -0.2), decays when unused. |
| **Principles** | `principles` | Distilled, high-trust rules. Seeded at 0.9 by promotion; gentler verify deltas (+0.05 / -0.1). Loaded first at session pre-flight; they override conflicting raw insights. |
| **Claims** | `claims` + `insight_claims` / `principle_claims` | Atomic, testable decompositions of the above — the verification substrate (§4). |

Auto-promotion: the daemon promotes an insight to a principle when confidence
≥ 0.85 with ≥ 3 verifies and a positive streak (see the lifecycle loops in
`engine_daemon.py`). Weekly decay lowers confidence of unrecalled insights.

**Entity graph** (`db/entity_extract.py`, `db/entity_normalize.py`, migration
027): every save extracts typed entities (port, ip, domain, path, service,
file, classname, env_var), normalizes them into canonical rows
(`entity_canonical`) with typed relations (`entity_relations`). The `graph`
MCP tool traverses it: `siblings` (claims sharing entities), `neighbors` (one
entity's relations), `impact` (1-hop blast radius of an entity change).

## 3. Recall path

`POST /recall` (daemon) → hybrid ranking: cosine similarity over
all-MiniLM-L6-v2 embeddings (via fastembed/onnxruntime, model cached in
`model-cache/`) + BM25 full-text + confidence weighting. Matching principles
ride along with insight hits.

Every hit carries a **liveness block**: `fresh | aging | unverified |
revalidating | stale`, derived from when the claim was last grounded vs its
volatility TTL. Agents are expected to discount `stale`/`revalidating` hits.
Recall of an aging/stale hit *enqueues a grounding job* for it
(recall-as-trigger): hot memories stay verified because using them re-tests
them.

## 4. Claim layer (`db/claim_layer.py`)

An insight is a narrative; verification runs on atoms. The post-save async
pipeline:

1. **Decompose** — LLM role `decompose` splits the text into atomic claim
   drafts (fail-soft: on failure, one summary claim).
2. **Classify** — rules first, LLM (`classify`) last, into a **closed P1–P5
   taxonomy**:
   - **P1 mechanical** — read-only shell check `{cmd, expect}`
   - **P2 documentary** — source anchor `{file, load-bearing substrings}`
   - **P3 temporal** — event assertion vs local ground truth
   - **P4 semantic** — evidence-bundle recipe `{sources[], question}` → LLM verdict
   - **P5 axiomatic** — preference/decision/history; terminal, never queued
3. **Author** — for P1/P4, an LLM (`author`) writes the executable predicate
   spec. P1 commands are validated against the same read-only guard the
   executor enforces.
4. **Canonicalize + link** — claims are hashed, deduped into a shared pool,
   and linked to their parent insight/principle.

Rollup: parent claim-health is computed from its linked claims' verdicts —
a principle whose claims roll up fresh is *compile-eligible* for
`crag distill` (`/principles/export`, `principles_export` behavior tested in
`apps/daemon/tests/test_principles_export_drain.py`).

Routing isolation (`assert_no_interactive_proxy`): background LLM roles must
never ride an interactive session's local proxy; they use the configured
provider directly (§7).

## 5. Grounding (v2 queue + v3 adjudication)

- **Queue** (`db/grounding_queue_v2.py`, `grounding_jobs` table): durable,
  deduplicated jobs (`enqueue_job` is INSERT OR IGNORE on pending). Fed by
  recall-as-trigger (hot claims) and a periodic sweep (cold claims past their
  volatility TTL).
- **Workers**: an in-daemon async pool drains jobs — runs the falsifier steps
  **read-only** (a forbidden-command guard is enforced at authoring time and
  again at execution, defence-in-depth), gathers evidence, and for P4 claims
  asks an LLM (`verdict`, escalating to `adjudicate` on hard cases) to judge.
- **History** (`grounding_history`): every job leaves a full reasoning trail —
  commands run, evidence, verdict, chain-of-thought, recipe version. Surfaced
  via `grounding(action="history"|"jobs"|"stats")`.
- **Doctrine: detection ≠ resolution.** Grounding *flags* drift; it never
  auto-demotes. Resolution (verify / update / supersede / clear) is the
  agent's or operator's job via the `audit` → `grounding(check)` → `clear`
  triage loop. Autoresolve exists only for narrow, safe cases
  (`db/grounding_resolve.py`, tested in `test_grounding_autoresolve.py`).
- **Budget guard** (`db/grounding_cost.py`, migration 030): daily call/token
  budgets, min-interval pacing, optional pause-on-budget — grounding can never
  starve the interactive session's quota.

## 6. Write path & disposition

- **Write gate** (`db/write_gate.py`): every save passes hard gates — schema
  validation, a live-credential secret scan (AWS keys, PATs, private keys...)
  — then lifecycle resolution (new / update / supersede / noop against
  near-duplicates). Non-clean writes route to `insights_staging`, not the
  corpus.
- **Capture pipeline** (`db/capture/`): an autonomic tailer reads agent
  transcript files (watermark + rate-limit + dedup state in
  `capture-state.db`), an extractor LLM (`extract` role) mines candidate
  lessons, and `emit.py` posts them to `POST /capture/event` (optional
  shared-secret token auth). Everything lands in staging — **capture never
  writes the corpus directly**.
- **Disposition engine** (`db/disposition.py`, migrations 031–033): every
  staging row is a proposed state transition with a policy tier:
  - **T0 auto** — clean provenance, executes automatically, logged.
  - **T1 agent-delegable** — needs session capability `granted`.
  - **T2 human** — secret-flagged / high-impact; only `human_approved` may
    accept. An agent alone gets `requires_human`.
  A drain-SLA sweep ages every entry toward a terminal-or-safe outcome
  (T1/T2 default to `defer`, never blind auto-accept).
- **Contradiction handling** (`db/contradiction.py`, `db/claim_contradiction.py`):
  new claims/insights are cross-checked against neighbors (cosine + entailment
  heuristics); suspects land in an audit queue. False positives are cleared
  (`clear_suspect`), true conflicts adjudicated (`arena`) with supersede
  provenance. The detector is deliberately high-recall / low-precision;
  triage is explicit.

## 7. LLM provider seam (`db/llm_client.py`, `db/grounding_config.py`)

One provider abstraction, four backends, selected by config only:

| Provider | Auth | Use |
|---|---|---|
| `anthropic-oauth` (default) | local Claude Code OAuth token | zero-config local mode; bills the subscription, guarded by budgets |
| `anthropic-api` | `ANTHROPIC_API_KEY` | server/team mode, metered |
| `openai` | `OPENAI_API_KEY` | OpenAI-compatible endpoints |
| `ollama-local` | none | local models, zero marginal cost |

Per-role model selection (`[models]` in `db/stack.toml`): cheap tier for
decompose/classify/author/extract, stronger tier for verdict/adjudicate.
Fail-open: any client error returns `None` and the caller degrades gracefully.

## 8. Observability & self-description

- `GET /health` — daemon health + model state.
- `GET /metrics` — Prometheus text (`crag_anchor_*` counters/gauges).
- `GET /guide`, `GET /llms.txt` — machine-readable surface docs
  (`db/capabilities.py`); also exposed as the `engine_guide` MCP tool and the
  `engine://guide` MCP resource.
- Token ledger (`token_ledger` table, `cost_report`/`add_token_record` tools):
  per-session token cost plus empirical memory-value counters (recall hits
  that changed the approach, repeated errors already covered by a saved
  insight, novel saves).

## 9. Testing

Test suites are standalone scripts (no pytest dependency): each file under
`apps/daemon/tests/`, `apps/mcp/tests/`, `db/tests/` builds a temp DB from the
real migrations, exercises the target module directly, and exits 0/1. Run any
of them with plain `python <file>`. `scripts/test-timestamp-convention.py` is a
repo-wide gate: all TEXT timestamps must be written by Python UTC ISO-8601
(never SQLite `datetime('now')`) because mixing the two formats corrupts
lexical ordering.
