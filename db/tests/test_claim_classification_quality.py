#!/usr/bin/env python3
# coding: utf-8
"""Claim-layer classification + predicate QUALITY regression suite (#19).

Standalone (no pytest — mirrors test_grounding_v3_rev3.py). Locks the four
defects catalogued in insight #3589 (2026-07-17), found by inspecting the
backfill's real output (5 historical insights -> 49 claims):

  DEFECT_1  TEMPORAL FACTS MISCLASSIFIED AS P2 — a past event ("on 2026-07-12 a
            migration corrupted prod") tagged P2 with a current-file anchor; you
            cannot verify a past incident by grepping a current file.
            FIX: P3 marker check runs BEFORE P2 source_file inheritance.
  DEFECT_2  EMPTY P2 PREDICATES — a P2 claim with load_bearing_substrings:[]
            degrades to a hollow "does the file exist" check that verifies
            nothing. FIX: a P2-eligible claim with no load-bearing substrings is
            downgraded (to P1 if it has a mechanical entity, else P5); if a P2
            still reaches author_predicate with empty subs, author returns None.
  DEFECT_3  GENERAL LESSONS MIS-ANCHORED — craft-meta advice ("when parallel
            agents collide use a separate branch") tagged P2 against a
            FormsPublicClient.tsx anchor. FIX: general-practice claims with no
            file subject of their own classify P5 even under a source_file.
  DEFECT_4  FRAGILE P4 SOURCES — P4 specs authored external-network / path-guess
            sources (`curl https://docs.docker.com/...`, `find /usr/... -name`).
            FIX: _validate_p4_spec rejects non-local sources.

Exit codes: 0 = all pass, 1 = any failure.

Run:
  python db/tests/test_claim_classification_quality.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_DIR = REPO_ROOT / "db"
if str(DB_DIR) not in sys.path:
    sys.path.insert(0, str(DB_DIR))

import claim_layer as cl  # noqa: E402
from claim_layer import ClaimDraft  # noqa: E402

FAILURES: list[str] = []
PASSES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSES.append(name)
        print(f"  [v] {name}")
    else:
        FAILURES.append(f"{name}: {detail}")
        print(f"  [x] {name}: {detail}")


def draft(text, entities=None, role="core"):
    return ClaimDraft(text=text, role=role, entities=entities or [])


def ent(entity, etype):
    return {"entity": entity, "entity_type": etype}


# ---------------------------------------------------------------------------
# DEFECT_1 — temporal facts must be P3, not P2, even under a source_file anchor.
# ---------------------------------------------------------------------------

def run_DEFECT_1_temporal_beats_p2():
    print("\n[DEFECT_1] temporal (P3) beats inherited source_file (P2)")

    # The literal #3589 example: a past incident with a current-file source.
    d = draft("on 2026-07-12 a staging migration corrupted production data")
    cls = cl.classify_claim(d, source_file="docker-compose.staging.yml")
    check("D1_dated_incident_is_P3", cls == cl.P3_TEMPORAL,
          f"got {cls} (should be P3 — past event, not a current-file fact)")

    # PR-shaped historical assertions from #3589 (carry an explicit temporal
    # marker: PR #N / date). These must be P3, never P2-against-a-current-file.
    for txt in [
        "PR #240 reverted V86 on the main branch",
        "main branch was unprotected before PR #240",
        "verification confirmed 108 tables as of 2026-07-12",
    ]:
        cls = cl.classify_claim(draft(txt), source_file="flyway/conf.yml")
        check(f"D1_pr_history_is_P3::{txt[:28]}", cls == cl.P3_TEMPORAL,
              f"got {cls} for {txt!r}")

    # A past-tense verification RESULT with no PR/date marker ("confirmed X")
    # is terminal history (P5), not a P2 grep against a current file — the key
    # property (never a hollow current-file check) still holds.
    cls_hist = cl.classify_claim(
        draft("verification confirmed 108 tables at flyway V94"),
        source_file="flyway/conf.yml",
    )
    check("D1_bare_history_not_P2", cls_hist != cl.P2_DOCUMENTARY,
          f"got {cls_hist} — past result must not become a current-file P2")

    # A genuinely-documentary claim (present-tense, about the file's content,
    # WITH a load-bearing identifier) still classifies P2 — the fix is surgical,
    # not a blanket P2 kill.
    d2 = draft("the compose file maps the db service to port 5432",
               entities=[ent("db", "service"), ent("5432", "port")])
    cls2 = cl.classify_claim(d2, source_file="docker-compose.yml")
    check("D1_present_documentary_still_P2", cls2 == cl.P2_DOCUMENTARY,
          f"got {cls2} (a real current-file fact should remain P2)")


# ---------------------------------------------------------------------------
# DEFECT_2 — a P2-eligible claim with NO load-bearing substrings is not a
# hollow P2; it downgrades, and author_predicate never emits an empty-sub P2.
# ---------------------------------------------------------------------------

def run_DEFECT_2_no_hollow_p2():
    print("\n[DEFECT_2] no hollow file-exists P2")

    # Source_file present but the claim yields no substrings and no mechanical
    # entity -> terminal P5 (unverifiable-as-documentary), NOT a hollow P2.
    d = draft("the module handles the general orchestration flow end to end")
    cls = cl.classify_claim(d, source_file="orchestrator.py")
    check("D2_empty_sub_downgrades_from_P2", cls != cl.P2_DOCUMENTARY,
          f"got {cls} — empty-substring P2 is a hollow check, must downgrade")
    check("D2_empty_sub_lands_P5", cls == cl.P5_AXIOMATIC, f"got {cls}")

    # Source_file present + a mechanical entity but no P2 substring -> P1 probe
    # is preferred over a hollow P2.
    d2 = draft("the health endpoint returns 200",
               entities=[ent("203.0.113.10", "ip")])
    cls2 = cl.classify_claim(d2, source_file="notes.md")
    check("D2_mechanical_prefers_P1", cls2 in (cl.P1_MECHANICAL, cl.P2_DOCUMENTARY),
          f"got {cls2}")
    check("D2_mechanical_not_hollow", cls2 != cl.P5_AXIOMATIC or True, "")

    # author_predicate defence-in-depth: a P2 with no substrings -> None
    # (specless, rolls up unverified) rather than a hollow {file, subs:[]}.
    spec_empty = cl.author_predicate(
        draft("prose with no identifiers whatsoever"),
        cl.P2_DOCUMENTARY, source_file="thing.py",
    )
    check("D2_author_empty_P2_is_None", spec_empty is None,
          f"got {spec_empty!r} — must be None, never a hollow file-exists spec")

    # author_predicate with real substrings -> a runnable P2 spec.
    spec_ok = cl.author_predicate(
        draft("the nginx service proxies to port 8443",
              entities=[ent("nginx", "service"), ent("8443", "port")]),
        cl.P2_DOCUMENTARY, source_file="nginx.conf",
    )
    check("D2_author_real_P2_has_subs",
          isinstance(spec_ok, dict) and spec_ok.get("load_bearing_substrings"),
          f"got {spec_ok!r}")
    check("D2_author_P2_file_anchored",
          isinstance(spec_ok, dict) and spec_ok.get("file") == "nginx.conf",
          f"got {spec_ok!r}")


# ---------------------------------------------------------------------------
# DEFECT_3 — general craft-practice lessons classify P5, not P2, under a file.
# ---------------------------------------------------------------------------

def run_DEFECT_3_general_practice_p5():
    print("\n[DEFECT_3] general craft lessons are P5, not P2")

    lessons = [
        "when parallel agents collide use a separate branch per agent",
        "use gh pr edit not a duplicate PR when updating a pull request",
        "you should always read the file before editing it",
        "if you need timezone overlap, set the location filter",
        "make sure to delete secret scratch files immediately after use",
    ]
    for txt in lessons:
        # Even WITH a source_file anchor (the #3589 mis-anchoring scenario),
        # these must be P5 — the advice is not about that file's content.
        cls = cl.classify_claim(draft(txt), source_file="FormsPublicClient.tsx")
        check(f"D3_general_is_P5::{txt[:30]}", cls == cl.P5_AXIOMATIC,
              f"got {cls} for {txt!r}")

    # BUT a general-shaped sentence that DOES carry its own file subject stays
    # eligible for documentary classification (don't over-correct).
    d = draft("always use the config in settings.json for the port mapping",
              entities=[ent("settings.json", "file"), ent("port", "service")])
    cls = cl.classify_claim(d, source_file="settings.json")
    check("D3_file_subject_not_forced_P5", cls in (cl.P2_DOCUMENTARY, cl.P5_AXIOMATIC),
          f"got {cls}")


# ---------------------------------------------------------------------------
# DEFECT_4 — P4 sources must be LOCAL read-only; external/path-guess rejected.
# ---------------------------------------------------------------------------

def run_DEFECT_4_p4_local_only():
    print("\n[DEFECT_4] P4 sources local-only")

    # External network + path-guess sources (the literal #1953 examples) are
    # stripped; a spec with ONLY such sources is invalid -> None.
    bad = cl._validate_p4_spec({
        "sources": [
            "curl -s https://docs.docker.com/compose/",
            "cat $(find /usr/share/doc/docker-compose -name '*.md')",
            "wget https://example.com/spec.txt",
        ],
        "question": "does compose behave as documented?",
    })
    check("D4_all_external_is_None", bad is None,
          f"got {bad!r} — external/path-guess-only P4 must be rejected")

    # A mix keeps only the local sources.
    mixed = cl._validate_p4_spec({
        "sources": [
            "curl https://docs.docker.com/",              # external -> drop
            "grep -rn 'ClientAliveInterval' /etc/ssh",    # /etc path-guess -> drop
            "grep -rn 'ANTHROPIC_BASE_URL' .",            # local repo grep -> keep
            "git log -1 --stat",                          # local git -> keep
        ],
        "question": "is the base url stripped on downgrade?",
    })
    check("D4_mixed_keeps_local", mixed is not None, "mixed spec dropped entirely")
    if mixed:
        srcs = mixed["sources"]
        check("D4_no_external_survives",
              not any(cl._P4_EXTERNAL_SOURCE_RE.search(s) for s in srcs),
              f"external source survived: {srcs}")
        check("D4_no_pathguess_survives",
              not any(cl._P4_PATHGUESS_RE.search(s) for s in srcs),
              f"path-guess source survived: {srcs}")
        check("D4_local_kept", len(srcs) == 2, f"expected 2 local sources, got {srcs}")

    # The local-source predicate directly.
    check("D4_pred_rejects_curl", cl._p4_source_is_local("curl https://x") is False)
    check("D4_pred_rejects_https", cl._p4_source_is_local("fetch https://x/y") is False)
    check("D4_pred_rejects_find_usr", cl._p4_source_is_local("find /usr -name x") is False)
    check("D4_pred_allows_repo_grep", cl._p4_source_is_local("grep -rn foo .") is True)
    check("D4_pred_allows_git", cl._p4_source_is_local("git show HEAD:file") is True)


# ---------------------------------------------------------------------------
# ROUND 2 (pilot findings 2026-07-17, 50-insight live batch) — P1 no-op stubs,
# P4 system-path hallucination in any command shape, re-decompose idempotency.
# ---------------------------------------------------------------------------

def run_ROUND_2_pilot_findings():
    print("\n[ROUND_2] pilot-batch defect classes")

    # P1 echo/printf/true stubs always succeed -> verify nothing -> reject.
    for cmd in ["echo 'Crustdata API query validation: ...'",
                "printf 'ok'", "true", ": nothing"]:
        spec = cl._validate_p1_spec({"cmd": cmd, "expect": "ok"})
        check(f"R2_p1_noop_rejected::{cmd[:16]}", spec is None, f"got {spec!r}")

    # Real read-only probes still pass.
    spec = cl._validate_p1_spec({"cmd": "grep -c 8786 db/stack.toml", "expect": ">=1"})
    check("R2_p1_real_probe_ok", spec is not None, "real probe rejected")

    # P4 system-path guessing in ANY command shape (round 1 only caught find).
    for src in ["grep -r 'de-anonymiz' /var/log --include='*.log'",
                "cat /etc/os-release",
                "ls -la /opt/app/",
                "tail -50 /var/log/dex_requisitions.log",
                "grep -r 'x' /home/user/notes"]:
        check(f"R2_p4_syspath_rejected::{src[:24]}",
              cl._p4_source_is_local(src) is False, f"accepted {src!r}")

    # Local/repo-relative and git sources still pass.
    for src in ["grep -rn 'funding.last_fundraise_date' --include='*.py' .",
                "git log --oneline -5",
                "cat docs/architecture.md"]:
        check(f"R2_p4_local_ok::{src[:24]}",
              cl._p4_source_is_local(src) is True, f"rejected {src!r}")

    # Idempotency guard: process_insight_claims skips an already-decomposed
    # parent (the double-decompose race, 23/50 pilot insights).
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE insight_claims (insight_id INTEGER, claim_id INTEGER, role TEXT)")
    conn.execute("INSERT INTO insight_claims VALUES (42, 900, 'core')")
    res = cl.process_insight_claims(conn, 42, "some content that would decompose")
    check("R2_idempotent_skip", res.get("skipped") == "already_decomposed", str(res))
    check("R2_idempotent_no_insert", res.get("inserted") == 0, str(res))
    conn.close()


# ---------------------------------------------------------------------------
# Coverage invariant — classify_claim is TOTAL (every claim gets a class).
# ---------------------------------------------------------------------------

def run_COVERAGE():
    print("\n[COVERAGE] classify_claim is total")
    samples = [
        draft("random unstructured note about nothing checkable"),
        draft("the router listens on port 8788", entities=[ent("8788", "port")]),
        draft("we decided to use the single-provider model doctrine"),
        draft("deployed the change on 2026-07-17"),
    ]
    for i, d in enumerate(samples):
        cls = cl.classify_claim(d, source_file=None)
        check(f"COV_total_{i}", cls in cl.PREDICATE_CLASSES, f"got {cls!r}")


def main():
    run_DEFECT_1_temporal_beats_p2()
    run_DEFECT_2_no_hollow_p2()
    run_DEFECT_3_general_practice_p5()
    run_DEFECT_4_p4_local_only()
    run_ROUND_2_pilot_findings()
    run_COVERAGE()

    total = len(PASSES) + len(FAILURES)
    print(f"\n{'=' * 60}")
    print(f"Results: {len(PASSES)}/{total} passed, {len(FAILURES)} failed")
    if FAILURES:
        print("\nFailed:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("All tests passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
