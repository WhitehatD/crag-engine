# crag-engine — packaging & install quickstart

crag-engine is the **verified-memory engine** that sits under Claude Code /
Cursor. This directory makes it installable on Linux, macOS, or Windows. Pick
one of the three paths below.

Every path ends the same way: a daemon listening on `127.0.0.1:8786` answering
`GET /health` with `{"ok": true, ...}`, and the `crag-engine-mcp` entry point
registered with your agent.

The **operator console** is embedded: the daemon serves it at
`http://127.0.0.1:8786/console`. The Docker image builds it automatically
(multi-stage). On a bare-metal install, build it once with
`cd apps/console && npm install && npm run build` — until then `/console`
returns a JSON build hint and the API is unaffected.

> **Zero-config defaults are repo-relative.** With no `CRAG_ENGINE_*` env and
> no `[paths]` block in `db/stack.toml`, the DB is `db/engine.db`, logs are
> `logs/`, and the bind is `127.0.0.1:8786`. Override anything via env (always
> wins) or `db/stack.toml` (see the commented `[paths]` section there).

---

## A. Docker Compose (recommended for a fresh host)

```bash
git clone <repo> crag-engine && cd crag-engine
docker compose -f packaging/docker/docker-compose.yml up -d --build
curl -sf http://127.0.0.1:8786/health
```

- DB, logs, and the embedding model cache persist on the `crag-engine-data`
  volume (`CRAG_ENGINE_HOME=/data` inside the container).
- The daemon binds `0.0.0.0:8786` **inside** the container; compose maps it to
  `127.0.0.1:8786` on the host, preserving the localhost-only contract.
- `mem_limit: 1g`, healthcheck `start_period: 120s` (embedding warm-up), and
  `restart: unless-stopped` are set by default.

## B. Pip install (per-OS, bare-metal)

```bash
git clone <repo> crag-engine && cd crag-engine
python3 -m venv .venv
# Linux/macOS:  source .venv/bin/activate
# Windows:      .venv\Scripts\activate
pip install -e '.[embeddings]'      # omit [embeddings] to start fail-soft (recall degraded until fastembed is present)
crag-engine                          # foreground; Ctrl-C to stop
# in another shell:
curl -sf http://127.0.0.1:8786/health
```

Console scripts installed by `pip install -e .`:

| Command | What it runs |
|---|---|
| `crag-engine` | the FastAPI memory daemon (`apps/daemon/engine_daemon.py`) |
| `crag-engine-mcp` | the stdio MCP server, a thin client of the daemon (`apps/mcp/mcp-server.py`) |
| `crag-engine-cli` | operator lifecycle tooling — migrate/backfill/decay (`db/engine-cli.py`) |

Run at boot:

- **Linux** — `packaging/systemd/crag-engine.service` (edit the User /
  WorkingDirectory / ExecStart paths, then `systemctl enable --now`).
- **macOS** — `packaging/launchd/sh.crag.engine.plist` (edit the `CHANGE_ME`
  paths, then `launchctl load`).
- **Windows** — register a Task Scheduler task that runs the venv's
  `crag-engine.exe` console script at logon (or use the Docker path).

## C. Just the MCP client (agent already has a daemon)

If a daemon is already running (locally or reachable via
`CRAG_ENGINE_DAEMON_URL`), you only need the MCP entry point registered — see
the next section.

---

## MCP registration (all paths)

After `pip install -e .` (path B/C) or with the console script otherwise on
PATH, register the stdio server with Claude Code in one line:

```bash
claude mcp add --scope user crag-engine crag-engine-mcp
```

The MCP server resolves the daemon URL from `CRAG_ENGINE_DAEMON_URL`, else
`CRAG_ENGINE_DAEMON_HOST`/`CRAG_ENGINE_DAEMON_PORT` via `db/stack.toml`, else
`http://127.0.0.1:8786`. A console entry point on PATH (rather than an absolute
python-file path) is deliberate — path-based registration breaks silently when
checkouts move; an entry point eliminates that failure class.

---

## Auth: local mode vs server/team mode

Grounding (the LLM-backed re-verification of memories) needs a model. Two modes:

### Local mode — **default**, zero extra config
`CRAG_ENGINE_GROUNDING_PROVIDER=anthropic-oauth` (the default). The daemon
calls a local OAuth passthrough using your Claude Code subscription
credentials. Nothing to set.

### Server / team mode — first-party Anthropic API
For a shared server with no interactive Claude session, use the Anthropic API:

```bash
pip install -e '.[anthropic-api]'
export CRAG_ENGINE_GROUNDING_PROVIDER=anthropic-api
export ANTHROPIC_API_KEY=sk-ant-...      # never commit; use an env_file / secret store
crag-engine
```

An OpenAI-compatible provider (`openai` / `ollama`) is also available via
`pip install -e '.[openai]'` + `CRAG_ENGINE_GROUNDING_PROVIDER=openai` +
`CRAG_ENGINE_LLM_BASE_URL=...`. All grounding LLM knobs live in `db/stack.toml`
`[grounding.llm]` / env (`CRAG_ENGINE_GROUNDING_*`); see `db/grounding_config.py`.

> **Secrets never go in git.** `db/stack.toml` holds *references* and
> non-secret defaults only; API keys stay in env or an env_file.

---

## Configuration surface (summary)

| Concern | Env (wins) | `db/stack.toml` | Default |
|---|---|---|---|
| Engine root | `CRAG_ENGINE_HOME` | `[paths].home` | repo root |
| DB file | `CRAG_ENGINE_DB_PATH` | `[paths].db_path` | `<home>/db/engine.db` |
| Log dir | `CRAG_ENGINE_LOG_DIR` | `[paths].log_dir` | `<home>/logs` |
| Daemon bind | `CRAG_ENGINE_DAEMON_HOST` / `CRAG_ENGINE_DAEMON_PORT` | `[paths].daemon_host` / `daemon_port` | `127.0.0.1:8786` |
| MCP → daemon | `CRAG_ENGINE_DAEMON_URL` | (derived from bind) | `http://127.0.0.1:8786` |
| External sources | `CRAG_ENGINE_SOURCE_<KEY>` | `[sources].<key>` | unset (fail-soft) |
| Grounding LLM | `CRAG_ENGINE_GROUNDING_*` | `[grounding.llm]` | anthropic-oauth / haiku |

Resolution order is always **env → stack.toml → repo-relative default**. Delete
`db/stack.toml` entirely and everything falls back to defaults — nothing breaks.
