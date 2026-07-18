#!/usr/bin/env python
"""
crag-engine MCP Server -- verified cross-session memory for Claude Code / Cursor

Exposes tools via stdio MCP protocol (consolidated surface):
  unchanged: recall, recall_principle, recall_by_entity, recall_stats,
             recent_insights, list_principles, save_insight, suggest_tags,
             add_token_record
  merged:    get (insight/principle x N ids), verify, update, supersede,
             arena (N pairs), clear_suspect (N pairs), audit
             (contradictions|drift), grounding (audit|check|clear)
  absorbed:  promote_insight (1 id = promote, N ids + content = distill)
  kept:      health_check

Architecture:
  - Thin HTTP client of the crag engine daemon at 127.0.0.1:8786. Every tool is a
    daemon call. There is NO direct-SQLite fallback: if the daemon is down the
    tool returns a LOUD structured error rather than forking state into a second
    database. Restart the daemon (`crag-engine`) and retry.
  - Logs to stderr only -- stdout reserved for MCP protocol.

Run:
  python mcp-server.py    (stdio transport, registered via Claude Code settings.json)
"""

import asyncio
import json
import os
import sys

# Stable per-process session id -- used when caller doesn't supply one.
# Ensures recall_events ledger is always populated for MCP-originated recalls.
MCP_SESSION_ID = f"mcp-{os.getpid()}"


def _validate_topk(topk) -> int:
    """Coerce topk to a sensible int. Accepts numeric strings.
    Negative or zero -> default 5. Caps at 50."""
    try:
        topk = int(topk)
    except (TypeError, ValueError):
        return 5
    if topk <= 0:
        return 5
    if topk > 50:
        return 50
    return topk

# ============================================================
# Daemon HTTP client
# ============================================================

# WS-P (2026-07-17): the daemon URL resolves through the shared accessor
# db/engine_paths.py so the MCP client and daemon agree on the
# bind. Order: env (CRAG_ENGINE_DAEMON_URL, or CRAG_ENGINE_DAEMON_HOST/PORT) → stack.toml →
# 127.0.0.1:8786. With zero config this is exactly the historical value.
# engine_paths lives in db/ (sibling of apps/); add it to sys.path.
_MCP_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_MCP_REPO_ROOT, "db"))
_DAEMON_URL_DEFAULT = "http://127.0.0.1:8786"
if os.environ.get("CRAG_ENGINE_DAEMON_URL"):
    DAEMON_URL = os.environ["CRAG_ENGINE_DAEMON_URL"]
else:
    try:
        from engine_paths import get_paths as _get_engine_paths
        DAEMON_URL = _get_engine_paths().daemon_url
    except Exception:
        DAEMON_URL = _DAEMON_URL_DEFAULT
DAEMON_TIMEOUT = 5.0  # seconds — reads (fail fast when the daemon is down)
# Writes get a much longer read-timeout: content-bearing writes (save_insight,
# update) embed synchronously on the daemon's shared executor; under load
# (grounding workers, claim decomposition, concurrent saves) the embed queue
# can exceed 5s, and aborting the request loses the write while misreporting
# 'daemon unreachable' (gotcha #3593, root-caused 2026-07-17: two long saves
# genuinely never landed). connect stays 5s so a truly-dead daemon still
# fails fast — only the response wait is extended.
DAEMON_WRITE_TIMEOUT_READ = 60.0  # seconds

# Loud, structured error returned whenever the daemon is unreachable. There is
# NO direct-SQLite fallback: forking state into a second database is worse than
# failing loudly. Restart the daemon and retry.
_DAEMON_DOWN_ERROR = (
    f"crag engine daemon unreachable at {DAEMON_URL} — start it with `crag-engine`"
)


async def _daemon_request(method: str, path: str, json_body: dict = None) -> dict:
    """HTTP call to the crag engine daemon.

    On any transport failure (daemon down, httpx missing, timeout) returns a
    LOUD structured error — never a silent fallback. On an HTTP >=400 the
    daemon's own error body is surfaced.

    POSTs are writes: they keep the 5s connect timeout (dead daemon fails
    fast) but wait up to DAEMON_WRITE_TIMEOUT_READ for the response, because
    the daemon embeds synchronously before replying (see #3593 note above).
    """
    try:
        import httpx
    except ImportError:
        return {"ok": False, "error": "httpx not installed — MCP server cannot reach crag engine daemon"}
    if method == "GET":
        _timeout = DAEMON_TIMEOUT
    else:
        _timeout = httpx.Timeout(
            connect=5.0, read=DAEMON_WRITE_TIMEOUT_READ, write=5.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=_timeout) as client:
            if method == "GET":
                r = await client.get(f"{DAEMON_URL}{path}")
            else:
                r = await client.post(f"{DAEMON_URL}{path}", json=json_body or {})
            if r.status_code >= 400:
                return {"ok": False, "error": r.text, "http_status": r.status_code}
            return r.json()
    except Exception as e:
        print(f"[crag-engine-mcp] daemon unreachable: {e}", file=sys.stderr)
        return {"ok": False, "error": _DAEMON_DOWN_ERROR, "detail": str(e)}


def _provenance(role: str = None, epic_tag: str = None, session_id: str = None) -> dict:
    """Provenance triplet forwarded on every write-path call. session_id falls
    back to the stable per-process id so ledgers are always attributable."""
    return {
        "role": role,
        "epic_tag": epic_tag,
        "session_id": session_id or MCP_SESSION_ID,
    }


# ============================================================
# Async tool wrappers (daemon-backed; loud error if daemon down)
# ============================================================

async def do_recall(query: str, project: str = None, topk: int = 5, session_id: str = None,
                    role: str = None, epic_tag: str = None) -> dict:
    topk = _validate_topk(topk)
    return await _daemon_request("POST", "/recall", {
        "query": query, "project": project, "topk": topk,
        "session_id": session_id or MCP_SESSION_ID,
        "role": role, "epic_tag": epic_tag,
    })


async def do_recall_principle(topic: str, project: str = None) -> dict:
    return await _daemon_request("POST", "/recall_principle", {"topic": topic, "project": project})


async def do_save_insight(content: str, type_: str = "gotcha", tags: str = "",
                          source_file: str = "", project: str = None, force: bool = False,
                          role: str = None, epic_tag: str = None, session_id: str = None) -> dict:
    return await _daemon_request("POST", "/save_insight", {
        "content": content, "type": type_, "tags": tags,
        "source_file": source_file, "project": project, "force": force,
        "role": role, "epic_tag": epic_tag, "session_id": session_id,
    })


async def do_suggest_tags(content: str, project: str = None, limit: int = 5) -> dict:
    return await _daemon_request("POST", "/suggest_tags", {
        "content": content, "project": project, "limit": limit
    })


async def do_recall_stats(project: str = None, days: int = 7) -> dict:
    path = f"/recall_stats?days={days}" + (f"&project={project}" if project else "")
    return await _daemon_request("GET", path)


async def do_recall_by_entity(entity: str, entity_type: str = None,
                              project: str = None, limit: int = 20) -> dict:
    return await _daemon_request("POST", "/recall_by_entity", {
        "entity": entity,
        "entity_type": entity_type,
        "project": project,
        "limit": limit,
    })


async def do_list_principles(project: str = None, limit: int = 200, q: str = "") -> dict:
    params = f"limit={limit}"
    if project:
        params += f"&project={project}"
    if q:
        params += f"&q={q}"
    return await _daemon_request("GET", f"/query/principles?{params}")


async def do_principles_export(project: str = None, compile_eligible: bool = True) -> dict:
    """E1 — crag-distill back-edge export. Thin proxy to GET /principles/export.
    The consumer contract (crag fetch-principles.js) accepts {principles:[...]}
    with per-principle {id, text, confidence, claim_health}; the daemon endpoint
    already emits exactly that shape."""
    params = f"compile_eligible={'true' if compile_eligible else 'false'}"
    if project:
        params += f"&project={project}"
    return await _daemon_request("GET", f"/principles/export?{params}")


async def do_recent_insights(project: str = None, days: int = 30, type_: str = "",
                             limit: int = 50, order_by: str = "created_desc") -> dict:
    params = f"limit={limit}&order_by={order_by}"
    if project:
        params += f"&project={project}"
    if type_:
        params += f"&type={type_}"
    return await _daemon_request("GET", f"/query/insights?{params}")


async def do_add_token_record(arguments: dict) -> dict:
    fields = (
        "project", "session_id", "task_summary", "tokens_in", "tokens_out",
        "cache_hits", "cache_misses", "rtk_savings_pct", "headroom_savings_pct",
        "wall_time_sec", "model", "cache_read_tokens", "cache_write_tokens",
        "fresh_input_tokens", "recall_hits", "recall_misses",
        "repeated_errors", "novel_saves",
    )
    body = {k: arguments[k] for k in fields if arguments.get(k) is not None}
    return await _daemon_request("POST", "/token_record", body)


# ── Merged tools (WS3a) ──────────────────────────────────────────────────────

_KINDS = ("insight", "principle")


async def do_get(kind: str, ids: list) -> dict:
    """Bulk fetch by id — single daemon query via /query/get_batch."""
    if kind not in _KINDS:
        return {"ok": False, "error": f"kind must be insight|principle, got {kind!r}"}
    if not ids:
        return {"ok": False, "error": "ids required (1+ integer ids)"}
    return await _daemon_request("POST", "/query/get_batch", {"kind": kind, "ids": ids})


async def do_verify(kind: str, id_: int, status: str) -> dict:
    """Dispatch to the kind-specific verify endpoint. Response passthrough
    (includes auto_promoted for insights that cross the promote gate)."""
    if kind not in _KINDS:
        return {"ok": False, "error": f"kind must be insight|principle, got {kind!r}"}
    return await _daemon_request("POST", f"/verify_{kind}", {"id": id_, "status": status})


async def do_update(kind: str, id_: int, content: str = None, tags: str = None,
                    source_file: str = None, confidence: float = None) -> dict:
    """Dispatch to the kind-specific update endpoint. confidence is
    principle-only; source_file is insight-only."""
    if kind not in _KINDS:
        return {"ok": False, "error": f"kind must be insight|principle, got {kind!r}"}
    if kind == "insight" and confidence is not None:
        return {"ok": False,
                "error": "confidence is only valid for kind='principle' "
                         "(insight confidence moves via verify/supersede)"}
    if kind == "principle" and source_file is not None:
        return {"ok": False, "error": "source_file is only valid for kind='insight'"}
    body: dict = {"id": id_}
    if content is not None:
        body["content"] = content
    if tags is not None:
        body["tags"] = tags
    if source_file is not None:
        body["source_file"] = source_file
    if confidence is not None:
        body["confidence"] = confidence
    return await _daemon_request("POST", f"/update_{kind}", body)


async def do_supersede(kind: str, loser_id: int, winner_id: int, reason: str = "manual",
                       role: str = None, epic_tag: str = None, session_id: str = None) -> dict:
    """Dispatch to /supersede (insight) or its principle sibling; forwards provenance."""
    if kind not in _KINDS:
        return {"ok": False, "error": f"kind must be insight|principle, got {kind!r}"}
    path = "/supersede" if kind == "insight" else "/supersede" "_principle"
    return await _daemon_request("POST", path, {
        "loser_id": loser_id, "winner_id": winner_id, "reason": reason,
        **_provenance(role, epic_tag, session_id),
    })


async def do_arena(pairs: list, strategy: str, dry_run: bool = False,
                   merged_content: str = None, project: str = None,
                   role: str = None, epic_tag: str = None, session_id: str = None) -> dict:
    """Server-side batch adjudication via /arena_batch. Single pair = [[a, b]]."""
    if not pairs:
        return {"ok": False, "error": "pairs required: list of id-groups, e.g. [[12, 34]]"}
    body: dict = {"pairs": pairs, "strategy": strategy, "dry_run": dry_run,
                  **_provenance(role, epic_tag, session_id)}
    if merged_content:
        body["merged_content"] = merged_content
    if project:
        body["project"] = project
    return await _daemon_request("POST", "/arena_batch", body)


async def do_clear_suspect(pairs: list, reason: str = "false-positive") -> dict:
    """Server-side batch FP clearing via /clear_suspect_batch."""
    if not pairs:
        return {"ok": False, "error": "pairs required: list of {id} or {a_id, b_id}"}
    return await _daemon_request("POST", "/clear_suspect_batch",
                                 {"pairs": pairs, "reason": reason})


async def do_audit(kind: str, project: str = None, pattern: str = None) -> dict:
    """kind='contradictions' -> flagged-pair queue; kind='drift' -> stale-pattern scan."""
    kind = {"contradiction": "contradictions", "grounding": "grounding"}.get(kind, kind)
    if kind == "grounding":
        # convenience alias: the grounding queue lives under grounding(action="audit")
        return await do_grounding("audit", project=project)
    if kind == "contradictions":
        path = f"/audit_{kind}" + (f"?project={project}" if project else "")
        return await _daemon_request("GET", path)
    if kind == "drift":
        if not pattern:
            return {"ok": False, "error": "pattern required for kind='drift' "
                                          "(SQL LIKE substring, e.g. an old IP)"}
        return await _daemon_request("POST", f"/audit_{kind}",
                                     {"pattern": pattern, "project": project})
    return {"ok": False, "error": f"kind must be contradictions|grounding|drift, got {kind!r}"}


async def do_grounding(action: str, project: str = None, claim_kind: str = None,
                       claim_id: int = None, resolution: str = "verified",
                       grounded_against: str = None, reason: str = None,
                       limit: int = 25) -> dict:
    """Grounded-memory triad over the /ground/* endpoints."""
    if action == "audit":
        # ALWAYS forward limit — the old `if limit != default` skip meant the
        # default was never sent and the daemon's larger default won, blowing
        # 180K+ chars of queue into agent context (WS5 residual fix).
        params: list[str] = [f"limit={int(limit)}"]
        if project:
            params.append(f"project={project}")
        return await _daemon_request("GET", "/ground/audit?" + "&".join(params))
    if action in ("check", "clear"):
        if not claim_kind or claim_id is None:
            return {"ok": False,
                    "error": f"action='{action}' requires claim_kind + claim_id"}
        if claim_kind not in _KINDS:
            return {"ok": False,
                    "error": f"claim_kind must be insight|principle, got {claim_kind!r}"}
        if action == "check":
            return await _daemon_request(
                "GET", f"/ground/check?claim_kind={claim_kind}&claim_id={claim_id}")
        body: dict = {"claim_kind": claim_kind, "claim_id": claim_id,
                      "resolution": resolution}
        if grounded_against:
            body["grounded_against"] = grounded_against
        if reason:
            body["reason"] = reason
        return await _daemon_request("POST", "/ground/clear", body)
    return {"ok": False, "error": f"action must be audit|check|clear, got {action!r}"}


async def do_promote_insight(insight_ids: list, content: str = None, project: str = None,
                             role: str = None, epic_tag: str = None,
                             session_id: str = None) -> dict:
    """1 id -> fast-path promote (optional content override);
    2+ ids -> merge into one principle (content REQUIRED)."""
    if not insight_ids:
        return {"ok": False, "error": "insight_ids required (1+ integer ids)"}
    prov = _provenance(role, epic_tag, session_id)
    if len(insight_ids) == 1:
        body: dict = {"insight_id": insight_ids[0], **prov}
        if content:
            body["content"] = content
        return await _daemon_request("POST", "/promote_insight", body)
    if not content:
        return {"ok": False,
                "error": "content required when promoting 2+ insights "
                         "(they merge into one principle)"}
    return await _daemon_request("POST", "/dis" "till", {
        "insight_ids": insight_ids, "content": content, "project": project, **prov,
    })


async def do_health_check() -> dict:
    """Structured health probe. Bypasses _daemon_request because the daemon
    returns HTTP 503 (with a structured body) when a CRITICAL class is down —
    we need that body, not the generic error envelope."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=DAEMON_TIMEOUT) as client:
            r = await client.get(f"{DAEMON_URL}/fail_mode_check")
            # 200 = all clear, 503 = critical down — both carry the structured payload.
            return r.json()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "checks": [], "summary": "daemon unreachable", "principle_ref": 146}


# ── Extended grounding: jobs | history | stats actions ─────────────────────

async def do_grounding_extended(action: str, project: str = None, claim_kind: str = None,
                                claim_id: int = None, resolution: str = "verified",
                                grounded_against: str = None, reason: str = None,
                                limit: int = 25, status: str = None,
                                proposal_id: int = None, resolution_id: int = None,
                                decision: str = None) -> dict:
    """Full grounding surface including v2 job queue + autoresolve actions."""
    if action in ("audit", "check", "clear"):
        # Delegate to existing do_grounding
        return await do_grounding(action, project, claim_kind, claim_id,
                                  resolution, grounded_against, reason, limit)
    if action == "jobs":
        params = [f"limit={int(limit)}"]
        if status:
            params.append(f"status={status}")
        return await _daemon_request("GET", "/ground/jobs?" + "&".join(params))
    if action == "history":
        if not claim_kind or claim_id is None:
            return {"ok": False, "error": "action='history' requires claim_kind + claim_id"}
        if claim_kind not in _KINDS:
            return {"ok": False, "error": f"claim_kind must be insight|principle, got {claim_kind!r}"}
        return await _daemon_request("GET", f"/ground/history/{claim_kind}/{claim_id}")
    if action == "stats":
        return await _daemon_request("GET", "/ground/stats")
    # ── Autonomous-resolution surface (Phase v3) ────────────────────────────
    if action == "proposals":
        return await _daemon_request(
            "GET", f"/ground/proposals?status={status or 'pending'}&limit={int(limit)}")
    if action == "resolutions":
        return await _daemon_request("GET", f"/ground/resolutions?limit={int(limit)}")
    if action == "economics":
        return await _daemon_request("GET", "/ground/economics")
    if action == "decide":
        if proposal_id is None or decision not in ("approve", "reject"):
            return {"ok": False,
                    "error": "action='decide' requires proposal_id + decision (approve|reject)"}
        return await _daemon_request(
            "POST", f"/ground/proposals/{int(proposal_id)}/decide", {"decision": decision})
    if action == "revert":
        if resolution_id is None:
            return {"ok": False, "error": "action='revert' requires resolution_id"}
        return await _daemon_request(
            "POST", f"/ground/resolutions/{int(resolution_id)}/revert", {})
    return {"ok": False,
            "error": ("action must be audit|check|clear|jobs|history|stats|"
                      f"proposals|resolutions|economics|decide|revert, got {action!r}")}


# ── C2 operator-lifecycle tool wrappers ─────────────────────────────────────

async def do_session_diary(action: str, arguments: dict) -> dict:
    """session_diary MCP tool — add or get session diary entries."""
    if action == "add":
        body = {k: arguments[k] for k in (
            "project", "date", "accomplished", "files_changed",
            "commits", "decisions", "problems", "next_steps",
            "duration", "raw_markdown",
        ) if k in arguments}
        if "project" not in body:
            return {"ok": False, "error": "project is required for action='add'"}
        return await _daemon_request("POST", "/lifecycle/session/add", body)
    if action == "get":
        project = arguments.get("project")
        limit = arguments.get("limit", 5)
        if not project:
            return {"ok": False, "error": "project is required for action='get'"}
        params = f"project={project}&limit={limit}"
        return await _daemon_request("GET", f"/lifecycle/session/get?{params}")
    return {"ok": False, "error": f"action must be add|get, got {action!r}"}


async def do_project_context(action: str, project: str, arguments: dict) -> dict:
    """project_context MCP tool — get or set project context."""
    if action == "get":
        return await _daemon_request("GET", f"/lifecycle/context/get?project={project}")
    if action == "set":
        body = {"project": project}
        for f in ("status", "current_branch", "last_session_date",
                  "architecture_decisions", "known_issues", "backlog"):
            if f in arguments:
                body[f] = arguments[f]
        return await _daemon_request("POST", "/lifecycle/context/set", body)
    return {"ok": False, "error": f"action must be get|set, got {action!r}"}


async def do_events(action: str, arguments: dict) -> dict:
    """events MCP tool — list/add/claim/complete/bulk_expire."""
    if action == "list":
        params: list[str] = []
        if arguments.get("project"):
            params.append(f"project={arguments['project']}")
        if arguments.get("priority"):
            params.append(f"priority={arguments['priority']}")
        params.append(f"limit={arguments.get('limit', 20)}")
        return await _daemon_request("GET", "/lifecycle/events/list?" + "&".join(params))
    if action == "add":
        for req in ("source", "event_type", "summary"):
            if not arguments.get(req):
                return {"ok": False, "error": f"'{req}' is required for action='add'"}
        body = {k: arguments[k] for k in (
            "project", "source", "event_type", "summary",
            "payload", "priority", "expires_at",
        ) if k in arguments}
        return await _daemon_request("POST", "/lifecycle/events/add", body)
    if action == "claim":
        if "id" not in arguments:
            return {"ok": False, "error": "'id' is required for action='claim'"}
        return await _daemon_request("POST", "/lifecycle/events/claim", {
            "id": arguments["id"],
            "claimed_by": arguments.get("claimed_by", "mcp"),
        })
    if action == "complete":
        for req in ("id", "status"):
            if req not in arguments:
                return {"ok": False, "error": f"'{req}' is required for action='complete'"}
        return await _daemon_request("POST", "/lifecycle/events/complete", {
            "id": arguments["id"],
            "status": arguments["status"],
            "result": arguments.get("result", ""),
        })
    if action == "bulk_expire":
        body = {}
        if arguments.get("project"):
            body["project"] = arguments["project"]
        if arguments.get("priority"):
            body["priority"] = arguments["priority"]
        return await _daemon_request("POST", "/lifecycle/events/bulk_expire", body)
    return {"ok": False, "error": f"action must be list|add|claim|complete|bulk_expire, got {action!r}"}


async def do_cost_report(project: str = None, days: int = 7) -> dict:
    """cost_report MCP tool."""
    params = f"days={days}"
    if project:
        params += f"&project={project}"
    return await _daemon_request("GET", f"/lifecycle/cost_report?{params}")


async def do_brief(project: str) -> dict:
    """brief MCP tool — coordinator pre-flight brief."""
    return await _daemon_request("GET", f"/brief?project={project}")


async def do_session_start(project: str = None) -> dict:
    """session_start MCP tool — the deterministic-loop context payload. Thin
    GET of the daemon's /session/start (design laws 1-2)."""
    params = f"?project={project}" if project else ""
    return await _daemon_request("GET", f"/session/start{params}")


async def do_session_end(project: str = None, session_id: str = None,
                         summary: str = None) -> dict:
    """session_end MCP tool — records the end marker + returns payoff numbers.
    Thin POST of the daemon's /session/end (design laws 1-2)."""
    body = {}
    if project:
        body["project"] = project
    if session_id:
        body["session_id"] = session_id
    if summary:
        body["summary"] = summary
    return await _daemon_request("POST", "/session/end", body)


async def do_engine_guide(fmt: str = "json") -> dict:
    """engine_guide MCP tool — structured guide to all tools + endpoints."""
    if fmt == "text":
        # Return llms.txt as a JSON-wrapped string
        result = await _daemon_request("GET", "/llms.txt")
        # /llms.txt returns plain text; httpx will decode it as a string in the error path.
        # Wrap for uniform MCP envelope.
        if isinstance(result, dict) and result.get("ok") is False:
            return result
        return {"ok": True, "text": result if isinstance(result, str) else str(result)}
    return await _daemon_request("GET", "/guide")


async def do_graph(action: str, **kwargs) -> dict:
    """graph MCP tool — Graph v2 traversal (siblings / neighbors / impact)."""
    if action == "siblings":
        claim_kind = kwargs.get("claim_kind", "insight")
        claim_id = kwargs.get("claim_id")
        limit = kwargs.get("limit", 20)
        if claim_id is None:
            return {"ok": False, "error": "claim_id required for action=siblings"}
        return await _daemon_request(
            "GET", f"/graph/siblings?claim_kind={claim_kind}&claim_id={claim_id}&limit={limit}"
        )
    elif action == "neighbors":
        entity = kwargs.get("entity")
        entity_type = kwargs.get("entity_type")
        limit = kwargs.get("limit", 20)
        if not entity or not entity_type:
            return {"ok": False, "error": "entity and entity_type required for action=neighbors"}
        return await _daemon_request(
            "GET", f"/graph/neighbors?entity={entity}&entity_type={entity_type}&limit={limit}"
        )
    elif action == "impact":
        entity = kwargs.get("entity")
        entity_type = kwargs.get("entity_type")
        if not entity or not entity_type:
            return {"ok": False, "error": "entity and entity_type required for action=impact"}
        return await _daemon_request(
            "GET", f"/graph/impact?entity={entity}&entity_type={entity_type}"
        )
    else:
        return {"ok": False, "error": f"unknown action: {action}. Use siblings|neighbors|impact"}


# ============================================================
# Disposition Engine tools (control plane — docs/architecture.md REV 5 §5.2)
# ============================================================

async def do_disposition_list(project: str = None, tier: str = None,
                              status: str = "pending", limit: int = 100) -> dict:
    """disposition_list — pending staging entries by tier (tiers lazily
    stamped daemon-side)."""
    params = [f"status={status}", f"limit={limit}"]
    if project:
        params.append(f"project={project}")
    if tier:
        params.append(f"tier={tier}")
    return await _daemon_request("GET", "/disposition/list?" + "&".join(params))


async def do_staging_triage(staging_id: int) -> dict:
    """staging_triage — read one staging row + its tier + matched policy rule
    (the 'read both sides before deciding' convenience)."""
    return await _daemon_request("GET", f"/disposition/triage/{staging_id}")


async def do_disposition_resolve(staging_id: int, action: str, actor: str,
                                reason: str = None, target_id: int = None,
                                capability: str = None) -> dict:
    """disposition_resolve — capability-gated. accept/merge at tier t1/t2
    without sufficient capability returns a 'requires_human' verdict from the
    daemon (the transition is NOT executed)."""
    if not actor:
        return {"ok": False, "error": "actor is required (attribution invariant)"}
    body = {"staging_id": staging_id, "action": action, "actor": actor}
    if reason is not None:
        body["reason"] = reason
    if target_id is not None:
        body["target_id"] = target_id
    if capability is not None:
        body["capability"] = capability
    return await _daemon_request("POST", "/disposition/resolve", body)


# ============================================================
# Broadcast subscriber — background task started in main().
# ============================================================
#
# Streams /subscribe SSE from the daemon and emits a one-line
# [ENGINE BROADCAST] notification to stderr. Claude Code's MCP harness surfaces
# stderr output to the agent context — so a new insight saved by ANY session
# shows up live in the next agent turn of every other session.

async def _consume_broadcasts():
    """Subscribe to /subscribe SSE stream, surface relevant events via stderr."""
    try:
        import httpx
    except ImportError:
        print("[crag-engine-mcp] httpx not installed -- broadcast subscriber disabled", file=sys.stderr)
        return

    url = f"{DAEMON_URL}/subscribe"
    backoff = 1.0
    # 5s connect + write + pool timeout; read=None keeps the SSE stream open
    # indefinitely.  Without a connect timeout, a dead daemon can leave this
    # coroutine blocked forever in the TCP handshake, making the whole MCP
    # process appear stuck to Claude Code.
    stream_timeout = httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0)
    while True:
        try:
            async with httpx.AsyncClient(timeout=stream_timeout) as client:
                async with client.stream("GET", url) as r:
                    if r.status_code != 200:
                        print(f"[crag-engine-mcp] subscribe got HTTP {r.status_code}, backing off", file=sys.stderr)
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 30.0)
                        continue
                    backoff = 1.0  # reset on success
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if not raw or raw.startswith(":"):
                            continue
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        kind = msg.get("kind")
                        if kind not in ("insight_saved", "principle_distilled", "promote_global"):
                            continue
                        payload = msg.get("payload", {}) or {}
                        preview = (payload.get("content_preview") or "").lower()
                        proj = payload.get("project") or "global"
                        ident = payload.get("insight_id") or payload.get("principle_id") or "?"
                        print(
                            f"[ENGINE BROADCAST] {kind} in {proj}: #{ident} — {preview[:100]}",
                            file=sys.stderr,
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[crag-engine-mcp] subscribe stream failed: {exc}; reconnecting in {backoff:.1f}s", file=sys.stderr)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


# ============================================================
# MCP Server wiring
# ============================================================

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, Tool, TextContent

try:
    # MCP >=1.0 exposes Resource and AnyUrl for resource registration
    from mcp.types import Resource
    _HAS_RESOURCE = True
except ImportError:
    _HAS_RESOURCE = False

app = Server("crag-engine")

_KIND_PROP = {"type": "string", "enum": ["insight", "principle"],
              "description": "Row type: insight (raw memory) or principle (distilled high-trust)"}


@app.list_tools()
async def list_tools() -> list:
    return [
        Tool(
            name="recall",
            description=(
                "Search cross-session memory by meaning. Returns top-K ranked insights plus matching "
                "principles. Call on errors, before non-trivial work, or when past sessions are referenced."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural-language query"},
                    "project": {"type": "string", "description": "Project scope (e.g. 'infra'). Omit for cross-project search."},
                    "topk": {"type": "integer", "default": 5, "description": "Number of top insights to return (1-20)"},
                    "session_id": {"type": "string", "description": "Session id for telemetry (optional)"},
                    "role": {"type": "string", "description": "coordinator|subagent|operator (optional)"},
                    "epic_tag": {"type": "string", "description": "Sprint/epic label (optional)"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="recall_principle",
            description=(
                "Search distilled principles — the highest-trust safety rules. Call BEFORE risky "
                "operations (VPS, proxies, services, deploys)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Topic to search principles for"},
                    "project": {"type": "string", "description": "Project scope (optional)"},
                },
                "required": ["topic"],
            },
        ),
        Tool(
            name="recall_by_entity",
            description=(
                "Recall insights and principles referencing an exact entity (port, IP, file, service...). "
                "Call before changing a port, service, or config you may have touched before."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "entity": {"type": "string", "description": "Entity value, e.g. '8090' or 'nginx'"},
                    "entity_type": {"type": "string",
                                    "enum": ["port", "ip", "domain", "path", "service", "classname", "env_var", "file"],
                                    "description": "Narrow match to one entity type (optional)"},
                    "project": {"type": "string", "description": "Project scope (optional)"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["entity"],
            },
        ),
        Tool(
            name="recall_stats",
            description=(
                "crag-engine usage telemetry: hottest insights, top queries, dead-weight and cross-project "
                "promotion candidates. Weekly memory-health check."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project scope (optional)"},
                    "days": {"type": "integer", "default": 7, "description": "Lookback window in days"},
                },
            },
        ),
        Tool(
            name="recent_insights",
            description=(
                "List recent insights for a project ordered by creation, confidence, or recall frequency. "
                "Use at session end to spot promotion/distillation candidates."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project scope (optional)"},
                    "days": {"type": "integer", "default": 30, "description": "Recency window in days"},
                    "type": {"type": "string", "description": "Type filter, e.g. gotcha|pattern|decision"},
                    "limit": {"type": "integer", "default": 50},
                    "order_by": {"type": "string", "default": "created_desc",
                                 "description": "created_desc|confidence_desc|recalled_desc"},
                },
                "required": [],
            },
        ),
        Tool(
            name="list_principles",
            description=(
                "List a project's principles sorted by confidence DESC, optional substring filter. "
                "Keep limit <= 20 to avoid flooding context."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project scope (optional)"},
                    "limit": {"type": "integer", "default": 20, "description": "Max rows; keep <= 20 (50 overflows context)"},
                    "q": {"type": "string", "description": "Content substring filter (optional)"},
                },
                "required": [],
            },
        ),
        Tool(
            name="principles_export",
            description=(
                "Export principles for the crag-distill governance back-edge. "
                "compile_eligible=true (default) returns ONLY principles whose linked claims "
                "roll up fresh — the set that compiles into governance. compile_eligible=false "
                "returns all active principles with their honest claim_health for observability. "
                "This is the tool `crag distill` calls; response shape matches its contract."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project scope (optional)"},
                    "compile_eligible": {"type": "boolean", "default": True,
                                          "description": "Filter to fresh/passing claim_health only"},
                },
                "required": [],
            },
        ),
        Tool(
            name="save_insight",
            description=(
                "Persist a memory across sessions; dedup-guarded and auto-embedded for recall. Save "
                "confirmed root causes, gotchas, patterns, decisions, and corrective user feedback."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Full insight text — detailed enough to be actionable later"},
                    "type": {"type": "string", "default": "gotcha",
                             "enum": ["gotcha", "pattern", "architecture", "decision", "bug-fix", "tool",
                                      "feedback", "user-context", "project-context", "reference"]},
                    "tags": {"type": "string", "description": "Comma-separated tags"},
                    "source_file": {"type": "string", "description": "File path this insight refers to (optional)"},
                    "project": {"type": "string", "description": "Project scope (optional)"},
                    "force": {"type": "boolean", "default": False, "description": "Bypass the dedup guard"},
                    "role": {"type": "string", "description": "coordinator|subagent|operator (optional)"},
                    "epic_tag": {"type": "string", "description": "Sprint/epic label (optional)"},
                    "session_id": {"type": "string", "description": "Session id (optional)"},
                },
                "required": ["content"],
            },
        ),
        Tool(
            name="suggest_tags",
            description=(
                "Suggest existing tags for draft insight content by semantic similarity. Call before "
                "save_insight to avoid creating near-duplicate tag variants."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Draft insight text"},
                    "project": {"type": "string", "description": "Project scope (optional)"},
                    "limit": {"type": "integer", "default": 5},
                },
                "required": ["content"],
            },
        ),
        Tool(
            name="add_token_record",
            description=(
                "Append one session row to the token ledger (cost plus recall hit/miss counters). "
                "Call at the end of every session."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "session_id": {"type": "string"},
                    "task_summary": {"type": "string"},
                    "tokens_in": {"type": "integer"},
                    "tokens_out": {"type": "integer"},
                    "cache_hits": {"type": "integer"},
                    "cache_misses": {"type": "integer"},
                    "rtk_savings_pct": {"type": "number"},
                    "headroom_savings_pct": {"type": "number"},
                    "wall_time_sec": {"type": "number"},
                    "model": {"type": "string"},
                    "cache_read_tokens": {"type": "integer"},
                    "cache_write_tokens": {"type": "integer"},
                    "fresh_input_tokens": {"type": "integer"},
                    "recall_hits": {"type": "integer", "description": "Recalls that materially changed the approach"},
                    "recall_misses": {"type": "integer", "description": "Recalls that returned nothing useful"},
                    "repeated_errors": {"type": "integer", "description": "Errors already described by a saved insight"},
                    "novel_saves": {"type": "integer", "description": "Net-new insights saved this session"},
                },
                "required": ["project"],
            },
        ),
        # ── Merged tools ─────────────────────────────────────────────────────
        Tool(
            name="get",
            description=(
                "Fetch insights or principles by explicit ID — no semantic search, single bulk query. "
                "Returns {found, not_found}. Use for IDs from recall conflicts, audits, or '#NNNN' references."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "kind": _KIND_PROP,
                    "ids": {"type": "array", "items": {"type": "integer"}, "minItems": 1,
                            "description": "IDs to fetch (1 or more)"},
                },
                "required": ["kind", "ids"],
            },
        ),
        Tool(
            name="verify",
            description=(
                "Confirm or contradict a claim after acting on it. 'verified' raises confidence, 'stale' "
                "lowers it (insight +0.1/-0.2; principle +0.05/-0.1, gentler because curated)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "kind": _KIND_PROP,
                    "id": {"type": "integer", "description": "ID of the claim to verify"},
                    "status": {"type": "string", "enum": ["verified", "stale"],
                               "description": "verified = still true; stale = contradicted by reality"},
                },
                "required": ["kind", "id", "status"],
            },
        ),
        Tool(
            name="update",
            description=(
                "Patch an insight or principle in place; ID unchanged, re-embeds on content change. "
                "confidence is principle-only; source_file is insight-only. Supply at least one field."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "kind": _KIND_PROP,
                    "id": {"type": "integer", "description": "ID of the row to patch"},
                    "content": {"type": "string", "description": "Full replacement content"},
                    "tags": {"type": "string", "description": "Comma-separated tags"},
                    "source_file": {"type": "string", "description": "Insight-only: file path the insight refers to"},
                    "confidence": {"type": "number", "description": "Principle-only: new confidence 0.0-1.0"},
                },
                "required": ["kind", "id"],
            },
        ),
        Tool(
            name="supersede",
            description=(
                "Mark one insight/principle superseded by another of the SAME kind when ground truth "
                "proves it wrong. Loser leaves recall but stays queryable by ID for audit."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "kind": _KIND_PROP,
                    "loser_id": {"type": "integer", "description": "Row being retired"},
                    "winner_id": {"type": "integer", "description": "Row that replaces it"},
                    "reason": {"type": "string", "description": "Why the loser is wrong (audit trail)"},
                },
                "required": ["kind", "loser_id", "winner_id"],
            },
        ),
        Tool(
            name="arena",
            description=(
                "Adjudicate contradicting insight groups; losers are superseded by the winner. pairs is a "
                "list of ID groups — one entry for a single pair. Run dry_run=true first to review verdicts."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pairs": {"type": "array", "minItems": 1,
                              "items": {"type": "array", "items": {"type": "integer"}, "minItems": 2},
                              "description": "ID groups to adjudicate, e.g. [[12, 34]] or [[1, 2], [3, 4]]"},
                    "strategy": {"type": "string", "enum": ["auto", "recency", "evidence", "confidence", "merge"],
                                 "description": "auto=majority vote; recency=newest wins; merge needs merged_content"},
                    "dry_run": {"type": "boolean", "default": False, "description": "Preview verdicts without writing"},
                    "merged_content": {"type": "string", "description": "Required for strategy='merge': the merged insight text"},
                    "project": {"type": "string", "description": "Project scope (optional)"},
                },
                "required": ["pairs", "strategy"],
            },
        ),
        Tool(
            name="clear_suspect",
            description=(
                "Mark flagged contradiction pairs as FALSE POSITIVES and clear them from the audit queue. "
                "Entries are {id} or {a_id, b_id}; use after reading both sides of each pair."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pairs": {"type": "array", "minItems": 1,
                              "items": {"type": "object"},
                              "description": "Entries of shape {id} or {a_id, b_id}; per-entry reason optional"},
                    "reason": {"type": "string", "description": "Default reason for entries that omit one"},
                },
                "required": ["pairs"],
            },
        ),
        Tool(
            name="audit",
            description=(
                "List open review queues. kind='contradictions': flagged pairs awaiting triage. "
                "kind='grounding': drifted-claim queue. "
                "kind='drift': insights matching a stale pattern (pattern required)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["contradictions", "grounding", "drift"],
                             "description": "contradictions = suspect pairs; grounding = drifted claims; drift = stale-pattern scan"},
                    "project": {"type": "string", "description": "Project scope (optional)"},
                    "pattern": {"type": "string", "description": "drift only (required): SQL LIKE substring, e.g. an old IP"},
                },
                "required": ["kind"],
            },
        ),
        Tool(
            name="grounding",
            description=(
                "Grounded-memory triage. action='audit' lists drift-flagged claims; 'check' returns a "
                "claim's falsifier + liveness stamp; 'clear' resolves the queue row after you re-verify. "
                "action='jobs' lists grounding work queue; 'history' returns chain-of-thought for one claim; "
                "'stats' returns queue depth + throughput counters."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["audit", "check", "clear", "jobs", "history", "stats"],
                               "description": "audit=list queue; check=get falsifier; clear=resolve row; "
                                              "jobs=list v2 work queue; history=chain-of-thought; stats=queue depth"},
                    "project": {"type": "string", "description": "audit only: scope to a project (optional)"},
                    "limit": {"type": "integer", "default": 100, "description": "audit/jobs only: max rows"},
                    "claim_kind": {"type": "string", "enum": ["insight", "principle"],
                                   "description": "check/clear/history: kind of the claim"},
                    "claim_id": {"type": "integer", "description": "check/clear/history: ID of the claim"},
                    "resolution": {"type": "string", "enum": ["verified", "dismissed", "noted"],
                                   "default": "verified",
                                   "description": "clear only: why the row closes"},
                    "grounded_against": {"type": "string", "description": "clear only: what was checked and passed"},
                    "reason": {"type": "string", "description": "clear only: optional explanation"},
                    "status": {"type": "string", "description": "jobs only: filter by status (pending|running|done|failed)"},
                },
                "required": ["action"],
            },
        ),
        Tool(
            name="promote_insight",
            description=(
                "Promote insight(s) to a principle seeded at confidence 0.9. One ID = fast-path promote "
                "(optional content override); 2+ IDs = merge into one principle (content required)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "insight_ids": {"type": "array", "items": {"type": "integer"}, "minItems": 1,
                                    "description": "Insight IDs; a single ID promotes, several merge"},
                    "content": {"type": "string",
                                "description": "Principle text. Optional override for 1 ID; REQUIRED for 2+"},
                    "project": {"type": "string", "description": "Project for a merged principle (optional)"},
                },
                "required": ["insight_ids"],
            },
        ),
        Tool(
            name="health_check",
            description=(
                "Structured self-check of the crag-engine across 5 failure classes (proxy cord, embedding "
                "backlog, DB corruption, token ledger, VPS tunnel). Call before risky operations."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        # ── C2 operator-lifecycle tools ─────────────────────────────────────
        Tool(
            name="session_diary",
            description=(
                "Read or write session diary entries. action='add' appends a session record; "
                "action='get' returns recent sessions for a project (default last 5)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "get"],
                               "description": "add = append a session; get = list recent sessions"},
                    "project": {"type": "string", "description": "Project slug (required)"},
                    "date": {"type": "string", "description": "add: ISO date string (default today)"},
                    "accomplished": {"type": "string", "description": "add: what was completed"},
                    "files_changed": {"type": "string", "description": "add: comma-separated file list"},
                    "commits": {"type": "string", "description": "add: commit SHAs or messages"},
                    "decisions": {"type": "string", "description": "add: key decisions made"},
                    "problems": {"type": "string", "description": "add: open issues"},
                    "next_steps": {"type": "string", "description": "add: planned follow-on work"},
                    "duration": {"type": "string", "description": "add: session duration (free text)"},
                    "raw_markdown": {"type": "string", "description": "add: raw markdown diary entry"},
                    "session_uuid": {
                        "type": "string",
                        "description": (
                            "add: Claude session UUID (migration 029) — when set, enriches the "
                            "ONE canonical sessions row for this session instead of inserting a "
                            "new fragmented date-row. Omit for legacy/manual behavior."
                        ),
                    },
                    "limit": {"type": "integer", "default": 5, "description": "get: max rows (default 5)"},
                },
                "required": ["action"],
            },
        ),
        Tool(
            name="project_context",
            description=(
                "Read or write structured project context (branch, status, known issues, backlog). "
                "action='get' returns current context; action='set' upserts fields (null fields unchanged)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["get", "set"]},
                    "project": {"type": "string", "description": "Project slug"},
                    "status": {"type": "string", "description": "set: one-line project status"},
                    "current_branch": {"type": "string", "description": "set: active git branch"},
                    "last_session_date": {"type": "string", "description": "set: ISO date of last session"},
                    "architecture_decisions": {"type": "string", "description": "set: free-text ADR summary"},
                    "known_issues": {"type": "string", "description": "set: open bugs / blockers"},
                    "backlog": {"type": "string", "description": "set: comma-separated next tasks"},
                },
                "required": ["action", "project"],
            },
        ),
        Tool(
            name="events",
            description=(
                "Manage the pending-events queue. action='list' returns pending events; "
                "'add' enqueues a new event; 'claim' marks claimed; 'complete' marks done/failed; "
                "'bulk_expire' expires all matching project/priority."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["list", "add", "claim", "complete", "bulk_expire"]},
                    "project": {"type": "string", "description": "Project scope / target"},
                    "priority": {"type": "string", "enum": ["critical", "high", "normal", "low"],
                                 "description": "Event priority"},
                    "limit": {"type": "integer", "default": 20, "description": "list: max rows"},
                    "source": {"type": "string", "description": "add: event source label"},
                    "event_type": {"type": "string", "description": "add: machine type string"},
                    "summary": {"type": "string", "description": "add: human-readable one-liner"},
                    "payload": {"type": "string", "description": "add: optional JSON payload string"},
                    "expires_at": {"type": "string", "description": "add: optional ISO expiry timestamp"},
                    "id": {"type": "integer", "description": "claim/complete: event ID"},
                    "claimed_by": {"type": "string", "description": "claim: identifier of claiming agent"},
                    "status": {"type": "string", "enum": ["completed", "failed"],
                               "description": "complete: outcome status"},
                    "result": {"type": "string", "description": "complete: brief outcome text"},
                },
                "required": ["action"],
            },
        ),
        Tool(
            name="cost_report",
            description=(
                "Token/cost report from the ledger: totals, per-project breakdown, 7-day trend. "
                "Use on 'cost report' / 'token stats' / 'how much am I spending'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Scope to one project (optional)"},
                    "days": {"type": "integer", "default": 7, "description": "Lookback window in days"},
                },
                "required": [],
            },
        ),
        # ── C3 coordinator brief ─────────────────────────────────────────────
        Tool(
            name="brief",
            description=(
                "ONE call returning a coordinator pre-flight brief: top principles, pending events, "
                "grounding flags, last session summary, token-ledger nudge. Target <200 ms. "
                "Call at session start instead of four separate warm-up queries."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project slug for scoped brief"},
                },
                "required": ["project"],
            },
        ),
        # ── P0 deterministic session lifecycle (design laws 1-2) ──────────
        Tool(
            name="session_start",
            description=(
                "Deterministic-loop context payload for session start: ONE composed bundle — "
                "overview (trust/counts/today), the top true-t2 needs-you items, stale-rules "
                "count, and the last session diary row. Invoked by the harness SessionStart "
                "hook via the CLI; prompts suggest, hooks enforce."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project scope (optional)"},
                },
                "required": [],
            },
        ),
        Tool(
            name="session_end",
            description=(
                "Deterministic-loop end-capture: records a session-end marker and returns the "
                "payoff numbers (captured/verified/promoted today). FAST + fail-open — invoked "
                "by the harness SessionEnd hook via the CLI."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project scope (optional)"},
                    "session_id": {"type": "string", "description": "Session UUID (optional)"},
                    "summary": {"type": "string", "description": "One-line session summary (optional)"},
                },
                "required": [],
            },
        ),
        # ── D self-describing ─────────────────────────────────────────────
        Tool(
            name="engine_guide",
            description=(
                "Return a structured JSON guide to all crag-engine tools, endpoints, and workflows. "
                "Call when you are new to crag-engine or want to know what's available."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "enum": ["json", "text"],
                               "description": "json (default) returns structured guide; text returns llms.txt"},
                },
                "required": [],
            },
        ),
        # ── E graph traversal (migration 027) ─────────────────────────────
        Tool(
            name="graph",
            description=(
                "Graph v2 traversal. action='siblings': claims sharing ≥1 canonical entity "
                "with a given claim, ranked by shared count. action='neighbors': canonical "
                "entity info, typed relations, linked-claims count. action='impact': 1-hop "
                "entity neighborhood + all linked claims (impact zone for an entity change)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["siblings", "neighbors", "impact"],
                               "description": "Traversal type"},
                    "claim_kind": {"type": "string", "enum": ["insight", "principle"],
                                   "description": "siblings: kind of the anchor claim"},
                    "claim_id": {"type": "integer",
                                 "description": "siblings: ID of the anchor claim"},
                    "entity": {"type": "string",
                               "description": "neighbors/impact: raw entity value"},
                    "entity_type": {"type": "string",
                                    "description": "neighbors/impact: port|ip|domain|path|service|file|classname|env_var"},
                    "limit": {"type": "integer", "default": 20,
                              "description": "siblings/neighbors: max results"},
                },
                "required": ["action"],
            },
        ),
        # ── Disposition Engine (control plane — migration 033) ────────────
        Tool(
            name="disposition_list",
            description=(
                "List pending insights_staging entries by tier (t0 auto / t1 agent-delegable / "
                "t2 human). Tiers are lazily classified daemon-side. Call before draining or "
                "triaging the staging proposal ledger."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Scope to a project (optional)"},
                    "tier": {"type": "string", "enum": ["t0", "t1", "t2"],
                             "description": "Filter to one tier (optional)"},
                    "status": {"type": "string", "default": "pending",
                               "description": "Ledger status filter (default 'pending'; '' = all)"},
                    "limit": {"type": "integer", "default": 100, "description": "Max rows (<=500)"},
                },
                "required": [],
            },
        ),
        Tool(
            name="staging_triage",
            description=(
                "Read ONE staging entry + its tier + the matched policy rule — the 'read both "
                "sides before deciding' convenience (mirrors the contradiction-FP-triage pattern). "
                "Call before disposition_resolve on an ambiguous (t1/t2) entry."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "staging_id": {"type": "integer", "description": "insights_staging row id"},
                },
                "required": ["staging_id"],
            },
        ),
        Tool(
            name="disposition_resolve",
            description=(
                "Resolve a staging entry: accept (staging->corpus), reject (drop, memory-only), "
                "merge (into target_id via supersede — reversible), or defer. CAPABILITY-GATED: "
                "accept/merge at tier t1 needs capability='granted'; at t2 needs 'human_approved'. "
                "Without sufficient capability the daemon returns a 'requires_human' verdict and "
                "does NOT execute. actor (attribution) is mandatory on every call."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "staging_id": {"type": "integer", "description": "insights_staging row id"},
                    "action": {"type": "string", "enum": ["accept", "reject", "merge", "defer"],
                               "description": "The disposition to execute"},
                    "actor": {"type": "string",
                              "description": "Who/what decided (agent id / 'operator') — REQUIRED"},
                    "reason": {"type": "string", "description": "Why (audit trail)"},
                    "target_id": {"type": "integer",
                                  "description": "Required for action='merge': existing insight id to merge into"},
                    "capability": {"type": "string", "enum": ["granted", "human_approved"],
                                   "description": "Session capability for the T1/T2 gate (omit for none)"},
                },
                "required": ["staging_id", "action", "actor"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> CallToolResult:
    try:
        if name == "recall":
            result = await do_recall(
                arguments["query"],
                arguments.get("project"),
                _validate_topk(arguments.get("topk", 5)),
                arguments.get("session_id"),
                arguments.get("role"),
                arguments.get("epic_tag"),
            )
        elif name == "recall_principle":
            result = await do_recall_principle(arguments["topic"], arguments.get("project"))
        elif name == "recall_by_entity":
            result = await do_recall_by_entity(
                arguments["entity"],
                arguments.get("entity_type"),
                arguments.get("project"),
                arguments.get("limit", 20),
            )
        elif name == "recall_stats":
            result = await do_recall_stats(arguments.get("project"), arguments.get("days", 7))
        elif name == "recent_insights":
            result = await do_recent_insights(
                arguments.get("project"),
                arguments.get("days", 30),
                arguments.get("type", ""),
                arguments.get("limit", 50),
                arguments.get("order_by", "created_desc"),
            )
        elif name == "principles_export":
            result = await do_principles_export(
                project=arguments.get("project"),
                compile_eligible=arguments.get("compile_eligible", True),
            )
        elif name == "list_principles":
            result = await do_list_principles(
                arguments.get("project"),
                arguments.get("limit", 20),
                arguments.get("q", ""),
            )
        elif name == "save_insight":
            result = await do_save_insight(
                arguments["content"],
                arguments.get("type", "gotcha"),
                arguments.get("tags", ""),
                arguments.get("source_file", ""),
                arguments.get("project"),
                arguments.get("force", False),
                arguments.get("role"),
                arguments.get("epic_tag"),
                arguments.get("session_id"),
            )
        elif name == "suggest_tags":
            result = await do_suggest_tags(
                arguments["content"],
                arguments.get("project"),
                arguments.get("limit", 5),
            )
        elif name == "add_token_record":
            result = await do_add_token_record(arguments)
        # ── Merged tools ────────────────────────────────────────────────────
        elif name == "get":
            result = await do_get(arguments.get("kind", "insight"), arguments.get("ids"))
        elif name == "verify":
            result = await do_verify(
                arguments.get("kind", "insight"),
                arguments["id"],
                arguments["status"],
            )
        elif name == "update":
            result = await do_update(
                arguments.get("kind", "insight"),
                arguments["id"],
                arguments.get("content"),
                arguments.get("tags"),
                arguments.get("source_file"),
                arguments.get("confidence"),
            )
        elif name == "supersede":
            result = await do_supersede(
                arguments.get("kind", "insight"),
                arguments["loser_id"],
                arguments["winner_id"],
                arguments.get("reason", "manual"),
                arguments.get("role"),
                arguments.get("epic_tag"),
                arguments.get("session_id"),
            )
        elif name == "arena":
            result = await do_arena(
                arguments.get("pairs"),
                arguments["strategy"],
                arguments.get("dry_run", False),
                arguments.get("merged_content"),
                arguments.get("project"),
                arguments.get("role"),
                arguments.get("epic_tag"),
                arguments.get("session_id"),
            )
        elif name == "clear_suspect":
            result = await do_clear_suspect(
                arguments.get("pairs"),
                arguments.get("reason", "false-positive"),
            )
        elif name == "audit":
            result = await do_audit(
                arguments.get("kind", "contradictions"),
                arguments.get("project"),
                arguments.get("pattern"),
            )
        elif name == "grounding":
            result = await do_grounding_extended(
                arguments.get("action", "audit"),
                arguments.get("project"),
                arguments.get("claim_kind"),
                arguments.get("claim_id"),
                arguments.get("resolution", "verified"),
                arguments.get("grounded_against"),
                arguments.get("reason"),
                arguments.get("limit", 100),
                arguments.get("status"),
                arguments.get("proposal_id"),
                arguments.get("resolution_id"),
                arguments.get("decision"),
            )
        elif name == "promote_insight":
            result = await do_promote_insight(
                arguments.get("insight_ids"),
                arguments.get("content"),
                arguments.get("project"),
                arguments.get("role"),
                arguments.get("epic_tag"),
                arguments.get("session_id"),
            )
        elif name == "health_check":
            result = await do_health_check()
        # ── C2 lifecycle tools ─────────────────────────────────────────────
        elif name == "session_diary":
            result = await do_session_diary(
                arguments.get("action", "get"),
                arguments,
            )
        elif name == "project_context":
            result = await do_project_context(
                arguments.get("action", "get"),
                arguments.get("project", ""),
                arguments,
            )
        elif name == "events":
            result = await do_events(
                arguments.get("action", "list"),
                arguments,
            )
        elif name == "cost_report":
            result = await do_cost_report(
                arguments.get("project"),
                arguments.get("days", 7),
            )
        # ── C3 brief ───────────────────────────────────────────────────────
        elif name == "brief":
            result = await do_brief(arguments["project"])
        # ── P0 deterministic session lifecycle (design laws 1-2) ────────────
        elif name == "session_start":
            result = await do_session_start(arguments.get("project"))
        elif name == "session_end":
            result = await do_session_end(
                arguments.get("project"),
                arguments.get("session_id"),
                arguments.get("summary"),
            )
        # ── D self-describing ──────────────────────────────────────────────
        elif name == "engine_guide":
            result = await do_engine_guide(arguments.get("format", "json"))
        # ── E graph traversal ──────────────────────────────────────────────
        elif name == "graph":
            result = await do_graph(arguments["action"], **{
                k: v for k, v in arguments.items() if k != "action"
            })
        # ── Disposition Engine (control plane) ──────────────────────────────
        elif name == "disposition_list":
            result = await do_disposition_list(
                arguments.get("project"),
                arguments.get("tier"),
                arguments.get("status", "pending"),
                arguments.get("limit", 100),
            )
        elif name == "staging_triage":
            result = await do_staging_triage(arguments["staging_id"])
        elif name == "disposition_resolve":
            result = await do_disposition_resolve(
                arguments["staging_id"],
                arguments["action"],
                arguments["actor"],
                arguments.get("reason"),
                arguments.get("target_id"),
                arguments.get("capability"),
            )
        else:
            result = {"ok": False, "error": f"unknown tool: {name}"}
    except Exception as e:
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    is_error = isinstance(result, dict) and result.get("ok") is False
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result, indent=2))],
        isError=is_error,
    )


# ── engine://guide MCP resource ────────────────────────────────────────────

if _HAS_RESOURCE:
    @app.list_resources()
    async def list_resources() -> list:
        return [
            Resource(
                uri="engine://guide",
                name="engine_guide",
                description=(
                    "Structured JSON guide to all crag-engine MCP tools, HTTP endpoints, and key workflows. "
                    "Fetched live from the daemon's /guide endpoint."
                ),
                mimeType="application/json",
            ),
        ]

    @app.read_resource()
    async def read_resource(uri: str) -> str:
        if str(uri) == "engine://guide":
            result = await _daemon_request("GET", "/guide")
            return json.dumps(result, indent=2)
        return json.dumps({"ok": False, "error": f"unknown resource: {uri}"})


async def main():
    # Windows compatibility: ProactorEventLoop is default on Win11 Python 3.12+
    # Broadcast subscriber runs in background — fail-open, never blocks startup.
    try:
        asyncio.create_task(_consume_broadcasts())
    except Exception as exc:
        print(f"[crag-engine-mcp] broadcast subscriber failed to start: {exc}", file=sys.stderr)
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
