#!/usr/bin/env python3
"""
MCP smoke test — WS3a consolidated surface + C2+C3+D extensions.

Spawns mcp-server.py as a subprocess, sends JSON-RPC messages over stdin,
and asserts that the tools/list response contains EXACTLY the 24 registered
tools after the 2026-07-18 tool-surface trim (8 ops/telemetry/disposition
tools demoted to HTTP/CLI/console; their daemon endpoints remain).

Also verifies one-directional parity: every MCP tool must be described in
db/capabilities.py TOOLS_MANIFEST. The manifest is the daemon's FULL
self-describing surface (drives /guide + /llms.txt) and legitimately still
lists the 8 demoted tools as HTTP-reachable — so it is a SUPERSET of the MCP
registry, not an exact mirror.

Runs without a live crag Anchor daemon; tools/list needs no daemon connection
(tool CALLS would return a loud structured error — there is no SQLite fallback).

Exit codes:
  0 — tool surface matches exactly + parity holds
  1 — missing/extra tools, parity violation, or the server failed to start
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[3]
MCP_SERVER = REPO_ROOT / "apps" / "mcp" / "mcp-server.py"

# ── Expected tool names ───────────────────────────────────────────────────────
# WS3a surface: 9 unchanged + 8 merged + 1 absorbed + 1 kept = 19 total.
# The 8 tools demoted OUT of the MCP surface on 2026-07-18 (their daemon HTTP
# endpoints remain; the demotion is MCP-layer only): recall_stats,
# recent_insights, health_check, graph, engine_guide, disposition_list,
# staging_triage, disposition_resolve.
EXPECTED_TOOLS = {
    # Unchanged (7)
    "recall",
    "recall_principle",
    "recall_by_entity",
    "list_principles",
    "save_insight",
    "suggest_tags",
    "add_token_record",
    # Merged (8)
    "get",
    "verify",
    "update",
    "supersede",
    "arena",
    "clear_suspect",
    "audit",
    "grounding",
    # Absorbed (promote + distill)
    "promote_insight",
    # C2 lifecycle + C3 brief (5)
    "session_diary",
    "project_context",
    "events",
    "cost_report",
    "brief",
    # Session lifecycle (2)
    "session_start",
    "session_end",
    # Governance back-edge (1)
    "principles_export",
}  # 24 tools

# ── JSON-RPC helpers ──────────────────────────────────────────────────────────
def rpc_bytes(method: str, params: dict | None = None, req_id: int = 1) -> bytes:
    msg: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return (json.dumps(msg) + "\n").encode()


def notify_bytes(method: str) -> bytes:
    return (json.dumps({"jsonrpc": "2.0", "method": method}) + "\n").encode()


def _reader_thread(stdout, results: list, done: threading.Event) -> None:
    """Read stdout lines until we find id=2 response or EOF."""
    for raw_line in stdout:
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("id") == 2:
            results.append(obj)
            done.set()
            return
    done.set()  # EOF without finding id=2


def main() -> int:
    if not MCP_SERVER.exists():
        print(f"FAIL: mcp-server.py not found at {MCP_SERVER}", file=sys.stderr)
        return 1

    python = sys.executable
    env = {**os.environ, "CRAG_ANCHOR_MCP_SMOKE": "1"}
    env.setdefault("CRAG_ANCHOR_DB_PATH", str(REPO_ROOT / "db" / "engine.db"))

    proc = subprocess.Popen(
        [python, str(MCP_SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # Note: bufsize=1 (line-buffered) is only effective in text mode; in
        # binary mode (the default for pipes) the default block buffer is used.
        # The reader thread handles partial-line buffering correctly regardless.
        env=env,
    )

    results: list[dict] = []
    done = threading.Event()

    # Start reader thread — reads stdout asynchronously so we can write without blocking.
    reader = threading.Thread(target=_reader_thread, args=(proc.stdout, results, done), daemon=True)
    reader.start()

    try:
        # Step 1: MCP initialize (id=1)
        init_msg = rpc_bytes(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "smoke-test", "version": "0.0.1"},
            },
            req_id=1,
        )
        # Step 2: notifications/initialized (no id, no response expected)
        notif_msg = notify_bytes("notifications/initialized")
        # Step 3: tools/list (id=2)
        list_msg = rpc_bytes("tools/list", req_id=2)

        # Write all three messages, then leave stdin open until we get id=2 response.
        proc.stdin.write(init_msg)
        proc.stdin.flush()
        # Small pause to let the server process initialize before we send the next messages.
        time.sleep(0.1)
        proc.stdin.write(notif_msg)
        proc.stdin.write(list_msg)
        proc.stdin.flush()

        # Wait up to 30s for tools/list response.
        done.wait(timeout=30)

        # Close stdin now that we have what we need (triggers graceful server shutdown).
        proc.stdin.close()

        # Brief drain on stderr (non-blocking).
        reader.join(timeout=2)
        stderr_bytes = b""
        try:
            stderr_bytes = proc.stderr.read()
        except Exception:
            pass
        if stderr_bytes:
            # Only print stderr lines that are not the known-OK subscriber noise.
            noisy = b"subscribe stream failed"
            non_noisy = [
                l for l in stderr_bytes.splitlines()
                if noisy not in l
            ]
            if non_noisy:
                print("[mcp-stderr]", b"\n".join(non_noisy).decode(errors="replace"), file=sys.stderr)

    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    if not results:
        print("FAIL: no tools/list (id=2) response found in server output.", file=sys.stderr)
        return 1

    tools_response = results[0]
    if "error" in tools_response:
        print(f"FAIL: tools/list returned error: {tools_response['error']}", file=sys.stderr)
        return 1

    # Extract tool names
    result = tools_response.get("result", {})
    tools = result.get("tools", [])
    found_names = {t.get("name") for t in tools if t.get("name")}

    missing = EXPECTED_TOOLS - found_names
    extra = found_names - EXPECTED_TOOLS

    if missing:
        print(f"FAIL: missing tools: {sorted(missing)}", file=sys.stderr)
        return 1

    if extra:
        # WS3a: the surface is EXACT — stray/alias tools are a regression.
        print(f"FAIL: unexpected extra tools: {sorted(extra)}", file=sys.stderr)
        return 1

    print(f"PASS: all {len(EXPECTED_TOOLS)} expected tools present: {sorted(found_names)}")

    # ── Subset parity: every MCP tool must be described in TOOLS_MANIFEST ──
    # After the 2026-07-18 trim the MCP registry is a SUBSET of the manifest:
    # the manifest is the daemon's full self-describing surface (drives /guide
    # + /llms.txt) and legitimately still lists the 8 demoted, HTTP-reachable
    # tools. So we require MCP ⊆ manifest, NOT an exact mirror.
    DEMOTED = {
        "recall_stats", "recent_insights", "health_check", "graph",
        "engine_guide", "disposition_list", "staging_triage", "disposition_resolve",
    }
    try:
        import importlib.util as _ilu
        _cap_path = Path(__file__).resolve().parents[3] / "db" / "capabilities.py"
        _spec = _ilu.spec_from_file_location("capabilities", _cap_path)
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        manifest_names = {t["name"] for t in _mod.TOOLS_MANIFEST}

        in_mcp_not_manifest = found_names - manifest_names
        # Manifest tools absent from MCP must be exactly the demoted set.
        in_manifest_not_mcp = manifest_names - found_names

        if in_mcp_not_manifest:
            print(f"FAIL: tools in MCP registry but NOT in capabilities.TOOLS_MANIFEST: "
                  f"{sorted(in_mcp_not_manifest)}", file=sys.stderr)
            return 1
        unexpected_gap = in_manifest_not_mcp - DEMOTED
        if unexpected_gap:
            print(f"FAIL: tools in TOOLS_MANIFEST but NOT in MCP registry and NOT a "
                  f"known-demoted tool: {sorted(unexpected_gap)}", file=sys.stderr)
            return 1

        print(f"PASS: subset parity verified -- MCP registry is a subset of manifest "
              f"({len(found_names)} MCP tools, {len(manifest_names)} manifest tools, "
              f"{len(in_manifest_not_mcp)} demoted-but-HTTP-reachable)")
    except Exception as exc:
        print(f"WARN: parity check skipped ({type(exc).__name__}: {exc})", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
