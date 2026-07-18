#!/usr/bin/env python3
# coding: utf-8
"""Phase 1b: Provider-agnostic grounding LLM + cost governance test suite.

Standalone (no pytest — mirrors test_grounding_autoresolve.py style).

Covers:
  T_CFG      grounding_config defaults, TOML merging, env-var overrides
  T_COST     grounding_cost record_call, budget_status, budget_exceeded
  T_USAGE    llm_client record_usage / get_last_usage / clear_last_usage sidecar
  T_AUTHOR   author_recipe config-driven max_tokens + escalation on malformed_output
  T_ADJESC   adjudicate escalation on uncertain verdict
  T_BUDGET   drain_one_job budget gate (pauses worker, no attempt consumed)
  T_RECORD   drain_one_job records cost via _record_llm_cost

Exit codes: 0 = all pass, 1 = any failure.

Run:
  python apps/daemon/tests/test_grounding_llm_config.py
"""
from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest import mock

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

REPO_ROOT = Path(__file__).resolve().parents[3]
LIVE_DB = REPO_ROOT / "db" / "engine.db"
MIGRATIONS = REPO_ROOT / "db" / "migrations"
DB_DIR = REPO_ROOT / "db"

if str(DB_DIR) not in sys.path:
    sys.path.insert(0, str(DB_DIR))

FAILURES: list[str] = []
PASSES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSES.append(name)
        print(f"  [v] {name}")
    else:
        FAILURES.append(f"{name}: {detail}")
        print(f"  [X] {name}  -- {detail}")


# ---------------------------------------------------------------------------
# Temp DB helper (reused from test_grounding_autoresolve.py)
# ---------------------------------------------------------------------------

def _apply_migration(conn: sqlite3.Connection, path: Path) -> None:
    sql = path.read_text(encoding="utf-8")
    for chunk in sql.split(";"):
        lines = [l for l in chunk.splitlines()
                 if l.strip() and not l.strip().startswith("--")]
        stmt = "\n".join(lines).strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            m = str(e).lower()
            if "duplicate column name" in m or "already exists" in m:
                continue
            raise


def build_temp_db() -> str:
    src = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    stmts = [r[0] for r in src.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]
    src.close()
    fd, path = tempfile.mkstemp(suffix=".db", prefix="llmcfg-")
    os.close(fd)
    conn = sqlite3.connect(path)
    for s in stmts:
        try:
            conn.execute(s)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    for mig in ("026_grounding_v2.sql", "028_grounding_autoresolve.sql",
                "030_grounding_llm_cost.sql"):
        p = MIGRATIONS / mig
        if p.exists():
            _apply_migration(conn, p)
    conn.commit()
    conn.close()
    print(f"temp DB (+030 cost ledger): {path}")
    return path


# ---------------------------------------------------------------------------
# Stub LLMs
# ---------------------------------------------------------------------------

class _FakeContent:
    def __init__(self, text): self.text = text


class _FakeUsage:
    def __init__(self, inp=100, out=50):
        self.input_tokens = inp
        self.output_tokens = out


class _FakeResp:
    def __init__(self, text, usage=None):
        self.content = [_FakeContent(text)]
        self.usage = usage or _FakeUsage()


class _SequenceLLM:
    """Returns responses in order; records each call's kwargs."""
    def __init__(self, responses):
        self._r = responses
        self._i = 0
        self.calls: list[dict] = []

    @property
    def messages(self): return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        r = self._r[min(self._i, len(self._r) - 1)]
        self._i += 1
        return r


# ---------------------------------------------------------------------------
# T_CFG: grounding_config defaults, TOML merging, env-var overrides
# ---------------------------------------------------------------------------

print("\n== T_CFG: grounding_config defaults + env + TOML ==")

import grounding_config

# (a) defaults when no TOML and no env
with mock.patch.object(grounding_config, "_STACK_TOML", Path("/nonexistent/stack.toml")):
    cfg = grounding_config._build_config()

check("T_CFG_a: default model is haiku", cfg.model == "claude-haiku-4-5-20251001",
      f"model={cfg.model}")
check("T_CFG_b: default provider is anthropic-oauth", cfg.provider == "anthropic-oauth",
      f"provider={cfg.provider}")
check("T_CFG_c: default author_max_tokens is 4096", cfg.author_max_tokens == 4096,
      f"author_max_tokens={cfg.author_max_tokens}")
check("T_CFG_d: default daily_budget_calls is 500", cfg.daily_budget_calls == 500,
      f"daily_budget_calls={cfg.daily_budget_calls}")
check("T_CFG_e: default pause_on_budget is True", cfg.pause_on_budget is True,
      f"pause_on_budget={cfg.pause_on_budget}")
check("T_CFG_f: default escalation_enabled is True", cfg.escalation_enabled is True,
      f"escalation_enabled={cfg.escalation_enabled}")
check("T_CFG_g: default escalation_model is sonnet-5", cfg.escalation_model == "claude-sonnet-5",
      f"escalation_model={cfg.escalation_model}")

# (b) env override wins over defaults
with mock.patch.dict(os.environ, {
    "CRAG_ANCHOR_GROUNDING_MODEL": "test-model-x",
    "CRAG_ANCHOR_GROUNDING_DAILY_BUDGET_CALLS": "42",
    "CRAG_ANCHOR_GROUNDING_PAUSE_ON_BUDGET": "false",
    "CRAG_ANCHOR_GROUNDING_TEMPERATURE": "0.7",
}):
    with mock.patch.object(grounding_config, "_STACK_TOML", Path("/nonexistent/stack.toml")):
        cfg_env = grounding_config._build_config()

check("T_CFG_h: env override model", cfg_env.model == "test-model-x",
      f"model={cfg_env.model}")
check("T_CFG_i: env override daily_budget_calls", cfg_env.daily_budget_calls == 42,
      f"daily_budget_calls={cfg_env.daily_budget_calls}")
check("T_CFG_j: env override pause_on_budget=false", cfg_env.pause_on_budget is False,
      f"pause_on_budget={cfg_env.pause_on_budget}")
check("T_CFG_k: env override temperature=0.7", cfg_env.temperature == 0.7,
      f"temperature={cfg_env.temperature}")

# (c) TOML file values are loaded
toml_content = b"""
[grounding]
max_attempts = 5

[grounding.llm]
model = "toml-model"
author_max_tokens = 8192

[grounding.budget]
daily_budget_tokens = 999999
"""
with tempfile.NamedTemporaryFile(suffix=".toml", delete=False) as tf:
    tf.write(toml_content)
    tf.flush()
    toml_path = Path(tf.name)

try:
    with mock.patch.object(grounding_config, "_STACK_TOML", toml_path):
        cfg_toml = grounding_config._build_config()
    check("T_CFG_l: TOML model loaded", cfg_toml.model == "toml-model",
          f"model={cfg_toml.model}")
    check("T_CFG_m: TOML author_max_tokens loaded", cfg_toml.author_max_tokens == 8192,
          f"author_max_tokens={cfg_toml.author_max_tokens}")
    check("T_CFG_n: TOML max_attempts loaded", cfg_toml.max_attempts == 5,
          f"max_attempts={cfg_toml.max_attempts}")
    check("T_CFG_o: TOML daily_budget_tokens loaded", cfg_toml.daily_budget_tokens == 999999,
          f"daily_budget_tokens={cfg_toml.daily_budget_tokens}")
    # non-overridden keys still get defaults
    check("T_CFG_p: un-overridden provider still default", cfg_toml.provider == "anthropic-oauth",
          f"provider={cfg_toml.provider}")
finally:
    toml_path.unlink(missing_ok=True)

# (d) env wins over TOML
toml_content2 = b"""
[grounding.llm]
model = "toml-model"
"""
with tempfile.NamedTemporaryFile(suffix=".toml", delete=False) as tf2:
    tf2.write(toml_content2)
    tf2.flush()
    toml_path2 = Path(tf2.name)

try:
    with mock.patch.dict(os.environ, {"CRAG_ANCHOR_GROUNDING_MODEL": "env-wins"}):
        with mock.patch.object(grounding_config, "_STACK_TOML", toml_path2):
            cfg_ew = grounding_config._build_config()
    check("T_CFG_q: env wins over TOML", cfg_ew.model == "env-wins",
          f"model={cfg_ew.model}")
finally:
    toml_path2.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# T_COST: grounding_cost record_call, budget_status, budget_exceeded
# ---------------------------------------------------------------------------

print("\n== T_COST: cost ledger + budget enforcement ==")

TEMP_DB = build_temp_db()
import grounding_cost

conn = sqlite3.connect(TEMP_DB)
conn.row_factory = sqlite3.Row

# (a) record_call inserts a row
grounding_cost.record_call(conn, "anthropic-oauth", "claude-haiku-4-5-20251001", "author",
                           1000, 200, claim_kind="insight", claim_id=42)
conn.commit()
row = conn.execute("SELECT * FROM llm_cost_ledger ORDER BY id DESC LIMIT 1").fetchone()
check("T_COST_a: row inserted", row is not None)
check("T_COST_b: provider recorded", row["provider"] == "anthropic-oauth",
      f"provider={row['provider']}")
check("T_COST_c: tokens_in recorded", row["tokens_in"] == 1000,
      f"tokens_in={row['tokens_in']}")
check("T_COST_d: tokens_out recorded", row["tokens_out"] == 200,
      f"tokens_out={row['tokens_out']}")
check("T_COST_e: est_cost_usd is not None (haiku has pricing)",
      row["est_cost_usd"] is not None, f"est_cost_usd={row['est_cost_usd']}")
check("T_COST_f: quota_type is weekly_subscription",
      row["quota_type"] == "weekly_subscription", f"quota_type={row['quota_type']}")

# (b) est_cost_usd is NULL for unknown model
grounding_cost.record_call(conn, "openai", "gpt-unknown-999", "adjudicate", 500, 100)
conn.commit()
row2 = conn.execute("SELECT est_cost_usd FROM llm_cost_ledger ORDER BY id DESC LIMIT 1").fetchone()
check("T_COST_g: unknown model -> est_cost_usd is NULL", row2["est_cost_usd"] is None,
      f"est_cost_usd={row2['est_cost_usd']}")

# (c) budget_status returns correct shape
cfg_budget = grounding_config._build_config()
status = grounding_cost.budget_status(conn, cfg_budget)
check("T_COST_h: budget_status has calls_today", "calls_today" in status, f"keys={list(status)}")
check("T_COST_i: calls_today >= 2 (we inserted 2)", status["calls_today"] >= 2,
      f"calls_today={status['calls_today']}")
check("T_COST_j: exceeded is False (2 calls << 500)", status["exceeded"] is False,
      f"exceeded={status['exceeded']}")

# (d) budget_exceeded with pause_on_budget=True and over limit
from dataclasses import replace
cfg_tight = replace(cfg_budget, daily_budget_calls=1, pause_on_budget=True)
check("T_COST_k: budget_exceeded=True when calls > 1",
      grounding_cost.budget_exceeded(conn, cfg_tight) is True)

# (e) budget_exceeded with pause_on_budget=False -> always False
cfg_nopause = replace(cfg_tight, pause_on_budget=False)
check("T_COST_l: budget_exceeded=False when pause_on_budget=False",
      grounding_cost.budget_exceeded(conn, cfg_nopause) is False)

conn.close()


# ---------------------------------------------------------------------------
# T_USAGE: llm_client record_usage / get_last_usage / clear_last_usage
# ---------------------------------------------------------------------------

print("\n== T_USAGE: thread-local usage sidecar ==")

import llm_client

llm_client.clear_last_usage()
u0 = llm_client.get_last_usage()
check("T_USE_a: clear leaves model=None", u0["model"] is None, f"model={u0['model']}")
check("T_USE_b: clear leaves tokens_in=0", u0["tokens_in"] == 0, f"tokens_in={u0['tokens_in']}")

# record usage
resp = _FakeResp("hello", _FakeUsage(inp=500, out=200))
llm_client.record_usage(resp, model="test-model", provider="test-provider")
u1 = llm_client.get_last_usage()
check("T_USE_c: tokens_in recorded", u1["tokens_in"] == 500, f"tokens_in={u1['tokens_in']}")
check("T_USE_d: tokens_out recorded", u1["tokens_out"] == 200, f"tokens_out={u1['tokens_out']}")
check("T_USE_e: model recorded", u1["model"] == "test-model", f"model={u1['model']}")
check("T_USE_f: provider recorded", u1["provider"] == "test-provider",
      f"provider={u1['provider']}")

# clear again
llm_client.clear_last_usage()
u2 = llm_client.get_last_usage()
check("T_USE_g: clear resets model to None", u2["model"] is None, f"model={u2['model']}")

# record_usage with None usage attr (defensive)
class _NoUsageResp:
    content = [_FakeContent("hi")]
    usage = None

llm_client.record_usage(_NoUsageResp(), model="m", provider="p")
u3 = llm_client.get_last_usage()
check("T_USE_h: None usage -> tokens_in=0", u3["tokens_in"] == 0, f"tokens_in={u3['tokens_in']}")


# ---------------------------------------------------------------------------
# T_AUTHOR: author_recipe config-driven max_tokens + escalation
# ---------------------------------------------------------------------------

print("\n== T_AUTHOR: config-driven author_recipe + escalation ==")

import grounding_author

_VALID_RECIPE_JSON = json.dumps({
    "falsification_question": "Is the config set?",
    "recipe": {
        "steps": ["grep -i foo /etc/conf"],
        "refutes_if": "no match",
        "supports_if": "match found",
    },
})

# (a) author_recipe uses cfg.author_max_tokens
stub_llm = _SequenceLLM([_FakeResp(_VALID_RECIPE_JSON)])
result, reason = grounding_author.author_recipe("test claim", [], stub_llm)
check("T_AUT_cfg_a: recipe returned", result is not None, f"result={result}")
# Check that the LLM call used cfg.author_max_tokens (default 4096)
call_kwargs = stub_llm.calls[0] if stub_llm.calls else {}
check("T_AUT_cfg_b: max_tokens from config (4096)",
      call_kwargs.get("max_tokens") == 4096,
      f"max_tokens={call_kwargs.get('max_tokens')}")

# (b) escalation on malformed_output: two calls made, second with escalation_model
malformed_resp = _FakeResp("this is not json {{{")
valid_resp = _FakeResp(_VALID_RECIPE_JSON)
esc_llm = _SequenceLLM([malformed_resp, valid_resp])
result_esc, reason_esc = grounding_author.author_recipe("test claim", [], esc_llm)
check("T_AUT_cfg_c: escalation produces result on second call",
      result_esc is not None, f"result_esc={result_esc}")
check("T_AUT_cfg_d: two LLM calls made (primary + escalation)",
      len(esc_llm.calls) == 2, f"calls={len(esc_llm.calls)}")
# First call uses primary model, second uses escalation_model
cfg_now = grounding_config.get_config()
first_model = esc_llm.calls[0].get("model", "") if len(esc_llm.calls) > 0 else ""
second_model = esc_llm.calls[1].get("model", "") if len(esc_llm.calls) > 1 else ""
check("T_AUT_cfg_e: first call uses primary model",
      first_model == cfg_now.model, f"first_model={first_model}")
check("T_AUT_cfg_f: second call uses escalation_model",
      second_model == cfg_now.escalation_model, f"second_model={second_model}")

# (c) no escalation when model= is explicitly passed (prevents re-escalation)
esc_llm2 = _SequenceLLM([malformed_resp, valid_resp])
result_noesc, _ = grounding_author.author_recipe(
    "test claim", [], esc_llm2, model="explicit-model"
)
check("T_AUT_cfg_g: explicit model= prevents escalation (only 1 call)",
      len(esc_llm2.calls) == 1, f"calls={len(esc_llm2.calls)}")

# (d) no escalation on write_guard_rejected (not a model-capability failure)
_FORBIDDEN_RECIPE_JSON = json.dumps({
    "falsification_question": "test",
    "recipe": {"steps": ["rm -rf /"], "refutes_if": "no", "supports_if": "yes"},
})
guard_llm = _SequenceLLM([_FakeResp(_FORBIDDEN_RECIPE_JSON)])
_, reason_guard = grounding_author.author_recipe("test claim", [], guard_llm)
check("T_AUT_cfg_h: write_guard_rejected does NOT trigger escalation (1 call)",
      len(guard_llm.calls) == 1, f"calls={len(guard_llm.calls)}")
check("T_AUT_cfg_i: reason is write_guard_rejected",
      reason_guard is not None and reason_guard.startswith("write_guard_rejected"),
      f"reason={reason_guard}")


# ---------------------------------------------------------------------------
# T_ADJESC: adjudicate escalation on uncertain verdict
# ---------------------------------------------------------------------------

print("\n== T_ADJESC: adjudicate escalation on uncertain ==")

_UNCERTAIN_ADJ = json.dumps({"verdict": "uncertain", "reasoning": "unclear", "evidence": ""})
_PASS_ADJ = json.dumps({"verdict": "pass", "reasoning": "confirmed", "evidence": "ok"})

uncertain_resp = _FakeResp(_UNCERTAIN_ADJ)
pass_resp = _FakeResp(_PASS_ADJ)

adj_llm = _SequenceLLM([uncertain_resp, pass_resp])
adj_result = grounding_author.adjudicate(
    claim="test claim",
    recipe={"steps": ["echo ok"], "refutes_if": "no", "supports_if": "ok"},
    step_outputs=["ok"],
    prior_history=[],
    llm=adj_llm,
)
check("T_ADJESC_a: escalation on uncertain produces pass verdict",
      adj_result.get("verdict") == "pass", f"verdict={adj_result.get('verdict')}")
check("T_ADJESC_b: two LLM calls made (primary + escalation)",
      len(adj_llm.calls) == 2, f"calls={len(adj_llm.calls)}")
adj_first_model = adj_llm.calls[0].get("model", "") if len(adj_llm.calls) > 0 else ""
adj_second_model = adj_llm.calls[1].get("model", "") if len(adj_llm.calls) > 1 else ""
check("T_ADJESC_c: first call uses primary model",
      adj_first_model == cfg_now.model, f"first={adj_first_model}")
check("T_ADJESC_d: second call uses escalation model",
      adj_second_model == cfg_now.escalation_model, f"second={adj_second_model}")

# (b) no escalation when model= is explicit
adj_llm2 = _SequenceLLM([uncertain_resp, pass_resp])
adj_result2 = grounding_author.adjudicate(
    claim="test", recipe={"steps": [], "refutes_if": "", "supports_if": ""},
    step_outputs=[], prior_history=[], llm=adj_llm2, model="explicit-model",
)
check("T_ADJESC_e: explicit model= prevents adjudicate escalation (1 call)",
      len(adj_llm2.calls) == 1, f"calls={len(adj_llm2.calls)}")

# (c) no escalation on pass verdict (only uncertain triggers it)
adj_llm3 = _SequenceLLM([pass_resp])
adj_result3 = grounding_author.adjudicate(
    claim="test", recipe={"steps": [], "refutes_if": "", "supports_if": ""},
    step_outputs=[], prior_history=[], llm=adj_llm3,
)
check("T_ADJESC_f: pass verdict does NOT trigger escalation (1 call)",
      len(adj_llm3.calls) == 1, f"calls={len(adj_llm3.calls)}")


# ---------------------------------------------------------------------------
# T_BUDGET: drain_one_job budget gate
# ---------------------------------------------------------------------------

print("\n== T_BUDGET: drain_one_job budget gate ==")

from grounding_queue_v2 import drain_one_job, enqueue_job

conn = sqlite3.connect(TEMP_DB)
conn.row_factory = sqlite3.Row

# Seed a claim + author job
cur = conn.execute(
    """INSERT INTO insights (project, type, content, tags, status, confidence,
                             grounding_due, created_at, updated_at)
       VALUES ('infra', 'gotcha', 'budget test claim', '', 'active', 0.5, 1,
               '2026-06-01T00:00:00+00:00', '2026-06-01T00:00:00+00:00')"""
)
budget_claim_id = cur.lastrowid
enqueue_job(conn, "insight", budget_claim_id, "author", priority=10)
conn.commit()

# Patch budget_exceeded to return True
with mock.patch("grounding_cost.budget_exceeded", return_value=True):
    did = drain_one_job(conn, _SequenceLLM([_FakeResp(_VALID_RECIPE_JSON)]))

check("T_BDG_a: drain_one_job returns False when budget exceeded", did is False,
      f"did={did}")
# Job should still be pending (not consumed)
job = conn.execute(
    "SELECT status, attempts FROM grounding_jobs WHERE claim_id=? AND status='pending'",
    (budget_claim_id,)
).fetchone()
check("T_BDG_b: job still pending (no attempt consumed)", job is not None,
      f"job={dict(job) if job else None}")
check("T_BDG_c: attempts still 0", job is not None and job["attempts"] == 0,
      f"attempts={job['attempts'] if job else '?'}")

conn.close()


# ---------------------------------------------------------------------------
# T_RECORD: drain_one_job records cost via _record_llm_cost
# ---------------------------------------------------------------------------

print("\n== T_RECORD: cost recording in drain_one_job ==")

conn = sqlite3.connect(TEMP_DB)
conn.row_factory = sqlite3.Row

# Seed claim + author job
cur2 = conn.execute(
    """INSERT INTO insights (project, type, content, tags, status, confidence,
                             grounding_due, created_at, updated_at)
       VALUES ('infra', 'gotcha', 'cost record test', '', 'active', 0.5, 1,
               '2026-06-01T00:00:00+00:00', '2026-06-01T00:00:00+00:00')"""
)
rec_claim_id = cur2.lastrowid
# priority=100 (higher than the leftover pending job from T_BUDGET, priority=10)
# so drain_one_job's `ORDER BY priority DESC, enqueued_at ASC` picks THIS job,
# not the still-pending budget-gated job left behind by the prior section.
enqueue_job(conn, "insight", rec_claim_id, "author", priority=100)
conn.commit()

# Count cost rows before
before_count = conn.execute("SELECT COUNT(*) AS c FROM llm_cost_ledger").fetchone()["c"]

# Drain the job with a stub LLM that returns a valid recipe
rec_llm = _SequenceLLM([_FakeResp(_VALID_RECIPE_JSON, _FakeUsage(inp=800, out=300))])
did_rec = drain_one_job(conn, rec_llm)
conn.commit()

after_count = conn.execute("SELECT COUNT(*) AS c FROM llm_cost_ledger").fetchone()["c"]
check("T_REC_a: drain_one_job processed", did_rec is True, f"did={did_rec}")
check("T_REC_b: cost ledger row appended", after_count > before_count,
      f"before={before_count} after={after_count}")

# Verify the cost row details
cost_row = conn.execute(
    "SELECT * FROM llm_cost_ledger WHERE claim_id=? ORDER BY id DESC LIMIT 1",
    (rec_claim_id,)
).fetchone()
if cost_row:
    check("T_REC_c: stage is 'author'", cost_row["stage"] == "author",
          f"stage={cost_row['stage']}")
    check("T_REC_d: claim_kind is 'insight'", cost_row["claim_kind"] == "insight",
          f"claim_kind={cost_row['claim_kind']}")
    check("T_REC_e: tokens_in recorded", cost_row["tokens_in"] == 800,
          f"tokens_in={cost_row['tokens_in']}")
    check("T_REC_f: tokens_out recorded", cost_row["tokens_out"] == 300,
          f"tokens_out={cost_row['tokens_out']}")
else:
    check("T_REC_c: cost row found for claim", False, "no cost row found")

conn.close()


# ---------------------------------------------------------------------------
# T_MIG030: migration 030 applies cleanly and is idempotent
# ---------------------------------------------------------------------------

print("\n== T_MIG030: migration 030 cost ledger ==")

fd, mig_path = tempfile.mkstemp(suffix=".db", prefix="mig030-")
os.close(fd)
src = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
stmts = [r[0] for r in src.execute(
    "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
).fetchall()]
src.close()
mig_conn = sqlite3.connect(mig_path)
for s in stmts:
    try:
        mig_conn.execute(s)
    except sqlite3.OperationalError:
        pass
mig_conn.commit()

# First application
mig_file = MIGRATIONS / "030_grounding_llm_cost.sql"
try:
    _apply_migration(mig_conn, mig_file)
    mig_ok = True
except Exception as exc:
    mig_ok = False
    check("T_MIG030_a: migration 030 applies", False, str(exc))

if mig_ok:
    check("T_MIG030_a: migration 030 applies without error", True)
    # Table exists
    tables = {r[0] for r in mig_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_cost_ledger'"
    ).fetchall()}
    check("T_MIG030_b: llm_cost_ledger table created", "llm_cost_ledger" in tables)
    # Columns
    cols = {r[1] for r in mig_conn.execute("PRAGMA table_info(llm_cost_ledger)").fetchall()}
    for col in ("ts", "provider", "model", "stage", "tokens_in", "tokens_out",
                "est_cost_usd", "quota_type"):
        check(f"T_MIG030_col_{col}", col in cols, f"cols={cols}")
    # schema_version=30
    sv = mig_conn.execute("SELECT version FROM schema_version WHERE version=30").fetchone()
    check("T_MIG030_c: schema_version 30 recorded", sv is not None)
    # Idempotency
    try:
        _apply_migration(mig_conn, mig_file)
        check("T_MIG030_d: idempotent (second run safe)", True)
    except Exception as exc:
        check("T_MIG030_d: idempotent (second run safe)", False, str(exc))

mig_conn.close()
os.unlink(mig_path)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

# Clean up temp DB
try:
    os.unlink(TEMP_DB)
except Exception:
    pass

print(f"\n{'='*60}")
print(f"Results: {len(PASSES)} passed, {len(FAILURES)} failed")
if FAILURES:
    print("\nFAILURES:")
    for f in FAILURES:
        print(f"  {f}")
    sys.exit(1)
else:
    print("All tests passed.")
    sys.exit(0)
