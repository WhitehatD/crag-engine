#!/usr/bin/env python3
# coding: utf-8
"""Autonomic capture runner (docs/architecture.md REV 6/8/9/11).

Ties tailer -> extractor -> emit into one standalone worker. As of the D8
hardening pass, `run_once()` IS wired as an always-on daemon lifespan task:
apps/daemon/engine_daemon.py imports this module and calls run_once() every
[capture].daemon_task_interval_sec seconds (default 120) inside a thread
(never blocking the event loop), gated on [capture].daemon_task_enabled
(default true) and fail-soft per cycle (a broken cycle logs and continues,
never crashing the daemon). This script REMAINS directly runnable for operator
one-shot/preview runs, for hosts that run the daemon with the task disabled and
prefer an external scheduler, and for a PreCompact/SessionEnd hook nudge with
--force-close-tail. Watermarks + rate limits (capture-state.db) make the
daemon-task and any manual run coexist safely — a span is processed at most once.

Usage:
  python db/capture/run_capture.py --once
  ... --once --dry-run                 # extract + print, no emit, no state writes
  ... --loop --interval-sec 300        # continuous poll loop (standalone alternative)
  ... --once --force-close-tail        # also capture the final in-flight span

Each pass:
  1. tailer.poll()                      -> new COMPLETE CaptureSpans
  2. skip spans already marked processed (capture_state.span_already_processed)
  3. extractor.extract_candidates()     -> sanitized ExtractedCandidates (fail-soft)
  4. emit.emit_candidates()             -> POST /capture/event (quarantined staging)
  5. mark the span processed (skipped entirely in --dry-run: dry runs must be
     side-effect-free so an operator can preview repeatedly)

Processed-marking note: a span is marked processed once extraction is
ATTEMPTED (succeeded or found 0 candidates) without raising. A raised
exception mid-extraction is caught by extractor.py itself (never propagates
here) and returns []  — so in practice this runner always marks a span
processed after one pass. "The transcript is retained for re-extraction"
(REV 6) means an operator can always re-run extraction over historical
spans by clearing rows from the capture_emitted table in the watermark
store (capture-state.db) — the raw JSONL is never touched or deleted by
this pipeline.

Scheduling (default is the in-daemon lifespan task above; the following are
OPTIONAL alternatives for hosts that disable it via daemon_task_enabled=false):
  Windows Task Scheduler:
    schtasks /create /tn "CragEngineCapture" /tr "<python> <this file> --once" /sc minute /mo 5
  Linux/mac cron: */5 * * * * <python> <this file> --once
  Claude Code hook wiring:
    PreCompact / SessionEnd -> this script --once --force-close-tail (latency nudge only;
    correctness never depends on the hook firing — see claude_code_tailer.py docstring).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger("crag-engine-capture")

_THIS_DIR = Path(__file__).resolve().parent            # db/capture/
_DB_DIR = _THIS_DIR.parent                                # db/
for p in (str(_THIS_DIR), str(_DB_DIR), str(_THIS_DIR / "adapters")):
    if p not in sys.path:
        sys.path.insert(0, p)

import config as capture_config  # noqa: E402
import state as capture_state  # noqa: E402
import extractor  # noqa: E402
import emit as capture_emit  # noqa: E402
import engine_paths  # noqa: E402
from claude_code_tailer import ClaudeCodeTailer  # noqa: E402


_INTERACTIVE_PROXY_PORTS = (":8788", ":8787")


def _get_extract_llm():
    """Best-effort role client for the 'extract' role, routing-isolation
    enforced (doctrine: background roles NEVER route through the interactive
    session's :8788 model-router / :8787 Headroom proxy).

    claim_layer.get_role_client already refuses a proxy base_url and, for
    anthropic-oauth, builds a DIRECT (api.anthropic.com) client with no
    explicit base_url. BUT the Anthropic SDK silently honors ANTHROPIC_BASE_URL
    from the environment — which claudex sets to the :8788 interactive proxy —
    so a naively-constructed client leaks background extraction traffic onto
    the operator's live lane. We neutralize any interactive-proxy env base_url
    ONLY for the duration of client construction (the SDK pins base_url at
    __init__), so the extract role gets a genuinely direct lane. This lives
    here, in the capture worker, precisely so claim_layer core is imported,
    never edited. Returns (llm_client_or_None, model_name)."""
    try:
        import claim_layer
        model = claim_layer.get_role_model("extract")

        saved: dict = {}
        for var in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE"):
            val = os.environ.get(var)
            if val and any(port in val for port in _INTERACTIVE_PROXY_PORTS):
                saved[var] = val
                del os.environ[var]
        try:
            llm = claim_layer.get_role_client("extract")
        finally:
            os.environ.update(saved)
        return llm, model
    except Exception as exc:
        logger.warning("run_capture: could not build extract role client (fail-soft, no-op extraction): %s", exc)
        return None, None


def run_once(*, dry_run: bool = False, force_close_tail: bool = False,
            max_spans: int = None) -> dict:
    cfg = capture_config.get_config()
    tailer = ClaudeCodeTailer(transcript_glob=cfg.transcript_glob, watermark_db=cfg.watermark_store)
    llm, model = _get_extract_llm()
    engine_db_path = str(engine_paths.get_paths().db_path)

    spans = tailer.poll(max_spans=max_spans or cfg.max_spans_per_poll,
                        force_close_tail=force_close_tail, persist_watermark=not dry_run)

    report = {"spans_seen": len(spans), "spans_skipped_dup": 0, "spans_processed": 0,
              "candidates": {"correction": 0, "discovered-practice": 0,
                             "anti-pattern": 0, "craft-meta": 0},
              "emit_summary": {"emitted": 0, "rate_limited": 0, "known_lesson": 0, "failed": 0},
              "sessions": set(), "details": []}

    for span in spans:
        if not dry_run and capture_state.span_already_processed(cfg.watermark_store, span.span_id):
            report["spans_skipped_dup"] += 1
            continue

        candidates = extractor.extract_candidates(span, llm=llm, model=model)
        for c in candidates:
            report["candidates"][c.category] = report["candidates"].get(c.category, 0) + 1

        result = capture_emit.emit_candidates(
            candidates, session=span.session, project=span.project,
            daemon_url=cfg.daemon_url, watermark_db=cfg.watermark_store,
            rate_budget=cfg.max_candidates_per_session_run, engine_db_path=engine_db_path,
            dedup_similarity=cfg.dedup_similarity, dry_run=dry_run,
        )
        for k in ("emitted", "rate_limited", "known_lesson", "failed"):
            report["emit_summary"][k] += result.get(k, 0)
        report["sessions"].add(span.session)
        report["spans_processed"] += 1
        report["details"].append({
            "span_id": span.span_id, "session": span.session,
            "n_events": len(span.events), "n_candidates": len(candidates),
            "candidates": result.get("results", []),
        })

        if not dry_run:
            capture_state.mark_span_processed(cfg.watermark_store, span.span_id, span.session)

    report["sessions"] = sorted(report["sessions"])
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Autonomic capture runner")
    parser.add_argument("--once", action="store_true", help="Run a single pass and exit (default if no mode given)")
    parser.add_argument("--loop", action="store_true", help="Run continuously (NOT scheduled by this change)")
    parser.add_argument("--interval-sec", type=float, default=300.0, help="Sleep between passes in --loop mode")
    parser.add_argument("--dry-run", action="store_true", help="Extract + print, no emit, no state writes")
    parser.add_argument("--force-close-tail", action="store_true",
                        help="Also capture the final in-flight span (PreCompact/SessionEnd nudge)")
    parser.add_argument("--max-spans", type=int, default=None, help="Override [capture].max_spans_per_poll")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.loop:
        print(f"[capture] loop mode, interval={args.interval_sec}s (Ctrl+C to stop)")
        try:
            while True:
                report = run_once(dry_run=args.dry_run, force_close_tail=args.force_close_tail,
                                  max_spans=args.max_spans)
                print(json.dumps(report, indent=2))
                time.sleep(args.interval_sec)
        except KeyboardInterrupt:
            print("[capture] loop stopped")
        return 0

    # Default (and --once): single pass.
    report = run_once(dry_run=args.dry_run, force_close_tail=args.force_close_tail,
                      max_spans=args.max_spans)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
