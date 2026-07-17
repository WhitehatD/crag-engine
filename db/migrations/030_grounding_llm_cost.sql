-- 030 - Grounding LLM cost ledger (schema v30)
--
-- WHY: Phase 1b / insight #3339 made the grounding LLM provider-agnostic
-- (db/grounding_config.py + db/llm_client.py) and per-stage max_tokens
-- configurable, but there was still no record of what any of it actually
-- costs or consumes. The default provider (anthropic-oauth) bills against
-- the CLAUDE SUBSCRIPTION WEEKLY LIMIT rather than a metered API key — no
-- surprise dollar bill, but grounding calls compete with the operator's own
-- Claude usage for the same weekly quota (starvation risk). This table is
-- the observability + enforcement substrate for that tradeoff:
--
--   1. Every grounding LLM call (author/adjudicate/correction, any provider)
--      appends one row here (db/grounding_cost.py::record_call). Append-only,
--      never updated — a durable usage/cost trail, same doctrine as
--      grounding_history.
--   2. `db/grounding_cost.py::budget_exceeded()` sums today's (UTC) rows
--      against [grounding.budget].daily_budget_calls / daily_budget_tokens
--      from stack.toml. When exceeded and pause_on_budget=true,
--      grounding_queue_v2.drain_one_job() refuses to start a new
--      LLM-calling job — fail-SAFE: the worker pauses itself rather than
--      risk exhausting a shared weekly quota.
--   3. `est_cost_usd` is an ESTIMATE only (using the $/Mtok figures in
--      db/grounding_cost.py — see that file's header comment). For
--      anthropic-oauth calls this is a "what this would have cost on the
--      metered API" figure, not an actual charge (subscription billing has
--      no per-call dollar cost). NULL for any model this table doesn't have
--      a verified price for — better an honest gap than a fabricated number.
--
-- ADDITIVE ONLY. No existing column/table is altered destructively.
-- Timestamp convention: ALL TEXT timestamps use the canonical Python
-- _utcnow_iso() output (YYYY-MM-DDTHH:MM:SS.ffffff+00:00). NEVER SQLite
-- datetime('now'). Day-bucketing for budget queries uses substr(ts,1,10)
-- (first 10 chars = YYYY-MM-DD), which is safe against this exact format.
--
-- Idempotency: engine-cli.py cmd_migrate auto-discovers db/migrations/*.sql
-- and skips any version already present in schema_version.

CREATE TABLE IF NOT EXISTS llm_cost_ledger (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT    NOT NULL,
    provider       TEXT    NOT NULL,               -- anthropic-oauth | anthropic-api | openai | ollama-local
    model          TEXT    NOT NULL,                -- the model actually used (may be the escalation model)
    stage          TEXT    NOT NULL,                -- author | adjudicate | correction
    claim_kind     TEXT,                             -- insight | principle | NULL (stage not claim-scoped)
    claim_id       INTEGER,
    tokens_in      INTEGER NOT NULL DEFAULT 0,
    tokens_out     INTEGER NOT NULL DEFAULT 0,
    est_cost_usd   REAL,                             -- NULL if model has no verified price (never fabricated)
    quota_type     TEXT    NOT NULL                  -- weekly_subscription | metered_api | local
);

CREATE INDEX IF NOT EXISTS idx_llm_cost_ledger_ts
    ON llm_cost_ledger(ts);
-- Budget queries filter "today" via substr(ts,1,10) = current UTC date;
-- index that expression directly so the daily sum stays O(log n).
CREATE INDEX IF NOT EXISTS idx_llm_cost_ledger_day
    ON llm_cost_ledger(substr(ts, 1, 10));
CREATE INDEX IF NOT EXISTS idx_llm_cost_ledger_claim
    ON llm_cost_ledger(claim_kind, claim_id);

-- Record migration
INSERT OR IGNORE INTO schema_version (version) VALUES (30);
