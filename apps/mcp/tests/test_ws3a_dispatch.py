#!/usr/bin/env python3
"""WS3a MCP dispatch tests — merged-tool routing and validation.

Loads mcp-server.py as a module (no daemon needed), monkeypatches
_daemon_request to capture (method, path, body), and asserts:
  - verify(kind=principle) dispatches to /verify_principle
  - update(kind=insight, confidence=...) returns a validation error (no HTTP)
  - promote_insight with 3 ids and no content returns an error (no HTTP)
  - grounding(action='clear') without claim_id returns an error (no HTTP)
  - arena single-pair call forwards provenance to /arena_batch
  - get / audit / supersede / clear_suspect dispatch to the right endpoints

Exit codes: 0 = all pass, 1 = any failure.

Run:
  python apps/mcp/tests/test_ws3a_dispatch.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MCP_PY = REPO_ROOT / "apps" / "mcp" / "mcp-server.py"

FAILURES: list[str] = []
PASSES: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASSES.append(name)
        print(f"  [v] {name}")
    else:
        FAILURES.append(f"{name}: {detail}")
        print(f"  [X] {name}  -- {detail}")


spec = importlib.util.spec_from_file_location("engine_mcp_ws3atest", str(MCP_PY))
mcp = importlib.util.module_from_spec(spec)
sys.modules["engine_mcp_ws3atest"] = mcp
spec.loader.exec_module(mcp)

CALLS: list[tuple] = []


async def fake_daemon_request(method: str, path: str, json_body: dict = None) -> dict:
    CALLS.append((method, path, json_body))
    return {"ok": True, "echo": {"method": method, "path": path, "body": json_body}}


mcp._daemon_request = fake_daemon_request


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


asyncio.set_event_loop(asyncio.new_event_loop())

# ---------------------------------------------------------------------------
print("\n== verify dispatch ==")
CALLS.clear()
r = run(mcp.do_verify("principle", 42, "stale"))
check("verify(kind=principle) -> /verify_principle",
      CALLS and CALLS[0][1] == "/verify_principle" and CALLS[0][2] == {"id": 42, "status": "stale"},
      str(CALLS))
CALLS.clear()
r = run(mcp.do_verify("insight", 7, "verified"))
check("verify(kind=insight) -> /verify_insight",
      CALLS and CALLS[0][1] == "/verify_insight", str(CALLS))
CALLS.clear()
r = run(mcp.do_verify("bogus", 7, "verified"))
check("verify(kind=bogus) -> error, no HTTP",
      r.get("ok") is False and not CALLS, f"{r} calls={CALLS}")

# ---------------------------------------------------------------------------
print("\n== update dispatch + validation ==")
CALLS.clear()
r = run(mcp.do_update("insight", 5, confidence=0.7))
check("update(kind=insight, confidence) -> error, no HTTP",
      r.get("ok") is False and "principle" in r.get("error", "") and not CALLS,
      f"{r} calls={CALLS}")
CALLS.clear()
r = run(mcp.do_update("principle", 5, source_file="x.py"))
check("update(kind=principle, source_file) -> error, no HTTP",
      r.get("ok") is False and not CALLS, f"{r} calls={CALLS}")
CALLS.clear()
r = run(mcp.do_update("principle", 5, content="new", confidence=0.8))
check("update(kind=principle) -> /update_principle with confidence",
      CALLS and CALLS[0][1] == "/update_principle"
      and CALLS[0][2] == {"id": 5, "content": "new", "confidence": 0.8}, str(CALLS))
CALLS.clear()
r = run(mcp.do_update("insight", 5, tags="a,b", source_file="y.py"))
check("update(kind=insight) -> /update_insight",
      CALLS and CALLS[0][1] == "/update_insight"
      and CALLS[0][2] == {"id": 5, "tags": "a,b", "source_file": "y.py"}, str(CALLS))

# ---------------------------------------------------------------------------
print("\n== promote_insight absorption ==")
CALLS.clear()
r = run(mcp.do_promote_insight([1, 2, 3]))
check("promote_insight 3 ids, no content -> error, no HTTP",
      r.get("ok") is False and "content" in r.get("error", "") and not CALLS,
      f"{r} calls={CALLS}")
CALLS.clear()
r = run(mcp.do_promote_insight([9]))
check("promote_insight 1 id -> /promote_insight",
      CALLS and CALLS[0][1] == "/promote_insight" and CALLS[0][2]["insight_id"] == 9,
      str(CALLS))
CALLS.clear()
r = run(mcp.do_promote_insight([9], content="override text"))
check("promote_insight 1 id + content -> content override",
      CALLS and CALLS[0][1] == "/promote_insight" and CALLS[0][2]["content"] == "override text",
      str(CALLS))
CALLS.clear()
r = run(mcp.do_promote_insight([1, 2], content="merged principle text"))
check("promote_insight 2 ids + content -> /distill",
      CALLS and CALLS[0][1] == "/distill"
      and CALLS[0][2]["insight_ids"] == [1, 2]
      and CALLS[0][2]["content"] == "merged principle text", str(CALLS))
CALLS.clear()
r = run(mcp.do_promote_insight([]))
check("promote_insight empty ids -> error, no HTTP",
      r.get("ok") is False and not CALLS, f"{r} calls={CALLS}")

# ---------------------------------------------------------------------------
print("\n== grounding validation + dispatch ==")
CALLS.clear()
r = run(mcp.do_grounding("clear", claim_kind="insight"))
check("grounding(clear) without claim_id -> error, no HTTP",
      r.get("ok") is False and "claim_id" in r.get("error", "") and not CALLS,
      f"{r} calls={CALLS}")
CALLS.clear()
r = run(mcp.do_grounding("audit", project="infra"))
check("grounding(audit) -> GET /ground/audit with limit ALWAYS forwarded (WS5 fix)",
      CALLS and CALLS[0][0] == "GET" and CALLS[0][1] == "/ground/audit?limit=25&project=infra",
      str(CALLS))
CALLS.clear()
r = run(mcp.do_grounding("check", claim_kind="principle", claim_id=12))
check("grounding(check) -> GET /ground/check",
      CALLS and CALLS[0][1] == "/ground/check?claim_kind=principle&claim_id=12", str(CALLS))
CALLS.clear()
r = run(mcp.do_grounding("clear", claim_kind="insight", claim_id=12,
                         resolution="dismissed", reason="structural claim"))
check("grounding(clear) -> POST /ground/clear with resolution",
      CALLS and CALLS[0][0] == "POST" and CALLS[0][1] == "/ground/clear"
      and CALLS[0][2]["resolution"] == "dismissed", str(CALLS))

# ---------------------------------------------------------------------------
print("\n== arena provenance forwarding ==")
CALLS.clear()
r = run(mcp.do_arena([[11, 22]], "recency", dry_run=True,
                     role="coordinator", epic_tag="ws3a", session_id="sess-1"))
body = CALLS[0][2] if CALLS else {}
check("arena single pair -> /arena_batch",
      CALLS and CALLS[0][1] == "/arena_batch" and body.get("pairs") == [[11, 22]], str(CALLS))
check("arena provenance forwarded",
      body.get("role") == "coordinator" and body.get("epic_tag") == "ws3a"
      and body.get("session_id") == "sess-1", str(body))
CALLS.clear()
r = run(mcp.do_arena([[1, 2]], "recency"))
check("arena default session_id falls back to MCP_SESSION_ID",
      CALLS and CALLS[0][2].get("session_id") == mcp.MCP_SESSION_ID, str(CALLS))
CALLS.clear()
r = run(mcp.do_arena([], "recency"))
check("arena empty pairs -> error, no HTTP",
      r.get("ok") is False and not CALLS, f"{r} calls={CALLS}")

# ---------------------------------------------------------------------------
print("\n== get / audit / supersede / clear_suspect dispatch ==")
CALLS.clear()
r = run(mcp.do_get("principle", [3, 4]))
check("get(kind=principle) -> /query/get_batch",
      CALLS and CALLS[0][1] == "/query/get_batch"
      and CALLS[0][2] == {"kind": "principle", "ids": [3, 4]}, str(CALLS))
CALLS.clear()
r = run(mcp.do_get("insight", []))
check("get empty ids -> error, no HTTP", r.get("ok") is False and not CALLS, f"{r}")

CALLS.clear()
r = run(mcp.do_audit("drift"))
check("audit(drift) without pattern -> error, no HTTP",
      r.get("ok") is False and "pattern" in r.get("error", "") and not CALLS, f"{r}")
CALLS.clear()
r = run(mcp.do_audit("drift", project="infra", pattern="198.51.100."))
check("audit(drift) -> POST /audit_drift",
      CALLS and CALLS[0][0] == "POST" and CALLS[0][1] == "/audit_drift"
      and CALLS[0][2]["pattern"] == "198.51.100.", str(CALLS))
CALLS.clear()
r = run(mcp.do_audit("contradictions", project="infra"))
check("audit(contradictions) -> GET /audit_contradictions?project=",
      CALLS and CALLS[0][0] == "GET" and CALLS[0][1] == "/audit_contradictions?project=infra",
      str(CALLS))

CALLS.clear()
r = run(mcp.do_supersede("principle", 1, 2, reason="drifted"))
check("supersede(kind=principle) -> /supersede_principle",
      CALLS and CALLS[0][1] == "/supersede_principle" and CALLS[0][2]["reason"] == "drifted",
      str(CALLS))
CALLS.clear()
r = run(mcp.do_supersede("insight", 1, 2))
check("supersede(kind=insight) -> /supersede",
      CALLS and CALLS[0][1] == "/supersede", str(CALLS))

CALLS.clear()
r = run(mcp.do_clear_suspect([{"a_id": 1, "b_id": 2}], reason="fp"))
check("clear_suspect -> /clear_suspect_batch",
      CALLS and CALLS[0][1] == "/clear_suspect_batch"
      and CALLS[0][2] == {"pairs": [{"a_id": 1, "b_id": 2}], "reason": "fp"}, str(CALLS))

# ---------------------------------------------------------------------------
print(f"\n{len(PASSES)} passed, {len(FAILURES)} failed")
if FAILURES:
    for f in FAILURES:
        print(f"FAIL: {f}")
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
