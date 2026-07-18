"""
db/capabilities.py — crag Anchor capability manifest (Phase D).

Single source of truth that drives:
  • MCP tool descriptions (mcp-server.py imports _TOOLS_MANIFEST)
  • OpenAPI summary fields (daemon generates from this)
  • GET /llms.txt — human+LLM-readable surface
  • GET /guide — structured JSON guide
  • MCP engine_guide tool
  • MCP engine://guide resource

Any new endpoint or tool MUST be registered here first.
"""

# ---------------------------------------------------------------------------
# Version string — bump when surface changes
# ---------------------------------------------------------------------------
MANIFEST_VERSION = "2.0.0"

# ---------------------------------------------------------------------------
# Tool surface: authoritative list of all 25 MCP tools in C2+C3+D surface.
# Format per tool:
#   name        str  — matches MCP tool name exactly
#   description str  — plain-text, ≤ 280 chars (fits one tweet; LLMs truncate)
#   category    str  — grouping for /guide output
#   required    list — required parameter names
#   params      dict — {param_name: "brief description"}
# ---------------------------------------------------------------------------
TOOLS_MANIFEST: list[dict] = [
    # ── Recall / search ────────────────────────────────────────────────────
    {
        "name": "recall",
        "category": "recall",
        "description": (
            "Search cross-session memory by meaning. Returns top-K ranked insights plus matching "
            "principles. Call on errors, before non-trivial work, or when past sessions are referenced."
        ),
        "required": ["query"],
        "params": {
            "query": "Natural-language query",
            "project": "Project scope (e.g. 'infra'). Omit for cross-project search.",
            "topk": "Number of top insights to return (1-20)",
            "session_id": "Session id for telemetry (optional)",
            "role": "coordinator|subagent|operator (optional)",
            "epic_tag": "Sprint/epic label (optional)",
        },
    },
    {
        "name": "recall_principle",
        "category": "recall",
        "description": (
            "Search distilled principles — the highest-trust safety rules. Call BEFORE risky "
            "operations (VPS, proxies, services, deploys)."
        ),
        "required": ["topic"],
        "params": {
            "topic": "Topic to search principles for",
            "project": "Project scope (optional)",
        },
    },
    {
        "name": "recall_by_entity",
        "category": "recall",
        "description": (
            "Recall insights and principles referencing an exact entity (port, IP, file, service...). "
            "Call before changing a port, service, or config you may have touched before."
        ),
        "required": ["entity"],
        "params": {
            "entity": "Entity value, e.g. '8090' or 'nginx'",
            "entity_type": "port|ip|domain|path|service|classname|env_var|file",
            "project": "Project scope (optional)",
            "limit": "Max rows (default 20)",
        },
    },
    {
        "name": "recall_stats",
        "category": "recall",
        "description": (
            "crag Anchor usage telemetry: hottest insights, top queries, dead-weight and cross-project "
            "promotion candidates. Weekly memory-health check."
        ),
        "required": [],
        "params": {
            "project": "Project scope (optional)",
            "days": "Lookback window in days (default 7)",
        },
    },
    {
        "name": "recent_insights",
        "category": "recall",
        "description": (
            "List recent insights for a project ordered by creation, confidence, or recall frequency. "
            "Use at session end to spot promotion/distillation candidates."
        ),
        "required": [],
        "params": {
            "project": "Project scope (optional)",
            "days": "Recency window in days (default 30)",
            "type": "Type filter, e.g. gotcha|pattern|decision",
            "limit": "Max rows (default 50)",
            "order_by": "created_desc|confidence_desc|recalled_desc",
        },
    },
    {
        "name": "list_principles",
        "category": "recall",
        "description": (
            "List a project's principles sorted by confidence DESC, optional substring filter. "
            "Keep limit <= 20 to avoid flooding context."
        ),
        "required": [],
        "params": {
            "project": "Project scope (optional)",
            "limit": "Max rows; keep <= 20 (50 overflows context)",
            "q": "Content substring filter (optional)",
        },
    },
    # ── Write / lifecycle ──────────────────────────────────────────────────
    {
        "name": "save_insight",
        "category": "write",
        "description": (
            "Persist a memory across sessions; dedup-guarded and auto-embedded for recall. "
            "Save confirmed root causes, gotchas, patterns, decisions, and corrective user feedback."
        ),
        "required": ["content"],
        "params": {
            "content": "Full insight text — detailed enough to be actionable later",
            "type": "gotcha|pattern|architecture|decision|bug-fix|tool|feedback|user-context|project-context|reference",
            "tags": "Comma-separated tags",
            "source_file": "File path this insight refers to (optional)",
            "project": "Project scope (optional)",
            "force": "Bypass the dedup guard",
            "role": "coordinator|subagent|operator (optional)",
            "epic_tag": "Sprint/epic label (optional)",
            "session_id": "Session id (optional)",
        },
    },
    {
        "name": "suggest_tags",
        "category": "write",
        "description": (
            "Suggest existing tags for draft insight content by semantic similarity. "
            "Call before save_insight to avoid creating near-duplicate tag variants."
        ),
        "required": ["content"],
        "params": {
            "content": "Draft insight text",
            "limit": "Max suggestions (default 5)",
            "project": "Project scope (optional)",
        },
    },
    {
        "name": "add_token_record",
        "category": "write",
        "description": (
            "Append one session row to the token ledger (cost plus recall hit/miss counters). "
            "Call at the end of every session."
        ),
        "required": ["project"],
        "params": {
            "project": "Project slug",
            "session_id": "Stable session UUID",
            "task_summary": "One-line description of session work",
            "tokens_in": "Total input tokens",
            "tokens_out": "Total output tokens",
            "cache_hits": "Cache-hit count",
            "cache_misses": "Cache-miss count",
            "rtk_savings_pct": "RTK token reduction %",
            "headroom_savings_pct": "Headroom savings %",
            "wall_time_sec": "Wall clock seconds",
            "model": "Model ID used",
            "recall_hits": "Recalls that materially changed the approach",
            "recall_misses": "Recalls that returned nothing useful",
            "repeated_errors": "Errors already described by a saved insight",
            "novel_saves": "Net-new insights saved this session",
        },
    },
    # ── Batch / merged tools ────────────────────────────────────────────────
    {
        "name": "get",
        "category": "batch",
        "description": (
            "Fetch insights or principles by explicit ID — no semantic search, single bulk query. "
            "Returns {found, not_found}. Use for IDs from recall conflicts, audits, or '#NNNN' references."
        ),
        "required": ["kind", "ids"],
        "params": {
            "kind": "insight or principle",
            "ids": "List of integer IDs to fetch",
        },
    },
    {
        "name": "verify",
        "category": "batch",
        "description": (
            "Confirm or contradict a claim after acting on it. 'verified' raises confidence, "
            "'stale' lowers it (insight +0.1/-0.2; principle +0.05/-0.1, gentler because curated)."
        ),
        "required": ["kind", "id", "status"],
        "params": {
            "kind": "insight or principle",
            "id": "Row ID",
            "status": "verified = still true; stale = contradicted by reality",
        },
    },
    {
        "name": "update",
        "category": "batch",
        "description": (
            "Patch an insight or principle in place; ID unchanged, re-embeds on content change. "
            "confidence is principle-only; source_file is insight-only. Supply at least one field."
        ),
        "required": ["kind", "id"],
        "params": {
            "kind": "insight or principle",
            "id": "Row ID",
            "content": "Full replacement content",
            "tags": "Comma-separated tags",
            "source_file": "Insight-only: file path the insight refers to",
            "confidence": "Principle-only: new confidence 0.0-1.0",
        },
    },
    {
        "name": "supersede",
        "category": "batch",
        "description": (
            "Mark one insight/principle superseded by another of the SAME kind when ground truth proves "
            "it wrong. Loser leaves recall but stays queryable by ID for audit."
        ),
        "required": ["kind", "loser_id", "winner_id"],
        "params": {
            "kind": "insight or principle",
            "loser_id": "Row being retired",
            "winner_id": "Row that replaces it",
            "reason": "Why the loser is wrong (audit trail)",
        },
    },
    {
        "name": "arena",
        "category": "batch",
        "description": (
            "Adjudicate contradicting insight groups; losers are superseded by the winner. "
            "pairs is a list of ID groups — one entry for a single pair. Run dry_run=true first to review verdicts."
        ),
        "required": ["pairs", "strategy"],
        "params": {
            "pairs": "ID groups to adjudicate, e.g. [[12, 34]] or [[1, 2], [3, 4]]",
            "strategy": "auto=majority vote; recency=newest wins; merge needs merged_content",
            "dry_run": "Preview verdicts without writing",
            "merged_content": "Required for strategy='merge': the merged insight text",
            "project": "Project scope (optional)",
        },
    },
    {
        "name": "clear_suspect",
        "category": "batch",
        "description": (
            "Mark flagged contradiction pairs as FALSE POSITIVES and clear them from the audit queue. "
            "Entries are {id} or {a_id, b_id}; use after reading both sides of each pair."
        ),
        "required": ["pairs"],
        "params": {
            "pairs": "Entries of shape {id} or {a_id, b_id}; per-entry reason optional",
            "reason": "Default reason for entries that omit one",
        },
    },
    {
        "name": "audit",
        "category": "batch",
        "description": (
            "List open review queues. kind='contradictions': flagged pairs awaiting triage. "
            "kind='grounding': drifted-claim queue. "
            "kind='drift': insights matching a stale pattern (pattern required)."
        ),
        "required": ["kind"],
        "params": {
            "kind": "contradictions = suspect pairs; grounding = drifted claims; drift = stale-pattern scan",
            "project": "Project scope (optional)",
            "pattern": "drift only (required): SQL LIKE substring, e.g. an old IP",
            "limit": "audit only: max rows (default 100)",
        },
    },
    {
        "name": "grounding",
        "category": "batch",
        "description": (
            "Grounded-memory triage + autonomous resolution. audit=drift queue; check=falsifier+"
            "liveness; clear=resolve row; jobs=work queue; history=chain-of-thought; stats=throughput; "
            "proposals=pending escalations; decide=approve|reject; resolutions=recent; revert=undo; "
            "economics=LLM cost."
        ),
        "required": ["action"],
        "params": {
            "action": "audit|check|clear|jobs|history|stats|proposals|resolutions|economics|decide|revert",
            "project": "audit only: scope to a project (optional)",
            "limit": "audit/jobs/proposals/resolutions: max rows (default 100)",
            "claim_kind": "check/clear/history: kind of the claim (insight|principle)",
            "claim_id": "check/clear/history: ID of the claim",
            "resolution": "clear only: verified|dismissed|noted",
            "grounded_against": "clear only: what was checked and passed",
            "reason": "clear only: optional explanation",
            "status": "jobs/proposals: filter by status (pending|running|done|failed|approved|rejected)",
            "proposal_id": "decide only: ID of the resolution_proposal to act on",
            "decision": "decide only: approve|reject",
            "resolution_id": "revert only: ID of the autonomous resolution to undo",
        },
    },
    {
        "name": "promote_insight",
        "category": "write",
        "description": (
            "Promote insight(s) to a principle seeded at confidence 0.9. One ID = fast-path promote "
            "(optional content override); 2+ IDs = merge into one principle (content required)."
        ),
        "required": ["insight_ids"],
        "params": {
            "insight_ids": "Insight IDs; a single ID promotes, several merge",
            "content": "Principle text. Optional override for 1 ID; REQUIRED for 2+",
            "project": "Project for a merged principle (optional)",
        },
    },
    {
        "name": "health_check",
        "category": "ops",
        "description": (
            "Structured self-check of the crag Anchor across 5 failure classes (proxy cord, embedding "
            "backlog, DB corruption, token ledger, VPS tunnel). Call before risky operations."
        ),
        "required": [],
        "params": {},
    },
    # ── C2 operator-lifecycle tools ────────────────────────────────────────
    {
        "name": "session_diary",
        "category": "lifecycle",
        "description": (
            "Read or write session diary entries. action='add' appends a session record; "
            "action='get' returns recent sessions for a project (default last 5)."
        ),
        "required": ["action"],
        "params": {
            "action": "add|get",
            "project": "Project slug (required for add; filter for get)",
            "date": "add: ISO date string (default today)",
            "accomplished": "add: what was completed",
            "files_changed": "add: comma-separated file list",
            "commits": "add: commit SHAs or messages",
            "decisions": "add: key decisions made",
            "problems": "add: open issues",
            "next_steps": "add: planned follow-on work",
            "session_uuid": (
                "add: Claude session UUID (migration 029) — when set, enriches the ONE "
                "canonical sessions row for this session instead of inserting a new "
                "fragmented date-row. Omit for legacy/manual behavior."
            ),
            "limit": "get: max rows to return (default 5)",
        },
    },
    {
        "name": "project_context",
        "category": "lifecycle",
        "description": (
            "Read or write structured project context (branch, status, known issues, backlog). "
            "action='get' returns current context; action='set' upserts fields (null fields unchanged)."
        ),
        "required": ["action", "project"],
        "params": {
            "action": "get|set",
            "project": "Project slug",
            "status": "set: one-line project status",
            "current_branch": "set: active git branch",
            "last_session_date": "set: ISO date of last session",
            "architecture_decisions": "set: free-text ADR summary",
            "known_issues": "set: open bugs / blockers",
            "backlog": "set: comma-separated next tasks",
        },
    },
    {
        "name": "events",
        "category": "lifecycle",
        "description": (
            "Manage the pending-events queue. action='list' returns pending events; "
            "'add' enqueues a new event; 'claim' marks claimed; 'complete' marks done/failed; "
            "'bulk_expire' expires all matching project/priority."
        ),
        "required": ["action"],
        "params": {
            "action": "list|add|claim|complete|bulk_expire",
            "project": "Project scope filter / target",
            "priority": "critical|high|normal|low (filter or value)",
            "limit": "list: max rows (default 20)",
            "source": "add: event source label",
            "event_type": "add: machine type string",
            "summary": "add: human-readable one-liner",
            "payload": "add: optional JSON payload string",
            "expires_at": "add: optional ISO expiry timestamp",
            "id": "claim/complete: event ID",
            "claimed_by": "claim: identifier of claiming agent",
            "status": "complete: completed|failed",
            "result": "complete: brief outcome text",
        },
    },
    {
        "name": "cost_report",
        "category": "lifecycle",
        "description": (
            "Token/cost report from the ledger: totals, per-project breakdown, 7-day trend. "
            "Use on 'cost report' / 'token stats' / 'how much am I spending'."
        ),
        "required": [],
        "params": {
            "project": "Scope to one project (optional)",
            "days": "Lookback window in days (default 7)",
        },
    },
    # ── C3 coordinator brief ───────────────────────────────────────────────
    {
        "name": "brief",
        "category": "lifecycle",
        "description": (
            "ONE call returning a coordinator pre-flight brief: top principles, pending events, "
            "grounding flags, last session summary, token-ledger nudge. Target <200 ms. "
            "Call at session start instead of four separate warm-up queries."
        ),
        "required": ["project"],
        "params": {
            "project": "Project slug for scoped brief",
        },
    },
    # ── P0 deterministic session lifecycle ─────────────────────────────────
    {
        "name": "session_start",
        "category": "lifecycle",
        "description": (
            "Deterministic-loop context payload for session start: ONE composed bundle — "
            "overview (trust/counts/today), top true-t2 needs-you items, stale-rules count, "
            "and the last session diary row. Invoked by the harness SessionStart hook via the CLI."
        ),
        "required": [],
        "params": {
            "project": "Project scope (optional)",
        },
    },
    {
        "name": "session_end",
        "category": "lifecycle",
        "description": (
            "Deterministic-loop end-capture: records a session-end marker and returns the payoff "
            "numbers (captured/verified/promoted today). FAST + fail-open — invoked by the harness "
            "SessionEnd hook via the CLI."
        ),
        "required": [],
        "params": {
            "project": "Project scope (optional)",
            "session_id": "Session UUID (optional)",
            "summary": "One-line session summary (optional)",
        },
    },
    # ── D self-describing ─────────────────────────────────────────────────
    {
        "name": "engine_guide",
        "category": "meta",
        "description": (
            "Return a structured JSON guide to all crag Anchor tools, endpoints, and workflows. "
            "Call when you are new to crag Anchor or want to know what's available."
        ),
        "required": [],
        "params": {
            "format": "json (default) | text",
        },
    },
    # ── E graph traversal (migration 027) ─────────────────────────────────
    {
        "name": "graph",
        "category": "graph",
        "description": (
            "Graph v2 traversal. action='siblings': claims sharing canonical entities. "
            "action='neighbors': entity info + typed relations + claims count. "
            "action='impact': 1-hop entity neighborhood + all linked claims."
        ),
        "required": ["action"],
        "params": {
            "action": "siblings|neighbors|impact",
            "claim_kind": "siblings: insight|principle",
            "claim_id": "siblings: integer claim ID",
            "entity": "neighbors/impact: raw entity value",
            "entity_type": "neighbors/impact: port|ip|domain|path|service|file|classname|env_var",
            "limit": "siblings/neighbors: max results (default 20)",
        },
    },
    # ── F disposition engine + governance back-edge ────────────────────────
    {
        "name": "disposition_list",
        "category": "disposition",
        "description": (
            "List pending insights_staging entries by tier (t0 auto / t1 agent-delegable / "
            "t2 human). Call before draining or triaging the staging proposal ledger."
        ),
        "required": [],
        "params": {
            "project": "Scope to a project (optional)",
            "tier": "t0|t1|t2 (optional)",
            "status": "Ledger status filter (default 'pending'; '' = all)",
            "limit": "Max rows (default 100, <=500)",
        },
    },
    {
        "name": "disposition_resolve",
        "category": "disposition",
        "description": (
            "Resolve a staging entry: accept (staging->corpus), reject, merge (into "
            "target_id via supersede), or defer. Capability-gated at t1/t2. "
            "actor (attribution) is mandatory on every call."
        ),
        "required": ["staging_id", "action", "actor"],
        "params": {
            "staging_id": "insights_staging row id",
            "action": "accept|reject|merge|defer",
            "actor": "Who/what decided — REQUIRED",
            "capability": "granted|human_approved (omit for none)",
            "target_id": "merge: existing insight id to merge into",
            "reason": "Why (audit trail)",
        },
    },
    {
        "name": "principles_export",
        "category": "governance",
        "description": (
            "Export principles for the governance back-edge. compile_eligible=true (default) "
            "returns ONLY principles whose linked claims roll up fresh."
        ),
        "required": [],
        "params": {
            "project": "Project scope (optional)",
            "compile_eligible": "Filter to fresh/passing claim_health only (default true)",
        },
    },
    {
        "name": "staging_triage",
        "category": "disposition",
        "description": (
            "Read ONE staging entry + its tier + the matched policy rule — the "
            "'read both sides before deciding' convenience. Call before "
            "disposition_resolve on an ambiguous (t1/t2) entry."
        ),
        "required": ["staging_id"],
        "params": {
            "staging_id": "insights_staging row id",
        },
    },
]

# Fast lookup by name
TOOLS_BY_NAME: dict[str, dict] = {t["name"]: t for t in TOOLS_MANIFEST}

# Categories present
TOOL_CATEGORIES = sorted({t["category"] for t in TOOLS_MANIFEST})

# ---------------------------------------------------------------------------
# Daemon endpoint surface: authoritative list of HTTP endpoints.
# Used for /llms.txt and /guide.
# ---------------------------------------------------------------------------
ENDPOINTS_MANIFEST: list[dict] = [
    # ── Recall ──────────────────────────────────────────────────────────────
    {"method": "POST", "path": "/recall", "summary": "Hybrid semantic+FTS recall"},
    {"method": "POST", "path": "/recall_principle", "summary": "Principle-tier search"},
    {"method": "POST", "path": "/recall_by_entity", "summary": "Entity-scoped recall"},
    {"method": "GET", "path": "/recall_stats", "summary": "Recall usage telemetry"},
    # ── Write ──────────────────────────────────────────────────────────────
    {"method": "POST", "path": "/save_insight", "summary": "Save insight (dedup-guarded)"},
    {"method": "POST", "path": "/suggest_tags", "summary": "Tag suggestion for new insight"},
    {"method": "POST", "path": "/verify_insight", "summary": "Verify/stale an insight"},
    {"method": "POST", "path": "/verify_principle", "summary": "Verify/stale a principle"},
    {"method": "POST", "path": "/update_insight", "summary": "Patch insight in place"},
    {"method": "POST", "path": "/update_principle", "summary": "Patch principle in place"},
    {"method": "POST", "path": "/supersede", "summary": "Supersede one insight by another"},
    {"method": "POST", "path": "/supersede_principle", "summary": "Supersede one principle by another"},
    {"method": "POST", "path": "/promote_insight", "summary": "Promote insight(s) to principle"},
    {"method": "POST", "path": "/token_record", "summary": "Append token ledger row"},
    # ── Query ──────────────────────────────────────────────────────────────
    {"method": "GET", "path": "/query/insights", "summary": "List insights (filterable)"},
    {"method": "GET", "path": "/query/insights/{insight_id}", "summary": "Get one insight by ID"},
    {"method": "GET", "path": "/query/principles", "summary": "List principles"},
    {"method": "GET", "path": "/query/principles/{principle_id}", "summary": "Get one principle by ID"},
    {"method": "POST", "path": "/query/get_batch", "summary": "Fetch insights/principles by IDs"},
    {"method": "GET", "path": "/query/session/{session_id}", "summary": "Get session metadata"},
    {"method": "GET", "path": "/query/entities", "summary": "List all entity types"},
    {"method": "GET", "path": "/query/entity/{entity_type}", "summary": "Entities by type"},
    # ── Grounding v2 ────────────────────────────────────────────────────────
    {"method": "POST", "path": "/ground/enqueue", "summary": "Enqueue a claim for grounding"},
    {"method": "GET", "path": "/ground/candidates", "summary": "Groundable claims (cold_only param)"},
    {"method": "GET", "path": "/ground/audit", "summary": "Drift-flagged claims queue"},
    {"method": "GET", "path": "/ground/check", "summary": "Falsifier for one claim"},
    {"method": "POST", "path": "/ground/clear", "summary": "Resolve a grounding row"},
    {"method": "POST", "path": "/ground/jobs/enqueue", "summary": "Enqueue grounding v2 job"},
    {"method": "GET", "path": "/ground/jobs", "summary": "List grounding jobs"},
    {"method": "GET", "path": "/ground/history/{claim_kind}/{claim_id}", "summary": "Chain-of-thought history"},
    {"method": "GET", "path": "/ground/stats", "summary": "Grounding queue statistics"},
    {"method": "GET", "path": "/ground/proposals", "summary": "Pending resolution proposals (escalations)"},
    {"method": "POST", "path": "/ground/proposals/{proposal_id}/decide", "summary": "Approve/reject a resolution proposal"},
    {"method": "GET", "path": "/ground/resolutions", "summary": "Recent autonomous resolutions (reversible)"},
    {"method": "POST", "path": "/ground/resolutions/{resolution_id}/revert", "summary": "Revert an autonomous resolution"},
    {"method": "GET", "path": "/ground/economics", "summary": "Grounding LLM config, budget status, 7-day spend breakdown"},
    # ── Arena / contradictions ───────────────────────────────────────────────
    {"method": "POST", "path": "/arena", "summary": "Adjudicate contradicting pairs"},
    {"method": "POST", "path": "/clear_suspect", "summary": "Clear contradiction FPs"},
    {"method": "GET", "path": "/audit_contradictions", "summary": "List suspect pairs"},
    # ── Lifecycle (C2) ───────────────────────────────────────────────────────
    {"method": "POST", "path": "/lifecycle/session/add", "summary": "Add a session diary entry"},
    {"method": "GET", "path": "/lifecycle/session/get", "summary": "Get recent sessions for a project"},
    {"method": "GET", "path": "/lifecycle/context/get", "summary": "Get project context"},
    {"method": "POST", "path": "/lifecycle/context/set", "summary": "Upsert project context"},
    {"method": "POST", "path": "/lifecycle/events/add", "summary": "Enqueue a pending event"},
    {"method": "GET", "path": "/lifecycle/events/list", "summary": "List pending events"},
    {"method": "POST", "path": "/lifecycle/events/claim", "summary": "Claim a pending event"},
    {"method": "POST", "path": "/lifecycle/events/complete", "summary": "Complete/fail a pending event"},
    {"method": "POST", "path": "/lifecycle/events/bulk_expire", "summary": "Bulk-expire pending events"},
    {"method": "GET", "path": "/lifecycle/cost_report", "summary": "Token ledger cost report"},
    # ── Ingest (auto-captured session data) ────────────────────────────────
    {"method": "POST", "path": "/ingest/session_tokens", "summary": "Parse transcript and UPSERT token_ledger row"},
    {"method": "POST", "path": "/ingest/session_state", "summary": "UPSERT auto-captured session facts (git branch, commits, wall time)"},
    # ── Brief (C3) ───────────────────────────────────────────────────────────
    {"method": "GET", "path": "/brief", "summary": "Coordinator pre-flight brief (<200 ms)"},
    # ── Ops ──────────────────────────────────────────────────────────────────
    {"method": "GET", "path": "/health", "summary": "Liveness probe"},
    {"method": "GET", "path": "/stats", "summary": "DB statistics"},
    {"method": "GET", "path": "/metrics", "summary": "Prometheus-format metrics"},
    # ── Self-describing (D) ──────────────────────────────────────────────────
    {"method": "GET", "path": "/llms.txt", "summary": "Machine-readable surface (LLM-friendly)"},
    {"method": "GET", "path": "/guide", "summary": "Structured JSON guide to all tools + endpoints"},
]


# ---------------------------------------------------------------------------
# Generators used by daemon endpoints
# ---------------------------------------------------------------------------

def render_llms_txt() -> str:
    """Render the full /llms.txt surface document."""
    lines = [
        "# crag Anchor — verified cross-session memory",
        f"# Version: {MANIFEST_VERSION}",
        f"# MCP tools: {len(TOOLS_MANIFEST)}",
        f"# HTTP endpoints: {len(ENDPOINTS_MANIFEST)}",
        "",
        "## MCP Tools (available via MCP integration)",
        "",
    ]
    for cat in TOOL_CATEGORIES:
        tools_in_cat = [t for t in TOOLS_MANIFEST if t["category"] == cat]
        lines.append(f"### {cat.title()}")
        for t in tools_in_cat:
            lines.append(f"  {t['name']:30s} {t['description'][:100]}")
        lines.append("")

    lines += [
        "## HTTP Endpoints (daemon at 127.0.0.1:8786)",
        "",
    ]
    for ep in ENDPOINTS_MANIFEST:
        lines.append(f"  {ep['method']:6s} {ep['path']:50s} {ep['summary']}")

    lines += [
        "",
        "## Key Workflows",
        "",
        "  Pre-session:    GET /brief?project=X",
        "  Recall:         POST /recall  {query, project, topk}",
        "  Save memory:    POST /save_insight  {content, type, tags, project}",
        "  Ground check:   GET /ground/check?claim_kind=insight&claim_id=N",
        "  Cost report:    GET /lifecycle/cost_report?project=X&days=7",
        "  Events:         GET /lifecycle/events/list?project=X",
        "",
        "## Restart (if daemon unreachable)",
        "  crag-anchor   # runs the daemon in the foreground",
        "",
    ]
    return "\n".join(lines)


def render_guide() -> dict:
    """Render the full /guide structured JSON."""
    by_category: dict[str, list] = {}
    for t in TOOLS_MANIFEST:
        by_category.setdefault(t["category"], []).append({
            "name": t["name"],
            "description": t["description"],
            "required": t["required"],
            "params": t["params"],
        })
    return {
        "version": MANIFEST_VERSION,
        "mcp_tool_count": len(TOOLS_MANIFEST),
        "endpoint_count": len(ENDPOINTS_MANIFEST),
        "tools_by_category": by_category,
        "endpoints": ENDPOINTS_MANIFEST,
        "key_workflows": {
            "pre_session": "GET /brief?project=X (or MCP brief(project='X'))",
            "recall": "POST /recall {query, project, topk}",
            "save_memory": "POST /save_insight {content, type, tags, project}",
            "ground_check": "GET /ground/check?claim_kind=insight&claim_id=N",
            "cost_report": "GET /lifecycle/cost_report?project=X&days=7",
            "events": "GET /lifecycle/events/list?project=X",
        },
        "restart": "crag-anchor  # run the daemon in the foreground",
    }
