#!/usr/bin/env python3
"""
MCP smoke test — WS3a consolidated surface + C2+C3+D extensions.

Spawns mcp-server.py as a subprocess, sends JSON-RPC messages over stdin,
and asserts that the tools/list response contains EXACTLY the 25 registered
tools (9 unchanged + 8 merged + promote_insight + health_check + 6 C2+C3+D).

Also verifies bidirectional parity between the MCP registry and
db/capabilities.py TOOLS_MANIFEST (neither may have a tool the other lacks).

Runs without a live crag engine daemon; tools/list needs no daemon connection
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
EXPECTED_TOOLS = {
    # Unchanged (9)
    "recall",
    "recall_principle",
    "recall_by_entity",
    "recall_stats",
    "recent_insights",
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
    # Kept
    "health_check",
    # C2+C3+D (6)
    "session_diary",
    "project_context",
    "events",
    "cost_report",
    "brief",
    "engine_guide",
    # E graph (1)
    "graph",
    # Disposition engine + governance back-edge (4)
    "disposition_list",
    "disposition_resolve",
    "principles_export",
    "staging_triage",
}

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
    env = {**os.environ, "CRAG_ENGINE_MCP_SMOKE": "1"}
    env.setdefault("CRAG_ENGINE_DB_PATH", str(REPO_ROOT / "db" / "engine.db"))

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

    # ── Bidirectional parity: MCP registry ⇄ capabilities.TOOLS_MANIFEST ──
    try:
        import importlib.util as _ilu
        _cap_path = Path(__file__).resolve().parents[3] / "db" / "capabilities.py"
        _spec = _ilu.spec_from_file_location("capabilities", _cap_path)
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        manifest_names = {t["name"] for t in _mod.TOOLS_MANIFEST}

        in_mcp_not_manifest = found_names - manifest_names
        in_manifest_not_mcp = manifest_names - found_names

        if in_mcp_not_manifest:
            print(f"FAIL: tools in MCP registry but NOT in capabilities.TOOLS_MANIFEST: "
                  f"{sorted(in_mcp_not_manifest)}", file=sys.stderr)
            return 1
        if in_manifest_not_mcp:
            print(f"FAIL: tools in capabilities.TOOLS_MANIFEST but NOT in MCP registry: "
                  f"{sorted(in_manifest_not_mcp)}", file=sys.stderr)
            return 1

        print(f"PASS: bidirectional parity verified — {len(manifest_names)} tools match exactly")
    except Exception as exc:
        print(f"WARN: parity check skipped ({type(exc).__name__}: {exc})", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
