# packages/mcp-spec — OpenMCP descriptor for crag Anchor

## Purpose

`openmcp.json` declares the MCP tools that the crag Anchor MCP server exposes
(`apps/mcp/mcp-server.py`). External MCPs that want to declare crag Anchor
compatibility reference this descriptor.

> **Note:** the descriptor is a *generated artifact*. The live
> `apps/mcp/mcp-server.py` `tools/list` handler is the authoritative surface
> (30 tools); regenerate `openmcp.json` from it whenever the tool surface
> changes (see "Updating" below).

## Tools (30 on the live MCP server)

| Tool | When to use |
|---|---|
| `recall` | Any error; before non-trivial implementation; "do you remember" questions |
| `recall_principle` | BEFORE risky operations touching services / deploys |
| `recall_by_entity` | Before touching a port, service, file, or any tracked entity |
| `recall_stats` | Weekly: hottest insights, dead weight, promotion candidates |
| `recent_insights` | Session end: spot promotion/distillation candidates |
| `list_principles` | Bulk-load a project's principles (keep limit <= 20) |
| `save_insight` | Root cause confirmed; non-obvious gotcha; corrective feedback |
| `suggest_tags` | BEFORE save_insight to avoid tag fragmentation |
| `add_token_record` | Session end: log token cost + recall hit/miss counters |
| `get` | Fetch insights **or** principles by explicit ID (bulk, no search) |
| `verify` | Confirm/contradict an insight **or** principle after acting on it |
| `update` | Patch an insight **or** principle in place (confidence = principle-only) |
| `supersede` | Retire an insight **or** principle in favour of a winner (same kind) |
| `arena` | Adjudicate contradicting ID groups (1..N pairs; dry_run first) |
| `clear_suspect` | Clear FALSE-POSITIVE contradiction flags (1..N pairs) |
| `audit` | Review queues: `contradictions`, `grounding`, or `drift` (pattern required for drift) |
| `grounding` | Grounded-memory triage: `audit` -> `check` -> `clear` |
| `promote_insight` | 1 ID = promote to principle; 2+ IDs + content = distill |
| `health_check` | Structured stack self-check before risky operations |
| `session_diary` | Append/read cross-session diary entries per project |
| `project_context` | Get/set structured project context (branch, status, backlog) |
| `events` | Pending-events queue: list/add/claim/complete/bulk_expire |
| `cost_report` | Token/cost ledger report: totals, per-project breakdown, trend |
| `brief` | One-call coordinator pre-flight: principles + events + grounding + ledger nudge |
| `engine_guide` | Structured JSON guide to all engine tools/endpoints/workflows |
| `graph` | Graph v2 traversal: `siblings` / `neighbors` / `impact` |
| `disposition_list` | List pending staging entries by policy tier (t0/t1/t2) |
| `disposition_resolve` | Accept / reject / merge / defer a staging entry (capability-gated) |
| `staging_triage` | Read one staging entry + its tier + matched policy rule |
| `principles_export` | Export compile-eligible principles for the governance back-edge |

## Updating

When `apps/mcp/mcp-server.py` adds, removes, or changes a tool:

1. Update `openmcp.json` to match (it is generated from the live `tools/list`
   response — spawn the server, run tools/list, dump).
2. Update `apps/mcp/tests/test_mcp_smoke.py` (`EXPECTED_TOOLS` asserts the
   surface EXACTLY — extra or missing tools fail CI).
3. Commit together.
