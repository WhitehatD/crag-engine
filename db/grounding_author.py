# coding: utf-8
"""Grounding v2 — Tier-A/B classification, LLM recipe authoring, LLM adjudication.

Architecture
------------
Two-tier falsification (see docs/architecture.md §A2 and migration 026):

  Tier A (mechanical): the claim's truth IS existence — path present, port
    bound, host reachable. `entity_extract.derive_falsifier` is correct here
    and makes zero LLM calls. Cost: free.

  Tier B (agentic): predicate-bearing claims (config values, behaviour under
    conditions, cron cadence, auth mode, negation assertions, etc.). The LLM
    authors a `falsification_question` (NL) and a structured `recipe`
    ({steps, refutes_if, supports_if}). A separate adjudication LLM call reads
    the ACTUAL step outputs + prior chain-of-thought and renders a verdict.

Public API (pure functions; take conn/llm as args — house style from lifecycle.py):

  classify_tier(content, entities) -> 'A' | 'B'
  author_recipe(claim_content, entities, llm) -> dict | None
  adjudicate(claim, recipe, step_outputs, prior_history, llm)
              -> {verdict, reasoning, evidence}

Timestamp: always _utcnow_iso() from db/lifecycle.py. NEVER datetime('now').
Write-guard: _FORBIDDEN tuple below (copy from apps/cron/groundskeeper.py:85-89)
  — validated on every LLM-authored step before the row is persisted.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

logger = logging.getLogger("crag-anchor")


# ---------------------------------------------------------------------------
# Write-guard — authoritative copy matches groundskeeper.py:85-89 (Tier-A
# path; NOT updated by this pass — see docs/architecture.md for the
# divergence note) and grounding_queue_v2.py (defence-in-depth re-check).
# Any write-capable token OR secret-exfiltration pattern in a recipe step
# → recipe is REJECTED.
# ---------------------------------------------------------------------------

# Safe redirect forms that must NOT trip the bare '>' guard below: fd-to-
# devnull and fd-dup redirects are stderr/stdout plumbing (e.g. the common
# `2>/dev/null` idiom on read-only shell commands), not file writes. Stripped
# out BEFORE the substring check so a real file-write redirect (`> file`,
# `>> file`) still trips the bare '>' / '>>' tokens in _FORBIDDEN.
_SAFE_REDIRECT_RE = re.compile(
    r"[0-9]?>{1,2}\s*/dev/null"   # 2>/dev/null, >/dev/null, 2>>/dev/null
    r"|&>{1,2}\s*/dev/null"       # &>/dev/null, &>>/dev/null (both streams)
    r"|[0-9]>&[0-9]"              # 2>&1, 1>&2 (fd duplication — no file write)
)


def _strip_safe_redirects(step: str) -> str:
    """Remove fd-to-devnull / fd-dup redirects so they don't trip the bare
    '>' guard. Any '>' surviving this strip is a real file-write redirect."""
    return _SAFE_REDIRECT_RE.sub(" ", step)


_FORBIDDEN = (
    " rm ", " mv ", " cp ", " dd ", ">", ">>", "tee ", "rmdir", "del ",
    "DELETE", "DROP", "INSERT", "UPDATE", "-X POST", "-X PUT", "-X DELETE",
    "-X PATCH", "--request ", "--data", "--upload-file",
    "curl -o", "curl -O", "--output", "git push", "git commit",
    "chmod", "chown", "kill ",
)

# Secret-exfiltration guard: recipe step OUTPUT is persisted verbatim into
# grounding_history (append-only) and re-fed to the adjudication LLM on every
# re-ground cycle, so a step that reads a secret "just to check" it leaks the
# secret into that durable, LLM-visible trail. Each pattern documents the
# concrete exfiltration vector it blocks.
_SECRET_PATTERNS = (
    re.compile(r"\bget secret\b", re.I),               # kubectl/gcloud "get secret <name>" — dumps a Secret resource
    re.compile(r"(?=.*\bjsonpath\b)(?=.*\bsecret\b)", re.I),  # jsonpath extraction of a Secret field (e.g. -o jsonpath='{.data.KEY}')
    re.compile(r"\bbase64\s+(-d|--decode)\b", re.I),    # decoding a base64-encoded secret/credential blob
    re.compile(r"\bcat\b.*\.env\b(?!\.example)"),       # `cat .env` (real dotenv) — grep and .env.example stay allowed
    re.compile(r"/\.credentials(?:\.json)?\b"),         # credential-store files, e.g. ~/.claude/.credentials.json
    re.compile(r"--token\b", re.I),                     # inline token/PAT arguments
    re.compile(r"\bauthorization\s*:", re.I),           # echoing an Authorization: header value
)

# Live-credential-SHAPE redaction — independent of _SECRET_PATTERNS above.
# _SECRET_PATTERNS blocks *steps that would fetch/decode* a secret; this catches
# the case where a credential value is ALREADY present verbatim in the claim
# content (e.g. an insight where a user asked to remember a full API key) and
# the LLM echoes it back into falsification_question/refutes_if/supports_if.
# Those fields are persisted into the `falsifiers` table and re-fed into
# grounding_history on every re-ground cycle — durable, LLM-visible storage —
# so any credential-shaped substring is redacted before persistence rather
# than trusting the LLM's own "don't echo secrets" instruction (2026-07-05:
# insight #2048's stored Anthropic key was echoed into 3 falsification
# questions and written to daemon.stderr.log before this guard existed).
_CREDENTIAL_SHAPE_RE = re.compile(
    r"sk-ant-api\d{2}-[A-Za-z0-9_-]{20,}"   # Anthropic API key
    r"|sk-[A-Za-z0-9]{20,}"                 # OpenAI-style secret key
    r"|AKIA[0-9A-Z]{16}"                    # AWS access key ID
    r"|gh[pousr]_[A-Za-z0-9]{30,}"          # GitHub PAT (ghp_/gho_/ghu_/ghs_/ghr_)
    r"|github_pat_[A-Za-z0-9_]{30,}"        # GitHub fine-grained PAT
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"        # Slack token
    r"|Bearer\s+[A-Za-z0-9\-_.]{20,}"       # inline bearer token
)


def _redact_credential_shapes(text: str) -> str:
    """Replace any live-credential-shaped substring with a redaction marker."""
    return _CREDENTIAL_SHAPE_RE.sub("[REDACTED-CREDENTIAL]", text)


# Matches an opening code fence anywhere in the text, with or without a
# language tag (```json, ```JSON, or bare ```).
_FENCE_OPEN_RE = re.compile(r"```[a-zA-Z]*\s*\n?")


def _extract_json_object(raw: str) -> Any:
    """Parse a JSON object out of raw LLM text, tolerating the failure modes
    that plain `json.loads(raw)` cannot survive:

    1. Markdown code fences (```json ... ```), including when the model adds
       explanatory prose BEFORE the opening fence or AFTER the closing fence.
    2. Trailing prose immediately after the JSON value with no fence at all
       (e.g. "...}\\nLet me know if you need anything else.").

    Root-caused 2026-07-05 (insight #3317): the previous implementation only
    stripped a fence when `lines[-1].strip() == "```"` held EXACTLY — i.e.
    only when the closing fence was the very last line of the response. Haiku
    routinely appends a trailing sentence after the closing fence, which left
    that stray text in `text` and made `json.loads` raise
    `json.JSONDecodeError: Extra data` on ~otherwise-valid recipes. This was
    the PRIMARY cause of the 952-failure incident, not an LLM-availability
    problem (the LLM client works and responds on every call).

    Strategy: locate the first '{' in the (fence-stripped) text and use
    `json.JSONDecoder.raw_decode` from that offset, which parses exactly one
    JSON value and explicitly ignores anything the model wrote afterward —
    the same "ignore trailing garbage" semantics `json.loads` refuses to give.

    Raises json.JSONDecodeError (same contract as json.loads) if no JSON
    object can be found.
    """
    text = _FENCE_OPEN_RE.sub("", raw, count=1)
    text = text.replace("```", "")

    start = text.find("{")
    if start == -1:
        raise json.JSONDecodeError("no JSON object found in LLM output", text, 0)

    return json.JSONDecoder().raw_decode(text, start)[0]


def _is_read_only(step: str) -> bool:
    """True iff the step string contains no write-capable token and no
    secret-exfiltration pattern."""
    s = f" {_strip_safe_redirects(step)} "
    if any(tok in s for tok in _FORBIDDEN):
        return False
    if any(pat.search(step) for pat in _SECRET_PATTERNS):
        return False
    return True


# ---------------------------------------------------------------------------
# Tier classification
# ---------------------------------------------------------------------------

# Indicators that the claim's truth is PURELY about existence (Tier A territory).
_PURE_EXISTENCE_PATTERNS = [
    # file/path/directory existence
    re.compile(r"\bexists?\b", re.I),
    re.compile(r"\bpresent\b", re.I),
    re.compile(r"\bis (a|an)?\s*(file|dir|folder|path|port|host|service|server)\b", re.I),
    re.compile(r"\blisten(s|ing)? on\b", re.I),
    re.compile(r"\breachab(le|ility)\b", re.I),
    re.compile(r"\bbound\b", re.I),        # "port X is bound"
    re.compile(r"\bup\b.*\bport\b", re.I),
]

# Indicators of PREDICATE-bearing content that demands Tier B.
_PREDICATE_PATTERNS = [
    re.compile(r"\b(is|are|was|were)\s+(off|on|enabled|disabled|required|allowed|denied)\b", re.I),
    re.compile(r"\bdefault\b", re.I),
    re.compile(r"\bconfig(uration)?\b", re.I),
    re.compile(r"\bsetting\b", re.I),
    re.compile(r"\bvalue\b", re.I),
    re.compile(r"\bequal(s)?\b", re.I),
    re.compile(r"\bset to\b", re.I),
    re.compile(r"\b(require[sd]?|enforce[sd]?)\b", re.I),
    re.compile(r"\bauth(entication)?\b", re.I),
    re.compile(r"\bbearer\b", re.I),
    re.compile(r"\btoken\b", re.I),
    re.compile(r"\bcron\b", re.I),
    re.compile(r"\brun(s|ning)? every\b", re.I),
    re.compile(r"\bschedule[d]?\b", re.I),
    re.compile(r"\bnot\s+\w+\b", re.I),      # negation
    re.compile(r"\bdoes\s+not\b", re.I),
    re.compile(r"\bno\b\s+\w+\b", re.I),
    re.compile(r"\b\d+\s*(min|hour|day|second|ms|sec)\b", re.I),  # duration/cadence
    re.compile(r"\bversion\b", re.I),
    re.compile(r"\bonly\b", re.I),
    re.compile(r"\bblocks?\b", re.I),
    re.compile(r"\ballow(s)?\b", re.I),
    re.compile(r"\bdeny\b", re.I),
    re.compile(r"\bmin(imum)?\b", re.I),
    re.compile(r"\bmax(imum)?\b", re.I),
    re.compile(r"\bthreshold\b", re.I),
    re.compile(r"\blimit\b", re.I),
    re.compile(r"\bdoes not exist\b", re.I),   # hallucination / negated-existence
    re.compile(r"\bnever\b", re.I),
    re.compile(r"\balways\b", re.I),
]

# Entity types that are inherently existence-checkable without predicate analysis.
_EXISTENCE_ENTITY_TYPES = {"ip", "domain", "port", "service"}


def classify_tier(content: str, entities: list[dict]) -> str:
    """Classify a claim as Tier-A (mechanical) or Tier-B (agentic).

    Tier-A: truth IS existence — ports bound, hosts reachable, paths present.
    Tier-B: predicate-bearing — config values, negations, cadence, auth mode, etc.
    When in doubt → 'B' (safety default: LLM authoring is better than silence).

    Args:
        content:  The full claim text.
        entities: List of entity dicts from entity_extract.extract_entities().

    Returns:
        'A' or 'B'.
    """
    if not content:
        return "B"

    # Step 1: if the claim has any predicate signal, it's Tier B.
    for pat in _PREDICATE_PATTERNS:
        if pat.search(content):
            return "B"

    # Step 2: if the claim is ONLY existence-typed entities + pure-existence language,
    # Tier A is safe.
    entity_types = {e.get("entity_type") for e in (entities or [])}
    has_existence_entities = bool(entity_types & _EXISTENCE_ENTITY_TYPES)
    has_pure_existence_language = any(p.search(content) for p in _PURE_EXISTENCE_PATTERNS)

    if has_existence_entities and has_pure_existence_language:
        return "A"

    # Step 3: when in doubt, Tier B (LLM is always more correct than silence).
    return "B"


# ---------------------------------------------------------------------------
# Recipe authoring
# ---------------------------------------------------------------------------

_AUTHOR_SYSTEM = """\
You are a falsification-recipe author for a knowledge-management system.
Your job: given a factual claim, write a read-only verification recipe so a
computer can test whether the claim is STILL TRUE by running safe shell
commands and reading their output.

Output ONLY valid JSON (no markdown, no code fences, no prose before/after).
There are TWO possible response shapes — pick exactly one:

1. A verification recipe:
{
  "falsification_question": "<one sentence: what would disprove this claim?>",
  "recipe": {
    "steps": ["<safe, read-only shell command 1>", "<...>"],
    "refutes_if": "<exact output pattern / condition that means the claim is FALSE>",
    "supports_if": "<exact output pattern / condition that means the claim is STILL TRUE>"
  }
}

2. An unverifiable declaration — use this INSTEAD of a recipe when, and ONLY
   when, the claim can only be checked by reading a secret value, decoding a
   credential, or performing a mutation:
{
  "unverifiable": true,
  "reason": "<one sentence: why this claim cannot be safely auto-verified>"
}

Rules:
- Every step MUST be read-only. NEVER include: rm, mv, cp, dd, >, >>, tee,
  DELETE, DROP, INSERT, UPDATE, curl -o/--output, git push, git commit, chmod,
  chown, kill.
- NEVER read, print, decode, or echo a secret value, credential, API key, or
  auth token — not even "just to check" it. This includes `kubectl get secret`,
  `base64 -d`/`--decode` on credential material, `cat`-ing a `.env` or
  `.credentials` file, or echoing an `Authorization:` header.
- NEVER include a step that mutates state (write, delete, restart, deploy).
- If the ONLY way to verify the claim is to read a secret or mutate something,
  return the unverifiable shape above. Do not invent a workaround that
  technically dodges the forbidden tokens but still exposes secret material.
- steps should be grep, cat (non-secret files), curl -s GET, systemctl status,
  docker ps, find, test, etc.
- steps should test the PREDICATE of the claim, NOT just that a noun exists.
- falsification_question should be a concrete, unambiguous yes/no question.
- refutes_if and supports_if must be concrete (not vague like "output is unexpected").

Example 1 — good read-only recipe:
Claim: "the daemon listens on port 8786"
{
  "falsification_question": "Is a process bound to TCP port 8786 on localhost?",
  "recipe": {
    "steps": ["curl -sf --connect-timeout 5 http://127.0.0.1:8786/health"],
    "refutes_if": "connection refused or curl exits non-zero",
    "supports_if": "HTTP response body contains \\"ok\\""
  }
}

Example 2 — unverifiable, requires reading a secret:
Claim: "the MINIO_SECRET_KEY environment variable is a 40-character value"
{
  "unverifiable": true,
  "reason": "Verifying the value requires reading the MINIO_SECRET_KEY secret, which must never be read or echoed."
}
"""

# NOTE: default is now sourced from grounding_config (stack.toml
# [grounding.llm].author_max_tokens, default 4096 — raised 2026-07-05 from
# the old hardcoded 2048 after real-load review). This module-level constant
# is kept only as the value read when grounding_config itself is unavailable
# for any reason (defence-in-depth; grounding_config always resolves the
# same fallback internally too).
_AUTHOR_MAX_TOKENS = 4096


def author_recipe(
    claim_content: str,
    entities: list[dict],
    llm: Any,
    model: Optional[str] = None,
) -> tuple[Optional[dict], Optional[str]]:
    """Ask the LLM to author a falsification recipe for the claim.

    Args:
        claim_content: The full text of the insight or principle.
        entities:      Entity list from entity_extract.extract_entities().
        llm:           An anthropic-shaped client (from llm_client.get_client()) —
                       works with any provider llm_client wires up, since
                       non-Anthropic providers are adapter-wrapped to the
                       same `.messages.create()` interface. If None, fails
                       open with reason 'llm_unavailable'.
        model:         Override the model for this call. Used internally for
                       the escalation retry below; production callers omit
                       this and get grounding_config's primary model.

    Returns:
        (result, reason) tuple.
        result is a dict with keys {falsification_question, recipe} on success
        (recipe is {steps: list[str], refutes_if: str, supports_if: str}),
        else None.
        reason is None on success, else exactly one of the four honest
        failure-reason codes (optionally suffixed with ": <detail>"):
          'llm_unavailable'        — no client / the API call itself failed
          'malformed_output'       — JSON parse or structural validation failed
          'write_guard_rejected'   — a step contained a forbidden token
          'mechanically_unverifiable' — the LLM declared the claim unverifiable

    Escalation: a 'malformed_output' result on the primary (cheap-tier) model
    is a signal the model struggled with this claim, not a settled answer —
    per [grounding.llm].escalation_enabled, this function retries ONCE with
    escalation_model before giving up. mechanically_unverifiable/
    write_guard_rejected/llm_unavailable are NOT retried: they're valid or
    terminal answers, not "the model got it wrong" signals, and retrying
    them would just burn a second LLM call for the same result.
    """
    if llm is None:
        return None, "llm_unavailable"

    import grounding_config

    cfg = grounding_config.get_config()
    active_model = model or cfg.model

    result, reason = _author_recipe_once(claim_content, entities, llm, active_model, cfg)

    if (
        result is None
        and model is None  # only escalate from the primary call, never re-escalate
        and cfg.escalation_enabled
        and cfg.escalation_model
        and cfg.escalation_model != active_model
        and reason
        # zero-steps is escalated too: the cheap model failing to FIND read-only
        # steps is a capability signal, not a settled unverifiability verdict —
        # only after the escalation model also returns zero steps does the claim
        # get durably marked judgment. Other mechanically_unverifiable reasons
        # (the LLM explicitly declaring unverifiability) stay non-retried.
        and (reason.startswith("malformed_output") or "zero-steps recipe" in reason)
    ):
        logger.info(
            "grounding_author: author_recipe escalating %s -> %s after malformed_output",
            active_model, cfg.escalation_model,
        )
        result, reason = _author_recipe_once(claim_content, entities, llm, cfg.escalation_model, cfg)

    return result, reason


def _author_recipe_once(
    claim_content: str,
    entities: list[dict],
    llm: Any,
    model: str,
    cfg: Any,
) -> tuple[Optional[dict], Optional[str]]:
    """Single (non-retrying) author_recipe LLM call at a given model."""
    import llm_client

    entity_summary = ""
    if entities:
        parts = [f"{e.get('entity_type','?')}:{e.get('entity','?')}" for e in entities[:5]]
        entity_summary = f"\nEntities detected: {', '.join(parts)}"

    user_msg = (
        f"Claim to verify:{entity_summary}\n\n{claim_content.strip()}"
    )

    try:
        resp = llm_client.call_with_retry(
            llm,
            model=model,
            max_tokens=cfg.author_max_tokens,
            system=_AUTHOR_SYSTEM,
            # Newer Claude models (sonnet-5, haiku-4.5+) return 400
            # "temperature is deprecated for this model" — only send it when
            # explicitly enabled AND the model accepts it (pop-don't-clamp).
            **({"temperature": cfg.temperature} if cfg.send_temperature else {}),
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip() if resp.content else ""
        llm_client.record_usage(resp, model=model, provider=cfg.provider)
    except llm_client.TransientLLMError:
        # Transient transport failure (401 mid-refresh / 429 / conn) survived
        # retries — NOT an answer. Propagate to the worker so the job is
        # requeued (attempt++), never recorded as a verdict/reason.
        raise
    except Exception as exc:
        logger.warning("grounding_author: author_recipe LLM call failed: %s", exc)
        return None, f"llm_unavailable: {exc}"

    return _parse_and_validate_recipe(raw)


def _parse_and_validate_recipe(raw: str) -> tuple[Optional[dict], Optional[str]]:
    """Parse JSON from LLM output and enforce write-guard on steps.

    Returns (result, reason) — see author_recipe() docstring for the contract.
    """
    try:
        obj = _extract_json_object(raw)
    except json.JSONDecodeError as exc:
        # raw may already contain a verbatim secret if the underlying claim
        # content did (see _redact_credential_shapes docstring) — redact
        # before this ever reaches a log file.
        logger.warning(
            "grounding_author: LLM returned non-JSON: %s | raw=%r",
            exc, _redact_credential_shapes(raw[:200]),
        )
        return None, f"malformed_output: non-JSON LLM output ({exc})"

    # Structural validation
    if not isinstance(obj, dict):
        logger.warning("grounding_author: LLM output is not a JSON object")
        return None, "malformed_output: LLM output is not a JSON object"

    # Unverifiable declaration — the LLM decided this claim can only be
    # checked by reading a secret or performing a mutation.
    if obj.get("unverifiable") is True:
        reason_text = obj.get("reason", "")
        if not isinstance(reason_text, str) or not reason_text.strip():
            reason_text = "LLM declared claim unverifiable without an explanation"
        reason_text = _redact_credential_shapes(reason_text)
        logger.info(
            "grounding_author: LLM declared claim mechanically unverifiable: %s",
            reason_text[:200],
        )
        return None, f"mechanically_unverifiable: {reason_text.strip()[:300]}"

    fq = obj.get("falsification_question", "")
    recipe = obj.get("recipe", {})

    # Redact live-credential-shaped substrings BEFORE any further processing —
    # the claim content itself may already contain a verbatim secret (a user
    # can ask the engine to remember a full API key), and the LLM may echo it back
    # into free-text fields that this guard doesn't otherwise inspect.
    if isinstance(fq, str):
        fq = _redact_credential_shapes(fq)

    if not isinstance(fq, str) or not fq.strip():
        logger.warning("grounding_author: missing or empty falsification_question")
        return None, "malformed_output: missing or empty falsification_question"

    if not isinstance(recipe, dict):
        logger.warning("grounding_author: recipe is not a dict")
        return None, "malformed_output: recipe is not a dict"

    steps = recipe.get("steps", [])
    if not isinstance(steps, list):
        logger.warning("grounding_author: recipe.steps is not a list")
        return None, "malformed_output: recipe.steps is not a list"

    # Write-guard: reject any step containing a write-capable token or a
    # secret-exfiltration pattern.
    redacted_steps = []
    for i, step in enumerate(steps):
        if not isinstance(step, str):
            logger.warning("grounding_author: recipe.steps[%d] is not a string", i)
            return None, f"malformed_output: recipe.steps[{i}] is not a string"
        if not _is_read_only(step):
            logger.warning(
                "grounding_author: recipe.steps[%d] contains forbidden write/secret token — recipe rejected. step=%r",
                i, _redact_credential_shapes(step)[:120],
            )
            return None, f"write_guard_rejected: step {i} contained a forbidden token"
        redacted_steps.append(_redact_credential_shapes(step))

    refutes_if = recipe.get("refutes_if", "")
    supports_if = recipe.get("supports_if", "")
    if not isinstance(refutes_if, str) or not isinstance(supports_if, str):
        logger.warning("grounding_author: recipe.refutes_if / supports_if must be strings")
        return None, "malformed_output: recipe.refutes_if / supports_if must be strings"

    # POISON-PILL GUARD (insight #77 incident, 2026-07-06): an empty steps list
    # used to pass validation and be persisted as a "successful" authoring —
    # the sweep then saw a truthy recipe string, enqueued reground, reground
    # found no steps and re-enqueued author, which produced another empty
    # recipe... one LLM call per sweep tick, forever (recipe_version reached
    # 156). A recipe with zero runnable read-only steps means the claim's
    # predicate cannot be mechanically checked without mutation/secrets —
    # that is the mechanically_unverifiable contract, which routes to
    # mark_judgment and permanently drains the claim from the flagged pool.
    if len(redacted_steps) == 0:
        logger.info(
            "grounding_author: LLM produced a recipe with zero steps — treating as mechanically unverifiable",
        )
        return None, (
            "mechanically_unverifiable: zero-steps recipe — the LLM produced no "
            "read-only steps (claim predicate likely requires mutation or secret access)"
        )

    return {
        "falsification_question": fq.strip(),
        "recipe": {
            "steps": redacted_steps,
            "refutes_if": _redact_credential_shapes(refutes_if.strip()),
            "supports_if": _redact_credential_shapes(supports_if.strip()),
        },
    }, None


# ---------------------------------------------------------------------------
# Adjudication
# ---------------------------------------------------------------------------

_ADJUDICATE_SYSTEM = """\
You are a grounding adjudicator for a knowledge-management system.
Given a factual claim, a verification recipe, the actual outputs of each step,
and (optionally) prior reasoning history, determine whether the claim is still
TRUE, has become FALSE, or is UNCERTAIN.

Output ONLY valid JSON (no markdown, no code fences, no prose):
{
  "verdict": "pass" | "fail" | "uncertain",
  "reasoning": "<chain-of-thought: what the outputs show, why that supports/refutes>",
  "evidence": "<the key output lines that drove the verdict>"
}

Rules:
- "pass"    = outputs clearly confirm the claim is still true.
- "fail"    = outputs clearly contradict the claim (it has drifted).
- "uncertain" = outputs are ambiguous, inconclusive, or the steps errored.
- If steps is empty or all steps failed, use "uncertain".
- Base your verdict on the ACTUAL step outputs, not on prior reasoning alone.
- Prior history gives context for how the claim has changed over time; use it
  to refine your chain-of-thought, never to bypass the actual outputs.
- Be concise but precise in reasoning (1-4 sentences is enough).
"""

_ADJUDICATE_MAX_TOKENS = 4096  # fallback-only; primary source is grounding_config


def adjudicate(
    claim: str,
    recipe: dict,
    step_outputs: list[str],
    prior_history: list[dict],
    llm: Any,
    model: Optional[str] = None,
) -> dict:
    """Render a grounding verdict by reading the actual step outputs with judgment.

    Args:
        claim:        The full text of the insight or principle being re-grounded.
        recipe:       The structured recipe dict {steps, refutes_if, supports_if}.
        step_outputs: List of verbatim output strings from each step (parallel to
                      recipe['steps']). Empty string if a step failed/timed-out.
        prior_history: List of dicts from grounding_history rows (reasoning+evidence
                       for this claim). Re-fed so chain-of-thought accumulates.
        llm:          An anthropic.Anthropic client. If None, returns 'uncertain'.
        model:        Override model name. None = use cfg.model (allows escalation).

    Returns:
        Dict {verdict: 'pass'|'fail'|'uncertain', reasoning: str, evidence: str}.
        Always returns a dict (fail-open to 'uncertain' on any error).
    """
    _UNCERTAIN = {
        "verdict": "uncertain",
        "reasoning": "LLM adjudication unavailable — client not initialised.",
        "evidence": "",
    }

    if llm is None:
        return _UNCERTAIN

    import grounding_config
    cfg = grounding_config.get_config()
    active_model = model or cfg.model

    result = _adjudicate_once(claim, recipe, step_outputs, prior_history,
                              llm, active_model, cfg)

    # Escalation: if the primary model returned "uncertain" and escalation is
    # enabled/configured/not-already-a-retry, retry once with the stronger model.
    if (
        result.get("verdict") == "uncertain"
        and model is None
        and cfg.escalation_enabled
        and cfg.escalation_model
        and cfg.escalation_model != active_model
    ):
        logger.info(
            "grounding_author: adjudicate escalating %s -> %s after uncertain verdict",
            active_model, cfg.escalation_model,
        )
        result = _adjudicate_once(claim, recipe, step_outputs, prior_history,
                                  llm, cfg.escalation_model, cfg)

    return result


def _adjudicate_once(
    claim: str,
    recipe: dict,
    step_outputs: list[str],
    prior_history: list[dict],
    llm: Any,
    model: str,
    cfg: Any,
) -> dict:
    """Single adjudication attempt with a specific model. Config-driven
    max_tokens/temperature, records usage via thread-local sidecar."""
    import llm_client

    # Build the user message: claim -> recipe -> step outputs -> prior history
    steps = recipe.get("steps", [])
    outputs_block = ""
    for i, (step, out) in enumerate(zip(steps, step_outputs or [])):
        outputs_block += f"\nStep {i+1}: {step}\nOutput:\n{(out or '<no output>').strip()}\n"
    if not outputs_block:
        outputs_block = "(no steps were executed)"

    history_block = ""
    if prior_history:
        history_block = "\n\nPrior grounding history (chronological, earliest first):\n"
        for h in prior_history[-5:]:  # cap at last 5 to stay within token budget
            ts = h.get("ts", "?")
            verdict = h.get("verdict", "?")
            reasoning = (h.get("reasoning") or "").strip()
            evidence = (h.get("evidence") or "").strip()
            history_block += (
                f"\n[{ts}] verdict={verdict}\n"
                f"  reasoning: {reasoning[:300]}\n"
                f"  evidence:  {evidence[:200]}\n"
            )

    user_msg = (
        f"Claim:\n{claim.strip()}\n\n"
        f"Recipe (refutes_if: {recipe.get('refutes_if','')} | "
        f"supports_if: {recipe.get('supports_if','')})\n"
        f"\nStep outputs:\n{outputs_block}"
        f"{history_block}"
    )

    try:
        resp = llm_client.call_with_retry(
            llm,
            model=model,
            max_tokens=cfg.adjudicate_max_tokens,
            system=_ADJUDICATE_SYSTEM,
            # Newer Claude models (sonnet-5, haiku-4.5+) return 400
            # "temperature is deprecated for this model" — only send it when
            # explicitly enabled AND the model accepts it (pop-don't-clamp).
            **({"temperature": cfg.temperature} if cfg.send_temperature else {}),
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip() if resp.content else ""
        llm_client.record_usage(resp, model=model, provider=cfg.provider)
    except llm_client.TransientLLMError:
        # THE main offender fix: a transient 401/429/conn failure must NOT
        # become a verdict. Recording {"verdict":"uncertain","reasoning":"LLM
        # call failed: <exc>"} here re-flags the claim, so the queue can never
        # drain. Propagate so drain_one_job requeues the job (attempt++) with
        # NO grounding_history verdict row written.
        raise
    except Exception as exc:
        logger.warning("grounding_author: adjudicate LLM call failed: %s", exc)
        return {
            "verdict": "uncertain",
            "reasoning": f"LLM call failed: {exc}",
            "evidence": "",
        }

    return _parse_adjudication(raw)


def _parse_adjudication(raw: str) -> dict:
    """Parse the LLM adjudication JSON. Returns fail-open 'uncertain' on any error."""
    _default = {
        "verdict": "uncertain",
        "reasoning": "Could not parse LLM adjudication output.",
        "evidence": "",
    }

    try:
        obj = _extract_json_object(raw)
    except json.JSONDecodeError:
        safe_raw = _redact_credential_shapes(raw[:200])
        logger.warning("grounding_author: adjudication output not JSON: %r", safe_raw)
        return {**_default, "reasoning": f"Non-JSON output: {safe_raw}"}

    verdict = obj.get("verdict", "")
    if verdict not in ("pass", "fail", "uncertain"):
        logger.warning("grounding_author: unexpected verdict %r — treating as uncertain", verdict)
        verdict = "uncertain"

    # reasoning/evidence are persisted verbatim into grounding_history (a
    # durable, append-only, LLM-re-fed store) — redact before returning, same
    # rationale as author_recipe's falsification_question redaction above.
    return {
        "verdict": verdict,
        "reasoning": _redact_credential_shapes(str(obj.get("reasoning", "")).strip()),
        "evidence": _redact_credential_shapes(str(obj.get("evidence", "")).strip()),
    }
