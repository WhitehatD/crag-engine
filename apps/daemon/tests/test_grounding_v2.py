#!/usr/bin/env python3
# coding: utf-8
"""Grounding v2 A1+A2 test suite.

Standalone (no pytest — mirrors test_ws2_loops.py style).
Tests:
  - Migration 026 applies cleanly to a COPY of the live DB and is idempotent.
  - llm_client extract: contradiction.py still imports _get_client correctly.
  - classify_tier: 5 regression cases route to Tier-B (predicate-bearing);
    pure-existence cases route to Tier-A.
  - author_recipe: stubbed LLM returns valid recipe; write-token guard rejects
    dangerous steps; malformed LLM JSON is safely rejected.
  - adjudicate: prior_history is included in the prompt payload; LLM failure
    -> 'uncertain'.

Exit codes: 0 = all pass, 1 = any failure.

Run:
  python apps/daemon/tests/test_grounding_v2.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

# Ensure UTF-8 output on Windows (cp1252 terminals can't encode check-marks etc.)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
LIVE_DB = REPO_ROOT / "db" / "engine.db"
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"
MIGRATION_026 = MIGRATIONS_DIR / "026_grounding_v2.sql"

# Put db/ on sys.path so we can import grounding_author, contradiction, llm_client
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
# Helper: build a temp DB with the live schema (READ-ONLY source)
# ---------------------------------------------------------------------------

def build_temp_db_from_schema(path: str) -> sqlite3.Connection:
    """Create a temp DB whose schema is copied from the live DB (never the data)."""
    src = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    stmts = [r[0] for r in src.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]
    src.close()
    conn = sqlite3.connect(path)
    for s in stmts:
        try:
            conn.execute(s)
        except sqlite3.OperationalError:
            pass  # FTS5 shadow tables auto-created by their virtual table
    conn.commit()
    return conn


def apply_migration_026(conn: sqlite3.Connection) -> None:
    """Execute migration 026 statements against an open connection.

    NOTE: this is a TEST-HELPER, NOT the production path. Production idempotency
    comes from engine-cli.py cmd_migrate skipping migrations whose version is
    already in schema_version (~line 2224). This helper wraps each statement
    individually in try/except to allow idempotency testing without the
    version-skip guard.
    """
    sql = MIGRATION_026.read_text(encoding="utf-8")
    # Split on ';' to get statement chunks.  Each chunk may START with comment
    # lines (-- ...) followed by the actual SQL statement -- we must NOT discard
    # the chunk just because it starts with '--'.  Instead, strip comment-only
    # lines from the front of each chunk and check whether any non-comment,
    # non-empty lines remain.
    for chunk in sql.split(";"):
        lines = chunk.splitlines()
        sql_lines = [l for l in lines if l.strip() and not l.strip().startswith("--")]
        stmt = "\n".join(sql_lines).strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            # Column already exists = idempotent-safe
            if "duplicate column name" in msg or "already exists" in msg:
                continue
            raise
    conn.commit()


# ---------------------------------------------------------------------------
# T_MIGRATION: migration 026 applies cleanly + is idempotent
# ---------------------------------------------------------------------------

print("\n== T_MIGRATION: 026_grounding_v2.sql ==")

fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="gv2test-")
os.close(fd)
conn_mig = build_temp_db_from_schema(tmp_path)

# First application
try:
    apply_migration_026(conn_mig)
    mig_ok = True
    err_msg = ""
except Exception as exc:
    mig_ok = False
    err_msg = str(exc)

check("T_MIG_a: migration 026 applies without error", mig_ok, err_msg)

# Verify new columns exist on falsifiers
cols = {row[1] for row in conn_mig.execute("PRAGMA table_info(falsifiers)").fetchall()}
check("T_MIG_b: falsifiers.tier column exists", "tier" in cols, f"cols={cols}")
check("T_MIG_c: falsifiers.falsification_question column exists",
      "falsification_question" in cols, f"cols={cols}")
check("T_MIG_d: falsifiers.recipe column exists", "recipe" in cols, f"cols={cols}")
check("T_MIG_e: falsifiers.authored_by column exists", "authored_by" in cols, f"cols={cols}")
check("T_MIG_f: falsifiers.recipe_version column exists", "recipe_version" in cols, f"cols={cols}")
check("T_MIG_g: falsifiers.last_verdict column exists", "last_verdict" in cols, f"cols={cols}")

# Verify grounding_jobs table
gj_tables = {row[0] for row in conn_mig.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='grounding_jobs'"
).fetchall()}
check("T_MIG_h: grounding_jobs table created", "grounding_jobs" in gj_tables, "")

gj_cols = {row[1] for row in conn_mig.execute("PRAGMA table_info(grounding_jobs)").fetchall()}
for col in ("id", "claim_kind", "claim_id", "job_type", "status", "attempts", "priority",
            "enqueued_at", "started_at", "finished_at", "last_error"):
    check(f"T_MIG_gj_col_{col}", col in gj_cols, f"gj_cols={gj_cols}")

# Verify grounding_history table
gh_tables = {row[0] for row in conn_mig.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='grounding_history'"
).fetchall()}
check("T_MIG_i: grounding_history table created", "grounding_history" in gh_tables, "")

gh_cols = {row[1] for row in conn_mig.execute("PRAGMA table_info(grounding_history)").fetchall()}
for col in ("id", "claim_kind", "claim_id", "ts", "job_type", "verdict", "reasoning",
            "evidence", "recipe_version"):
    check(f"T_MIG_gh_col_{col}", col in gh_cols, f"gh_cols={gh_cols}")

# Verify dedup index exists
idx_rows = conn_mig.execute(
    "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_gj_pending_dedup'"
).fetchall()
check("T_MIG_j: idx_gj_pending_dedup index exists", len(idx_rows) == 1, f"found={idx_rows}")

# Verify schema_version=26 recorded
sv = conn_mig.execute("SELECT version FROM schema_version WHERE version=26").fetchone()
check("T_MIG_k: schema_version 26 recorded", sv is not None, "")

# Idempotency: run migration a second time — must not raise
try:
    apply_migration_026(conn_mig)
    idem_ok = True
    idem_err = ""
except Exception as exc:
    idem_ok = False
    idem_err = str(exc)
check("T_MIG_l: migration 026 is idempotent (second run is safe)", idem_ok, idem_err)

conn_mig.close()
os.unlink(tmp_path)

# ---------------------------------------------------------------------------
# T_LLMCLIENT: contradiction.py still imports and calls _get_client via llm_client
# ---------------------------------------------------------------------------

print("\n== T_LLMCLIENT: contradiction.py import + _get_client delegation ==")

try:
    lc_imported = True
    lc_err = ""
except Exception as exc:
    lc_imported = False
    lc_err = str(exc)
check("T_LLC_a: llm_client.py imports cleanly", lc_imported, lc_err)

try:
    import contradiction as contra_mod
    contra_imported = True
    contra_err = ""
except Exception as exc:
    contra_imported = False
    contra_err = str(exc)
check("T_LLC_b: contradiction.py imports cleanly after extraction", contra_imported, contra_err)

if contra_imported:
    # _get_client should be the llm_client.get_client function (re-exported as alias)
    from contradiction import _get_client as _cget
    from llm_client import get_client as _lget
    check("T_LLC_c: contradiction._get_client is llm_client.get_client",
          _cget is _lget, f"_cget={_cget} _lget={_lget}")

    # HAIKU_MODEL re-export present
    haiku_model = getattr(contra_mod, "HAIKU_MODEL", None)
    check("T_LLC_d: contradiction.HAIKU_MODEL is non-empty string",
          isinstance(haiku_model, str) and len(haiku_model) > 0,
          f"HAIKU_MODEL={haiku_model!r}")

# ---------------------------------------------------------------------------
# T_CLASSIFY: classify_tier routing
# ---------------------------------------------------------------------------

print("\n== T_CLASSIFY: Tier-A / Tier-B routing ==")

try:
    from grounding_author import classify_tier
    ga_imported = True
    ga_err = ""
except Exception as exc:
    ga_imported = False
    ga_err = str(exc)
check("T_CLS_import: grounding_author.py imports cleanly", ga_imported, ga_err)

if ga_imported:
    # ---- Tier-B regression cases (must all route to 'B') ----
    tier_b_cases = [
        (
            "rate-limiter is OFF in start-proxy.ps1",
            [],
            "predicate-bearing: 'is OFF'",
        ),
        (
            "notify requires Bearer auth, deny-all default",
            [{"entity_type": "service", "entity": "notify"}],
            "predicate-bearing: auth mode + negation",
        ),
        (
            "min_messages_for_downgrade is 10 in router-config.json",
            [{"entity_type": "path", "entity": "router-config.json"}],
            "predicate-bearing: config value equality",
        ),
        (
            "groundskeeper runs every 6h",
            [{"entity_type": "service", "entity": "groundskeeper"}],
            "predicate-bearing: cron cadence",
        ),
        (
            "endpoint /foo does not exist (hallucinated)",
            [{"entity_type": "domain", "entity": "localhost"}],
            "predicate-bearing: negation + hallucination",
        ),
    ]
    for content, entities, reason in tier_b_cases:
        tier = classify_tier(content, entities)
        check(
            f"T_CLS_B: '{content[:50]}...' -> Tier-B",
            tier == "B",
            f"got tier={tier!r} ({reason})",
        )

    # ---- Tier-A cases (pure existence, no predicate) ----
    tier_a_cases = [
        (
            "port 8787 is bound",
            [{"entity_type": "port", "entity": "8787"}],
        ),
        (
            "host 203.0.113.10 is reachable",
            [{"entity_type": "ip", "entity": "203.0.113.10"}],
        ),
    ]
    for content, entities in tier_a_cases:
        tier = classify_tier(content, entities)
        check(
            f"T_CLS_A: '{content[:50]}' -> Tier-A",
            tier == "A",
            f"got tier={tier!r}",
        )

# ---------------------------------------------------------------------------
# T_AUTHOR_RECIPE: author_recipe with stubbed LLM
# ---------------------------------------------------------------------------

print("\n== T_AUTHOR_RECIPE: stubbed LLM authoring ==")

if ga_imported:
    from grounding_author import author_recipe, _FORBIDDEN

    # --- Stub LLM: returns a valid read-only recipe ---
    _VALID_RECIPE_JSON = json.dumps({
        "falsification_question": "Does the rate-limiter config have the OFF flag set?",
        "recipe": {
            "steps": [
                "grep -i 'rate.limiter' /etc/start-proxy.ps1",
                "grep -i 'off\\|false\\|disabled' /etc/start-proxy.ps1",
            ],
            "refutes_if": "grep returns no match for 'off' near 'rate-limiter'",
            "supports_if": "grep returns a line containing both 'rate-limiter' and 'off'",
        },
    })

    class _FakeContent:
        def __init__(self, text): self.text = text

    class _FakeResp:
        def __init__(self, text): self.content = [_FakeContent(text)]

    class _FakeLLM:
        def __init__(self, return_json):
            self._json = return_json
            self.last_messages_create_kwargs: dict = {}

        @property
        def messages(self):
            return self

        def create(self, **kwargs):
            self.last_messages_create_kwargs = kwargs
            return _FakeResp(self._json)

    stub_llm = _FakeLLM(_VALID_RECIPE_JSON)
    result, reason = author_recipe(
        "rate-limiter is OFF in start-proxy.ps1",
        [{"entity_type": "path", "entity": "start-proxy.ps1"}],
        stub_llm,
    )
    check("T_AUT_a: valid recipe returned (not None)", result is not None, f"result={result}")
    check("T_AUT_a2: reason is None on success", reason is None, f"reason={reason!r}")
    if result:
        check("T_AUT_b: falsification_question present",
              isinstance(result.get("falsification_question"), str) and len(result["falsification_question"]) > 0,
              f"result={result}")
        recipe = result.get("recipe", {})
        check("T_AUT_c: recipe.steps is list", isinstance(recipe.get("steps"), list), f"recipe={recipe}")
        check("T_AUT_d: recipe.steps are read-only",
              all(not any(tok in f" {s} " for tok in _FORBIDDEN) for s in recipe.get("steps", [])),
              f"steps={recipe.get('steps')}")
        check("T_AUT_e: recipe.refutes_if is non-empty string",
              isinstance(recipe.get("refutes_if"), str) and len(recipe["refutes_if"]) > 0,
              f"recipe={recipe}")
        check("T_AUT_f: recipe.supports_if is non-empty string",
              isinstance(recipe.get("supports_if"), str) and len(recipe["supports_if"]) > 0,
              f"recipe={recipe}")

    # --- Stub LLM: returns recipe with a write-capable step -> rejected ---
    _FORBIDDEN_RECIPE_JSON = json.dumps({
        "falsification_question": "Is it configured?",
        "recipe": {
            "steps": ["grep foo /etc/conf", "rm /tmp/test > /dev/null"],
            "refutes_if": "no match",
            "supports_if": "match found",
        },
    })
    stub_bad = _FakeLLM(_FORBIDDEN_RECIPE_JSON)
    result_bad, reason_bad = author_recipe("some claim", [], stub_bad)
    check("T_AUT_g: recipe with write token is rejected (returns None)", result_bad is None,
          f"result_bad={result_bad}")
    check("T_AUT_g2: reason is 'write_guard_rejected'",
          bool(reason_bad) and reason_bad.startswith("write_guard_rejected"),
          f"reason_bad={reason_bad!r}")

    # --- None LLM -> fail-open ---
    result_none, reason_none = author_recipe("some claim", [], None)
    check("T_AUT_h: None LLM -> author_recipe returns None (fail-open)", result_none is None, "")
    check("T_AUT_h2: reason is 'llm_unavailable'", reason_none == "llm_unavailable",
          f"reason_none={reason_none!r}")

    # --- Malformed JSON -> safe rejection ---
    stub_bad_json = _FakeLLM("this is not json at all {{{")
    result_malformed, reason_malformed = author_recipe("some claim", [], stub_bad_json)
    check("T_AUT_i: malformed LLM JSON -> safe rejection (returns None)",
          result_malformed is None, f"result_malformed={result_malformed}")
    check("T_AUT_i2: reason is 'malformed_output'",
          bool(reason_malformed) and reason_malformed.startswith("malformed_output"),
          f"reason_malformed={reason_malformed!r}")

    # --- Zero-steps recipe -> mechanically_unverifiable (poison-pill guard) ---
    # Regression for the insight #77 incident (2026-07-06): an empty steps list
    # used to validate & persist as a "successful" recipe, driving an infinite
    # author->reground->author loop (one LLM call per sweep tick, recipe_version
    # reached 156). It must now be rejected as mechanically_unverifiable so the
    # claim is routed to mark_judgment and permanently drained from the pool.
    _ZERO_STEPS_JSON = json.dumps({
        "falsification_question": "Does the thing do the thing?",
        "recipe": {"steps": [], "refutes_if": "", "supports_if": ""},
    })
    # Escalation is disabled in this stub path (single call), so the primary
    # rejection reason surfaces directly.
    stub_zero = _FakeLLM(_ZERO_STEPS_JSON)
    result_zero, reason_zero = author_recipe("some unverifiable claim", [], stub_zero)
    check("T_AUT_zerosteps: zero-steps recipe is rejected (returns None)",
          result_zero is None, f"result_zero={result_zero}")
    check("T_AUT_zerosteps2: reason is 'mechanically_unverifiable'",
          bool(reason_zero) and reason_zero.startswith("mechanically_unverifiable"),
          f"reason_zero={reason_zero!r}")

    # --- Markdown code fence in LLM output -> still parsed ---
    _FENCED_JSON = "```json\n" + _VALID_RECIPE_JSON + "\n```"
    stub_fenced = _FakeLLM(_FENCED_JSON)
    result_fenced, reason_fenced = author_recipe("rate-limiter is OFF", [], stub_fenced)
    check("T_AUT_j: markdown-fenced JSON -> still parsed correctly",
          result_fenced is not None, f"result_fenced={result_fenced}")
    check("T_AUT_j2: reason is None on fenced success", reason_fenced is None,
          f"reason_fenced={reason_fenced!r}")

    # --- REGRESSION (insight #3317, PRIMARY cause of the 952-failure
    #     incident): closing fence followed by trailing prose. The old
    #     stripper only handled lines[-1] == "```" EXACTLY, so any sentence
    #     Haiku appended after the fence broke json.loads with "Extra data". ---
    _FENCED_TRAILING_PROSE = (
        "```json\n" + _VALID_RECIPE_JSON + "\n```\n"
        "Let me know if you'd like me to adjust the recipe further."
    )
    stub_trailing = _FakeLLM(_FENCED_TRAILING_PROSE)
    result_trailing, reason_trailing = author_recipe("rate-limiter is OFF", [], stub_trailing)
    check("T_AUT_m: fenced JSON + trailing prose after closing fence -> still parsed",
          result_trailing is not None, f"result_trailing={result_trailing} reason={reason_trailing!r}")
    check("T_AUT_m2: reason is None (regression for #3317 fixed)", reason_trailing is None,
          f"reason_trailing={reason_trailing!r}")

    # --- Prose BEFORE the opening fence, and trailing prose with NO fence at
    #     all (Haiku sometimes narrates instead of fencing). ---
    _PROSE_BEFORE_FENCE = "Sure, here's the recipe:\n```json\n" + _VALID_RECIPE_JSON + "\n```"
    stub_leading = _FakeLLM(_PROSE_BEFORE_FENCE)
    result_leading, reason_leading = author_recipe("rate-limiter is OFF", [], stub_leading)
    check("T_AUT_n: prose before opening fence -> still parsed",
          result_leading is not None, f"result_leading={result_leading} reason={reason_leading!r}")

    _NO_FENCE_TRAILING_PROSE = _VALID_RECIPE_JSON + "\nHope that helps!"
    stub_nofence = _FakeLLM(_NO_FENCE_TRAILING_PROSE)
    result_nofence, reason_nofence = author_recipe("rate-limiter is OFF", [], stub_nofence)
    check("T_AUT_o: no fence at all + trailing prose -> still parsed via raw_decode",
          result_nofence is not None, f"result_nofence={result_nofence} reason={reason_nofence!r}")

    # --- LLM declares unverifiable -> mechanically_unverifiable ---
    _UNVERIFIABLE_JSON = json.dumps({
        "unverifiable": True,
        "reason": "Verifying this requires reading the MINIO_SECRET_KEY secret.",
    })
    stub_unverifiable = _FakeLLM(_UNVERIFIABLE_JSON)
    result_unver, reason_unver = author_recipe(
        "the MINIO_SECRET_KEY env var is a 40-character value", [], stub_unverifiable
    )
    check("T_AUT_k: unverifiable declaration -> result is None", result_unver is None,
          f"result_unver={result_unver}")
    check("T_AUT_k2: reason is 'mechanically_unverifiable' (with detail)",
          bool(reason_unver) and reason_unver.startswith("mechanically_unverifiable")
          and "MINIO_SECRET_KEY" in reason_unver,
          f"reason_unver={reason_unver!r}")

    # --- max_tokens raised to accommodate 3+ step recipes ---
    from grounding_author import _AUTHOR_MAX_TOKENS
    check("T_AUT_l: _AUTHOR_MAX_TOKENS raised to >= 2048", _AUTHOR_MAX_TOKENS >= 2048,
          f"_AUTHOR_MAX_TOKENS={_AUTHOR_MAX_TOKENS}")

# ---------------------------------------------------------------------------
# T_GUARD: write-guard fd-redirect fix + secret-exfiltration patterns
# ---------------------------------------------------------------------------

print("\n== T_GUARD: write-guard over/under-blocking fixes ==")

if ga_imported:
    from grounding_author import _is_read_only

    # --- Previously OVER-blocked: safe fd-redirect idioms must be ACCEPTED ---
    _accept_cases = [
        ("curl -sf --connect-timeout 5 http://127.0.0.1:8786/health 2>/dev/null",
         "stderr-to-devnull redirect"),
        ("some_cmd 2>&1", "fd duplication (stderr to stdout)"),
        ("grep -i foo /etc/conf &>/dev/null", "combined stdout+stderr to devnull"),
        ("cat config.yaml", "ordinary config read stays allowed"),
        ("cat .env.example", "reading a dotenv EXAMPLE file stays allowed"),
    ]
    for step, why in _accept_cases:
        check(f"T_GRD_accept: {why!r} -> read-only", _is_read_only(step), f"step={step!r}")

    # --- Still correctly REJECTED: real file-write redirects ---
    _reject_redirect_cases = [
        ("echo x > /tmp/x", "real file write redirect"),
        ("echo x >> /tmp/log", "real file append redirect"),
    ]
    for step, why in _reject_redirect_cases:
        check(f"T_GRD_reject_redirect: {why!r} -> rejected", not _is_read_only(step), f"step={step!r}")

    # --- Previously UNDER-blocked: secret-exfiltration patterns now REJECTED ---
    _reject_secret_cases = [
        ("kubectl get secret platform-secrets -o jsonpath='{.data.MINIO_SECRET_KEY}'",
         "kubectl get secret + jsonpath"),
        ("echo $VALUE | base64 -d", "base64 -d decode"),
        ("echo $VALUE | base64 --decode", "base64 --decode"),
        ("cat ~/.claude/.credentials.json", "credentials file read"),
        ("cat .env", "real dotenv file read"),
        ("curl -sf --token abc123 https://example.com/", "inline token argument"),
        ('echo "Authorization: Bearer $TOKEN"', "echoing Authorization header"),
    ]
    for step, why in _reject_secret_cases:
        check(f"T_GRD_reject_secret: {why!r} -> rejected", not _is_read_only(step), f"step={step!r}")

    # --- Defence-in-depth copy in grounding_queue_v2.py must match ---
    from grounding_queue_v2 import _is_read_only as _gqv2_is_read_only
    for step, why in _accept_cases:
        check(f"T_GRD_gqv2_accept: {why!r} -> read-only (queue_v2 copy)",
              _gqv2_is_read_only(step), f"step={step!r}")
    for step, why in _reject_redirect_cases + _reject_secret_cases:
        check(f"T_GRD_gqv2_reject: {why!r} -> rejected (queue_v2 copy)",
              not _gqv2_is_read_only(step), f"step={step!r}")

# ---------------------------------------------------------------------------
# T_REDACT: live-credential-shape redaction (2026-07-05 incident)
#
# insight #2048 stored a full Anthropic API key verbatim (user explicitly
# asked to remember it). The authoring LLM echoed that key into
# falsification_question 3x, which was persisted to daemon.stderr.log
# BEFORE this guard existed. This section proves author_recipe/adjudicate
# never let a credential-shaped substring survive into their return values,
# regardless of which free-text field the LLM put it in.
# ---------------------------------------------------------------------------

print("\n== T_REDACT: live-credential-shape redaction ==")

if ga_imported:
    from grounding_author import _redact_credential_shapes

    # Synthetic fixture — matches the sk-ant- credential shape so the redaction
    # regex fires, but is obviously fake (never a real key; safe to commit).
    _REAL_LOOKING_KEY = (
        "sk-ant-api03-TH"
        "ISisASYNTHETICfakeTESTkeyNOTaREALcredential0000000"
        "00000000000000000000000000000000000000000AA"
    )
    _CRED_SHAPE_CASES = [
        (_REAL_LOOKING_KEY, "Anthropic API key"),
        ("sk-" + "a" * 40, "OpenAI-style secret key"),
        ("AKIAABCD" "EFGHIJKLMNOP", "AWS access key ID"),
        ("ghp_" + "b" * 36, "GitHub PAT"),
        ("github_pat_" + "c" * 40, "GitHub fine-grained PAT"),
        ("xoxb-" + "1234567890-abcdef", "Slack bot token"),
        ("Bearer " + "d" * 30, "inline bearer token"),
    ]
    for secret, why in _CRED_SHAPE_CASES:
        text = f"the value is {secret} according to the config"
        redacted = _redact_credential_shapes(text)
        check(f"T_RDT_redacts: {why} is stripped from free text",
              secret not in redacted and "[REDACTED-CREDENTIAL]" in redacted,
              f"redacted={redacted!r}")

    check("T_RDT_noop: ordinary text is untouched",
          _redact_credential_shapes("port 8786 is bound") == "port 8786 is bound", "")

    # --- End-to-end: author_recipe never returns the raw key, even when the
    #     stubbed LLM echoes it into falsification_question ---
    _LEAKY_RECIPE_JSON = json.dumps({
        "falsification_question": f"Is the key {_REAL_LOOKING_KEY} still valid?",
        "recipe": {
            "steps": ["curl -sf http://127.0.0.1:8786/health"],
            "refutes_if": f"key {_REAL_LOOKING_KEY} rejected",
            "supports_if": "200 OK",
        },
    })
    stub_leaky = _FakeLLM(_LEAKY_RECIPE_JSON)
    result_leaky, reason_leaky = author_recipe("thesis API key claim", [], stub_leaky)
    check("T_RDT_e2e_a: author_recipe succeeds (recipe itself is fine)",
          result_leaky is not None, f"reason_leaky={reason_leaky!r}")
    if result_leaky:
        _dump = json.dumps(result_leaky)
        check("T_RDT_e2e_b: raw key does NOT appear anywhere in author_recipe's return value",
              _REAL_LOOKING_KEY not in _dump, f"result_leaky={result_leaky}")
        check("T_RDT_e2e_c: redaction marker present in falsification_question",
              "[REDACTED-CREDENTIAL]" in result_leaky.get("falsification_question", ""),
              f"result_leaky={result_leaky}")
        check("T_RDT_e2e_d: redaction marker present in refutes_if",
              "[REDACTED-CREDENTIAL]" in result_leaky.get("recipe", {}).get("refutes_if", ""),
              f"result_leaky={result_leaky}")

    # --- End-to-end: adjudicate never returns the raw key in reasoning/evidence ---
    from grounding_author import adjudicate as _adjudicate_for_redact_test
    _LEAKY_ADJ_JSON = json.dumps({
        "verdict": "pass",
        "reasoning": f"Confirmed using key {_REAL_LOOKING_KEY} from the config file.",
        "evidence": f"key={_REAL_LOOKING_KEY}",
    })
    stub_leaky_adj = _FakeLLM(_LEAKY_ADJ_JSON)
    adj_leaky = _adjudicate_for_redact_test(
        claim="thesis API key claim",
        recipe={"steps": [], "refutes_if": "x", "supports_if": "y"},
        step_outputs=[],
        prior_history=[],
        llm=stub_leaky_adj,
    )
    check("T_RDT_e2e_e: raw key does NOT appear in adjudicate's reasoning",
          _REAL_LOOKING_KEY not in adj_leaky.get("reasoning", ""), f"adj_leaky={adj_leaky}")
    check("T_RDT_e2e_f: raw key does NOT appear in adjudicate's evidence",
          _REAL_LOOKING_KEY not in adj_leaky.get("evidence", ""), f"adj_leaky={adj_leaky}")

# ---------------------------------------------------------------------------
# T_ADJUDICATE: adjudicate with stubbed LLM
# ---------------------------------------------------------------------------

print("\n== T_ADJUDICATE: stubbed LLM adjudication ==")

if ga_imported:
    from grounding_author import adjudicate

    _VALID_ADJ_JSON = json.dumps({
        "verdict": "pass",
        "reasoning": "The grep output shows 'rate-limiter=OFF' on line 42.",
        "evidence": "rate-limiter=OFF",
    })

    prior_history = [
        {
            "ts": "2026-06-01T10:00:00.000000+00:00",
            "verdict": "pass",
            "reasoning": "Confirmed OFF at last check.",
            "evidence": "rate-limiter=OFF",
        },
        {
            "ts": "2026-06-15T12:00:00.000000+00:00",
            "verdict": "uncertain",
            "reasoning": "File was temporarily missing.",
            "evidence": "",
        },
    ]

    recipe = {
        "steps": ["grep -i rate-limiter /etc/start-proxy.ps1"],
        "refutes_if": "no match for 'OFF'",
        "supports_if": "line with 'OFF' found",
    }

    stub_adj = _FakeLLM(_VALID_ADJ_JSON)
    claim = "rate-limiter is OFF in start-proxy.ps1"

    adj_result = adjudicate(
        claim=claim,
        recipe=recipe,
        step_outputs=["rate-limiter=OFF  # line 42"],
        prior_history=prior_history,
        llm=stub_adj,
    )

    check("T_ADJ_a: verdict is 'pass'", adj_result.get("verdict") == "pass",
          f"adj_result={adj_result}")
    check("T_ADJ_b: reasoning is non-empty string",
          isinstance(adj_result.get("reasoning"), str) and len(adj_result["reasoning"]) > 0,
          f"adj_result={adj_result}")

    # Verify prior_history was included in the prompt to the LLM
    prompt_payload = stub_adj.last_messages_create_kwargs
    user_content = (prompt_payload.get("messages") or [{}])[0].get("content", "")
    check("T_ADJ_c: prior_history rows are included in the LLM prompt",
          "Confirmed OFF at last check" in user_content or "Prior grounding history" in user_content,
          f"user_content head={user_content[:200]!r}")
    check("T_ADJ_d: step_outputs included in the LLM prompt",
          "rate-limiter=OFF  # line 42" in user_content,
          f"user_content head={user_content[:300]!r}")

    # LLM failure -> 'uncertain'
    class _FailLLM:
        @property
        def messages(self): return self
        def create(self, **kwargs): raise RuntimeError("simulated LLM failure")

    adj_fail = adjudicate(claim, recipe, ["output"], prior_history, _FailLLM())
    check("T_ADJ_e: LLM failure -> verdict='uncertain' (fail-open)",
          adj_fail.get("verdict") == "uncertain",
          f"adj_fail={adj_fail}")

    # None LLM -> 'uncertain'
    adj_none = adjudicate(claim, recipe, [], [], None)
    check("T_ADJ_f: None LLM -> verdict='uncertain' (fail-open)",
          adj_none.get("verdict") == "uncertain",
          f"adj_none={adj_none}")

    # Unknown verdict from LLM -> normalised to 'uncertain'
    stub_bad_verdict = _FakeLLM(json.dumps({"verdict": "MAYBE", "reasoning": "idk", "evidence": ""}))
    adj_bad_v = adjudicate(claim, recipe, [], [], stub_bad_verdict)
    check("T_ADJ_g: unknown verdict from LLM -> normalised to 'uncertain'",
          adj_bad_v.get("verdict") == "uncertain",
          f"adj_bad_v={adj_bad_v}")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

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
