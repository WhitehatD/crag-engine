#!/usr/bin/env python3
"""Measure MCP tool-definition payload (descriptions + inputSchema chars).

Usage: python measure_payload.py <path-to-mcp-server.py>
Spawns the server, runs tools/list over JSON-RPC, prints per-surface totals.
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path


def rpc(method, params=None, req_id=1):
    m = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        m["params"] = params
    return (json.dumps(m) + "\n").encode()


def main(server_path: str) -> int:
    env = {**os.environ}
    proc = subprocess.Popen(
        [sys.executable, server_path],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env,
    )
    results, done = [], threading.Event()

    def reader():
        for raw in proc.stdout:
            line = raw.strip()
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
        done.set()

    threading.Thread(target=reader, daemon=True).start()
    try:
        proc.stdin.write(rpc("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "measure", "version": "0"}}, 1))
        proc.stdin.flush()
        time.sleep(0.1)
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode())
        proc.stdin.write(rpc("tools/list", req_id=2))
        proc.stdin.flush()
        done.wait(timeout=30)
        proc.stdin.close()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    if not results:
        print("FAIL: no tools/list response")
        return 1
    tools = results[0]["result"]["tools"]
    desc_chars = sum(len(t.get("description") or "") for t in tools)
    schema_chars = sum(len(json.dumps(t.get("inputSchema") or {}, separators=(",", ":"))) for t in tools)
    total = desc_chars + schema_chars
    full_json = len(json.dumps(tools, separators=(",", ":")))
    print(f"server: {Path(server_path).name}")
    print(f"tools: {len(tools)}")
    print(f"description chars: {desc_chars}")
    print(f"inputSchema chars: {schema_chars}")
    print(f"definition payload (desc+schema): {total}")
    print(f"full tools/list JSON: {full_json}")
    over = [t["name"] for t in tools if len(t.get("description") or "") > 200]
    print(f"descriptions over 200 chars: {over or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
