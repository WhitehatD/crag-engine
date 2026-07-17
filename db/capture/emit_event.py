#!/usr/bin/env python
"""I1 emitter CLI — POST a gate-failure / hook-block / CI-red event to the
crag-engine capture receiver (POST /capture/event).

This is the SENDING side of the closed-loop capture protocol. The receiver
(apps/daemon/engine_daemon.py `/capture/event`) already validates + dedups +
stages these; this wraps the existing db/capture/emit.post_capture_event()
helper so shell hooks and CI YAMLs have ONE tiny, auth-aware, fail-soft
entrypoint instead of hand-rolled curl calls that would each have to re-handle
the X-Capture-Token header, daemon URL resolution, and JSON shaping.

House style (db/capture): NEVER raise, NEVER block the caller. A gate hook that
emits must not fail the gate because the daemon is down — always exit 0 unless
--strict is passed. The failure the caller already hit is the important signal;
losing the capture of it is a soft loss.

Usage (called by the 3 I1 taps):

  # gate failure (quality-gate hook after a MANDATORY gate fails)
  python -m capture.emit_event --source gate_failure \
      --project infra --title "test-policies.sh" --detail "3/42 policies failed" \
      --dedup-key "gate:test-policies:$(git rev-parse --short HEAD)"

  # hook block (policy-engine.sh / sandbox-guard.sh denied an action)
  python -m capture.emit_event --source hook_block \
      --project infra --title "policy-engine BLOCK rule=prod-push" \
      --detail "attempted force-push to main"

  # CI red (a CI job finished failed)
  python -m capture.emit_event --source ci_red \
      --project infra --title "ci.yml / test job" --detail "exit 1 on pytest" \
      --sha "$FORGEJO_SHA" --run-url "$RUN_URL"

Reads the daemon URL + auth token from the SAME db/capture config seam the
receiver uses, so emitter and receiver never disagree on the shared secret.
"""
from __future__ import annotations

import argparse
import hashlib
import sys

# Package-relative when run as `python -m capture.emit_event`; path-injected
# fallback so a hook can also call it as a bare script.
try:
    from capture import emit as _emit
    from capture import config as _config
except Exception:  # pragma: no cover - bare-script fallback
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from capture import emit as _emit
    from capture import config as _config

VALID_SOURCES = {"gate_failure", "hook_block", "ci_red"}


def _daemon_url() -> str:
    try:
        cfg = _config.get_config()
        return getattr(cfg, "daemon_url", None) or "http://127.0.0.1:8786"
    except Exception:
        return "http://127.0.0.1:8786"


def _derive_dedup_key(source: str, title: str, extra: str) -> str:
    blob = f"{source}|{title}|{extra}"
    return f"{source}:" + hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Emit an I1 capture event to the crag-engine receiver.")
    ap.add_argument("--source", required=True, choices=sorted(VALID_SOURCES))
    ap.add_argument("--project", default=None)
    ap.add_argument("--title", required=True, help="short label for the failure")
    ap.add_argument("--detail", default="", help="free-text detail / error tail")
    ap.add_argument("--sha", default="", help="git sha (ci_red / gate_failure)")
    ap.add_argument("--run-url", default="", help="CI run URL (ci_red)")
    ap.add_argument("--dedup-key", default="", help="stable key; derived if omitted")
    ap.add_argument("--daemon-url", default="", help="override daemon URL")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero if the POST fails (default: fail-soft, always 0)")
    args = ap.parse_args(argv)

    payload = {
        "title": args.title,
        "detail": args.detail,
    }
    if args.sha:
        payload["sha"] = args.sha
    if args.run_url:
        payload["run_url"] = args.run_url

    dedup_key = args.dedup_key or _derive_dedup_key(
        args.source, args.title, args.sha or args.detail[:80])

    daemon_url = args.daemon_url or _daemon_url()

    result = _emit.post_capture_event(
        daemon_url=daemon_url,
        source=args.source,
        payload=payload,
        project=args.project,
        dedup_key=dedup_key,
    )

    ok = bool(result.get("ok"))
    if ok:
        staged = result.get("staging_id")
        deduped = result.get("deduped")
        note = "deduped" if deduped else "staged"
        print(f"emit_event: {note} source={args.source} staging_id={staged}")
    else:
        print(f"emit_event: FAILED source={args.source} error={result.get('error')}",
              file=sys.stderr)

    if args.strict and not ok:
        return 1
    return 0  # fail-soft: never break the caller's gate/hook/CI step


if __name__ == "__main__":
    raise SystemExit(main())
