# coding: utf-8
"""Grounding v3 — claim verify executors + worker.

One executor per predicate class. Each returns a verdict dict
{verdict: pass|fail|uncertain, reasoning, evidence}. P5 is NEVER queued or
verified (terminal/axiomatic) — the groundskeeper only SURFACES it past
review_after; there is deliberately no P5 executor here.

Lanes (grounding_jobs.lane):
    'local'  — P2/P3: free, no LLM, no subprocess-network.
    'shell'  — P1: sandboxed read-only shell (existing _is_read_only guard).
    'llm'    — P4: evidence bundle -> role 'verdict' client (background lane,
               never :8788/:8787).

House style: pure functions take an open sqlite3.Connection. Timestamps via
lifecycle._utcnow_iso(). Fail-soft: an executor error -> 'uncertain', never a
crash. Transient LLM failures re-raise TransientLLMError so the worker requeues.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("crag-engine")

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from lifecycle import _utcnow_iso  # noqa: E402
import claim_layer  # noqa: E402
import grounding_queue_v2 as gq2  # noqa: E402
import grounding_config  # noqa: E402

_CREATE_NO_WINDOW = gq2._CREATE_NO_WINDOW
_STEP_TIMEOUT = gq2._PROBE_TIMEOUT_SEC

# lane per class
_LANE = {
    claim_layer.P1_MECHANICAL: "shell",
    claim_layer.P2_DOCUMENTARY: "local",
    claim_layer.P3_TEMPORAL: "local",
    claim_layer.P4_SEMANTIC: "llm",
    claim_layer.P5_AXIOMATIC: "none",  # never queued
}


def lane_for(predicate_class: str) -> str:
    return _LANE.get(predicate_class, "local")


def _run_ro(cmd: str) -> str:
    """Run a single read-only bash command through the SACRED guard. Returns
    output (stdout+stderr, capped) or a sentinel string on skip/timeout."""
    if not gq2._is_read_only(cmd):
        return "<skipped: write-token detected>"
    try:
        p = subprocess.run(
            ["bash", "-lc", cmd],
            capture_output=True, text=True, timeout=_STEP_TIMEOUT,
            creationflags=_CREATE_NO_WINDOW,
        )
        return ((p.stdout or "") + (p.stderr or "")).strip()[:600]
    except subprocess.TimeoutExpired:
        return "<timeout>"
    except Exception as exc:
        return f"<exec-error: {exc}>"


# ===========================================================================
# P1 mechanical — read-only shell {cmd, expect}
# ===========================================================================

def verify_p1(spec: dict) -> dict:
    cmd = str((spec or {}).get("cmd", "")).strip()
    expect = str((spec or {}).get("expect", "")).strip()
    if not cmd:
        return {"verdict": "uncertain", "reasoning": "no cmd in P1 spec", "evidence": ""}
    out = _run_ro(cmd)
    if out.startswith("<skipped") or out.startswith("<timeout") or out.startswith("<exec-error"):
        return {"verdict": "uncertain", "reasoning": f"probe error: {out}", "evidence": out}
    # No expect substring authored -> existence/non-empty semantics: pass if any output.
    if not expect:
        verdict = "pass" if out else "fail"
        return {"verdict": verdict, "reasoning": "existence probe (no expect substring)", "evidence": out}
    verdict = "pass" if expect.lower() in out.lower() else "fail"
    return {"verdict": verdict,
            "reasoning": f"expect {'found' if verdict == 'pass' else 'MISSING'}: {expect!r}",
            "evidence": out}


# ===========================================================================
# P2 documentary — source anchor {file, load_bearing_substrings[]|region_hash}
# ===========================================================================

def verify_p2(spec: dict) -> dict:
    spec = spec or {}
    f = str(spec.get("file", "") or "").strip()
    subs = spec.get("load_bearing_substrings") or []
    if not f:
        return {"verdict": "uncertain", "reasoning": "no file anchor in P2 spec", "evidence": ""}
    # 1. File must exist.
    exists_out = _run_ro(f"test -e '{f}' && echo PRESENT || echo MISSING")
    if "MISSING" in exists_out:
        return {"verdict": "fail", "reasoning": f"anchor file missing: {f}", "evidence": exists_out}
    if "PRESENT" not in exists_out:
        return {"verdict": "uncertain", "reasoning": "anchor existence probe inconclusive", "evidence": exists_out}
    # 2. Load-bearing substrings must still be present in the file.
    if not subs:
        return {"verdict": "pass", "reasoning": "anchor file present (no substrings to check)", "evidence": exists_out}
    missing = []
    ev_lines = [exists_out]
    for s in subs[:5]:
        s = str(s).strip()
        if not s:
            continue
        # Fixed-string grep (-F), quote for safety; guard still applies.
        out = _run_ro(f"grep -Fq -- {json.dumps(s)} '{f}' && echo FOUND || echo ABSENT")
        ev_lines.append(f"{s}: {out}")
        if "FOUND" not in out:
            missing.append(s)
    if missing:
        return {"verdict": "fail",
                "reasoning": f"load-bearing substrings absent from {f}: {missing}",
                "evidence": "\n".join(ev_lines)}
    return {"verdict": "pass", "reasoning": f"all {len(subs)} substrings present in {f}",
            "evidence": "\n".join(ev_lines)}


# ===========================================================================
# P3 temporal — event assertion vs local ground truth (git log / events / ledger)
# ===========================================================================

def verify_p3(conn, claim_text: str, spec: dict) -> dict:
    """Best-effort local-truth check. We look for corroboration of the event in
    git log (default branch) and, when a PR/commit ref is present, its presence.
    A P3 claim is a PAST event; absence of contradicting evidence => pass
    (events don't un-happen). We only FAIL when a referenced artifact is
    demonstrably absent (e.g. a commit SHA not in history)."""
    import re
    text = claim_text or ""
    ev_lines = []

    # Commit SHA reference -> must be reachable in history.
    m = re.search(r"\bcommit ([0-9a-f]{7,40})\b", text, re.I)
    if m:
        sha = m.group(1)
        out = _run_ro(f"git cat-file -t {sha} 2>/dev/null || echo ABSENT")
        ev_lines.append(f"commit {sha}: {out}")
        if "ABSENT" in out or out.strip() == "":
            return {"verdict": "fail", "reasoning": f"referenced commit {sha} not in history",
                    "evidence": "\n".join(ev_lines)}
        return {"verdict": "pass", "reasoning": f"commit {sha} present", "evidence": "\n".join(ev_lines)}

    # PR reference -> corroborate in git log messages (best-effort).
    m = re.search(r"\bpr #?(\d+)\b", text, re.I)
    if m:
        pr = m.group(1)
        out = _run_ro(f"git log --oneline -50 | grep -F '#{pr}' | head -3")
        ev_lines.append(f"PR #{pr} in recent log: {out or '(none)'}")
        verdict = "pass" if out.strip() else "uncertain"
        return {"verdict": verdict,
                "reasoning": f"PR #{pr} {'corroborated' if out.strip() else 'not found in recent 50 commits'}",
                "evidence": "\n".join(ev_lines)}

    # Bare event assertion with a date: treat as pass (event is historical; no
    # local artifact to falsify). This keeps P3 from churning as 'failed'.
    return {"verdict": "pass", "reasoning": "historical event assertion; no falsifiable local artifact",
            "evidence": "P3: no commit/PR ref to check"}


# ===========================================================================
# P4 semantic — evidence-bundle recipe {sources[], question} -> LLM verdict
# ===========================================================================

_P4_VERDICT_SYSTEM = (
    "You are a fact-checker. Given a CLAIM and freshly-gathered EVIDENCE, decide "
    "if the claim still holds. Return STRICT JSON: "
    '{"verdict": "pass|fail|uncertain", "reasoning": "<one sentence>"}. '
    "'pass' = evidence supports the claim; 'fail' = evidence contradicts it; "
    "'uncertain' = evidence insufficient. Output ONLY the JSON object."
)


def verify_p4(claim_text: str, spec: dict, llm: Any) -> dict:
    spec = spec or {}
    sources = spec.get("sources") or []
    question = str(spec.get("question", "") or "").strip()
    if not sources:
        return {"verdict": "uncertain", "reasoning": "no evidence sources in P4 spec", "evidence": ""}
    if llm is None:
        return {"verdict": "uncertain", "reasoning": "no LLM available for P4 verdict", "evidence": ""}

    gathered = []
    for s in sources[:6]:
        s = str(s).strip()
        if not s:
            continue
        gathered.append(f"$ {s}\n{_run_ro(s)}")
    evidence = "\n\n".join(gathered)

    payload = (
        f"CLAIM:\n{claim_text}\n\nQUESTION: {question}\n\nEVIDENCE:\n{evidence}"
    )
    cfg = grounding_config.get_config()
    model = claim_layer.get_role_model("verdict")
    try:
        resp = gq2.llm_client.call_with_retry(
            llm, model=model, max_tokens=cfg.adjudicate_max_tokens,
            messages=[{"role": "user", "content": _P4_VERDICT_SYSTEM + "\n\n" + payload}],
        )
        gq2.llm_client.record_usage(resp, model=model, provider=cfg.provider)
        text = resp.content[0].text if getattr(resp, "content", None) else ""
    except gq2.llm_client.TransientLLMError:
        raise  # worker requeues
    except Exception as exc:
        return {"verdict": "uncertain", "reasoning": f"P4 LLM error: {exc}", "evidence": evidence}

    obj = claim_layer._parse_decompose_json_obj(text) or {}
    verdict = str(obj.get("verdict", "uncertain")).lower()
    if verdict not in ("pass", "fail", "uncertain"):
        verdict = "uncertain"
    return {"verdict": verdict, "reasoning": str(obj.get("reasoning", ""))[:400], "evidence": evidence}


# ===========================================================================
# Worker — drain one CLAIM verify job (claim_kind='claim')
# ===========================================================================

def verify_claim(conn, claim_id: int, llm: Any = None) -> Optional[dict]:
    """Load a claim, dispatch to its class executor, persist verdict + history.
    Returns the verdict dict, or None if the claim is missing/P5. Re-raises
    TransientLLMError (P4 path) so the worker requeues."""
    row = conn.execute(
        "SELECT id, text, predicate_class, predicate_spec, predicate_version "
        "FROM claims WHERE id=? AND status='active'", (claim_id,),
    ).fetchone()
    if not row:
        return None
    pclass = row["predicate_class"]
    if pclass == claim_layer.P5_AXIOMATIC:
        return None  # terminal — never verified

    spec = {}
    if row["predicate_spec"]:
        try:
            spec = json.loads(row["predicate_spec"])
        except Exception:
            spec = {}

    if pclass == claim_layer.P1_MECHANICAL:
        result = verify_p1(spec)
    elif pclass == claim_layer.P2_DOCUMENTARY:
        result = verify_p2(spec)
    elif pclass == claim_layer.P3_TEMPORAL:
        result = verify_p3(conn, row["text"], spec)
    elif pclass == claim_layer.P4_SEMANTIC:
        result = verify_p4(row["text"], spec, llm)
    else:
        result = {"verdict": "uncertain", "reasoning": f"unknown class {pclass!r}", "evidence": ""}

    verdict = result.get("verdict", "uncertain")
    now = _utcnow_iso()
    lane = lane_for(pclass)

    # Persist verdict to the claim + append history.
    try:
        if verdict == "pass":
            conn.execute("UPDATE claims SET last_verdict='pass', grounded_at=?, grounding_due=0, updated_at=? WHERE id=?",
                         (now, now, claim_id))
        elif verdict == "fail":
            conn.execute("UPDATE claims SET last_verdict='fail', grounding_due=1, updated_at=? WHERE id=?",
                         (now, claim_id))
        else:
            conn.execute("UPDATE claims SET last_verdict=?, updated_at=? WHERE id=?",
                         (verdict, now, claim_id))
        conn.execute(
            "INSERT INTO grounding_history (claim_kind, claim_id, ts, job_type, verdict, reasoning, evidence, recipe_version, lane) "
            "VALUES ('claim', ?, ?, 'verify', ?, ?, ?, ?, ?)",
            (claim_id, now, verdict, result.get("reasoning", ""), result.get("evidence", ""),
             row["predicate_version"], lane),
        )
        conn.commit()
    except Exception as exc:
        logger.warning("claim_verify: persist failed for claim %s: %s", claim_id, exc)

    return result
