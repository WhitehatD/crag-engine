# coding: utf-8
"""Autonomic capture pipeline (docs/architecture.md REV 6/8/9/10/11).

The loop's FUEL. Capture runs AROUND the agent, not BY it: an out-of-band
tailer reads the PERSISTED transcript (survives compaction/crash), an
injection-defended extractor mines candidate lessons, and each candidate is
emitted to the daemon's /capture/event endpoint where it lands in
`insights_staging` as UNTRUSTED, QUARANTINED data. The trust boundary is the
PROMOTION gate (the Disposition Engine), NOT capture.

Ports & adapters (REV 11): `ports.py` defines the harness-agnostic
`CaptureAdapter` interface + the normalized `CaptureEvent`/`CaptureSpan`
shapes. `adapters/claude_code_tailer.py` is the highest-fidelity Claude Code
adapter (JSONL tailer). `adapters/gateway.py` is a documented stub for the
universal fallback. `core` here imports NOTHING from a specific harness — the
dependency arrow points inward.
"""
