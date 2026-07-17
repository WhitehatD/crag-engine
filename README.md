# crag-engine

> **Unit tests for memory.** The verified-memory engine for [crag](https://crag.sh):
> every memory an AI agent saves is decomposed into atomic claims, each claim gets an
> executable falsifier, and a grounding loop re-verifies them against reality —
> so recall returns *verified* facts, not stale notes.

![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)
![Python: 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![Status: Alpha](https://img.shields.io/badge/status-alpha-orange)

---

## What it is

AI coding agents forget everything between sessions — and worse, when they *do*
have memory, that memory silently rots: ports change, files move, decisions get
reversed, and the agent keeps recalling the stale version with full confidence.

crag-engine treats memory the way engineers treat code: **untested memory is
broken memory.**

- **Save** — insights captured from agent sessions pass a write-path governance
  gate (schema checks, secret scan, dedup, lifecycle resolution) before they
  enter the corpus.
- **Decompose** — each insight is broken into atomic claims (P1–P5: existence,
  behavior, causal, spec, meta), and each claim gets an *executable predicate* —
  a cheap, read-only check that can prove it wrong.
- **Ground** — a background worker pool re-runs falsifiers (recall-triggered for
  hot claims, sweep-based for cold ones). Trust is how recently a claim was
  re-grounded against reality, not a number that only rises.
- **Recall** — hybrid semantic + full-text search (embeddings + BM25 +
  confidence), with a per-hit **liveness verdict** (`fresh` / `aging` /
  `unverified` / `revalidating` / `stale`) so the agent knows what to discount.
- **Govern** — contradiction detection, arena adjudication, supersede chains,
  confidence lifecycle (verify/decay/promote), and a tiered disposition engine
  (T0 auto / T1 agent / T2 human) for anything an agent proposes to persist.

## Architecture at a glance

```
Claude Code / Cursor session
        │  (stdio)
        ▼
crag-engine-mcp ──── 30 MCP tools, thin HTTP client, no local state
        │  (HTTP, localhost)
        ▼
crag-engine daemon ── FastAPI on 127.0.0.1:8786
        │              ├─ embedding model (all-MiniLM-L6-v2, in-RAM)
        │              ├─ claim layer (decompose → classify → author falsifiers)
        │              ├─ grounding workers (v2 queue + v3 LLM adjudication)
        │              ├─ disposition engine (T0/T1/T2 staging triage)
        │              └─ capture pipeline (transcript tailer → extractor → emit)
        ▼
SQLite (WAL) ──────── engine.db: insights, principles, claims, falsifiers,
                      entity graph, grounding history, token ledger
```

See [docs/architecture.md](docs/architecture.md) for the honest deep-dive.

## Quickstart

```bash
git clone <repo> crag-engine && cd crag-engine
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e '.[embeddings]'

crag-engine                      # start the daemon (first boot downloads the ~90MB embedding model)
curl -sf http://127.0.0.1:8786/health

# register the MCP server with Claude Code:
claude mcp add --scope user crag-engine crag-engine-mcp
```

Docker, systemd, and launchd paths are documented in
[packaging/README.md](packaging/README.md). Zero-config defaults are
repo-relative (`db/engine.db`, `logs/`, bind `127.0.0.1:8786`); everything is
overridable via `CRAG_ENGINE_*` env vars or `db/stack.toml`.

## Relation to crag

[crag](https://crag.sh) (`npm i -g @whitehatd/crag`) is the deterministic
**governance compiler**: one `governance.md` source of truth, compiled into
every agent format (CLAUDE.md, .cursorrules, AGENTS.md, hooks). crag-engine is
the **memory + verification engine** underneath it: verified insights distill
into principles, and principles whose claims roll up *fresh* can compile back
into governance rules (`crag distill`). Compiler + engine, one product: rules
that are derived from verified reality, not vibes.

## MCP surface

30 tools — recall (`recall`, `recall_principle`, `recall_by_entity`), knowledge
capture (`save_insight`, `suggest_tags`), lifecycle (`get`, `verify`, `update`,
`supersede`, `promote_insight`), governance queues (`audit`, `arena`,
`clear_suspect`, `grounding`), disposition (`disposition_list`,
`disposition_resolve`, `staging_triage`), session state (`session_diary`,
`project_context`, `events`, `brief`), telemetry (`recall_stats`,
`recent_insights`, `cost_report`, `add_token_record`, `health_check`),
governance export (`principles_export`), and introspection (`engine_guide`,
`graph`). Full table in
[packages/mcp-spec/README.md](packages/mcp-spec/README.md).

## Development

```bash
pip install -e '.[all]'
ruff check .                                   # lint
python apps/daemon/tests/test_engine_paths.py  # test suites are standalone scripts
python db/tests/test_write_gate.py             # (each exits 0 on pass, 1 on fail)
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). **Every line of
source in this repository is Apache-2.0** — no carve-outs, no dual licensing,
no contributor surprises. Commercial capabilities (hosted console, team memory
server, SSO/RBAC, audit export) are delivered as separate distributions built
on this engine — see [crag.sh](https://crag.sh).
