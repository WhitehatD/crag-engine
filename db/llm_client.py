# coding: utf-8
"""Provider-agnostic LLM client factory for in-daemon calls (Grounding v2 +
Contradiction). Phase 1b (insight #3339): the daemon can now source grounding
LLM calls from any of 4 providers, selected entirely by `db/stack.toml`
(or the matching env var) — zero code changes to swap providers.

Authentication architecture (default provider: anthropic-oauth)
-----------------------------------------------------------------
The crag engine daemon does NOT use ANTHROPIC_API_KEY directly by default. Claude
Code authenticates via OAuth against Claude.ai consumer accounts
(token cached at `~/.claude/.credentials.json` under
`.claudeAiOauth.accessToken`). Anthropic accepts that bearer via the
`anthropic-beta: oauth-2025-04-20` header (handled by the Anthropic SDK when
`auth_token=` is passed instead of `api_key=`). This means grounding calls
bill against the CLAUDE SUBSCRIPTION WEEKLY LIMIT, not a metered API key —
see `db/grounding_cost.py` for the budget guard this motivated.

Provider factory
----------------
`get_client()` reads `grounding_config.get_config().provider` and returns a
client shaped like `client.messages.create(model=, max_tokens=, system=,
messages=) -> resp` where `resp.content[0].text` is the completion text and
`resp.usage.{input,output}_tokens` is the token count — the Anthropic SDK's
native shape. Non-Anthropic providers (openai, ollama-local) are wrapped in
`_OpenAICompatClient`, a thin adapter presenting that same interface, so
every call site in grounding_author.py / grounding_resolve.py is provider-
agnostic and never branches on provider type.

  anthropic-oauth  -> Anthropic SDK, auth_token from ~/.claude/.credentials.json,
                      base_url = config base_url (api.anthropic.com by default).
  anthropic-api    -> Anthropic SDK, api_key from $ANTHROPIC_API_KEY.
  openai           -> openai SDK, api_key from $OPENAI_API_KEY, wrapped in
                      _OpenAICompatClient.
  ollama-local     -> openai SDK pointed at config base_url (Ollama's
                      OpenAI-compatible /v1 endpoint), no real API key
                      needed, wrapped in _OpenAICompatClient.

Fail-open: returns None on any error (missing package, missing/expired
token, unknown provider, proxy unreachable). Callers must handle None
gracefully — this has been true since the module's introduction and every
grounding call site already does (see grounding_author.author_recipe /
adjudicate, grounding_resolve.draft_correction).

House style: pure functions take sqlite3.Connection / client as args, never
open/close connections themselves (see db/lifecycle.py docstring).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

import grounding_config

logger = logging.getLogger("crag-engine")

try:
    import anthropic as _anthropic_mod
    _HAVE_ANTHROPIC = True
except ImportError:
    _HAVE_ANTHROPIC = False


# ---------------------------------------------------------------------------
# Transient-failure signalling (2026-07-16 resilience fix)
# ---------------------------------------------------------------------------
# The grounding loop re-verifies memory claims by calling an LLM (Haiku via
# anthropic-oauth by default). Two
# transient transport failures churn that loop forever if they leak into a
# call site as a normal exception:
#
#   401 authentication_error  — the Claude Code OAuth token (read fresh per
#     call from ~/.claude/.credentials.json) is mid-refresh / momentarily
#     invalid.
#   429 rate_limit_error      — Headroom's shared TokenBucketRateLimiter
#     throttles the background grounding burst against the same bucket the
#     interactive session uses.
#
# Pre-fix, the grounding call sites caught these as generic exceptions and
# RECORDED THEM AS A VERDICT ({"verdict":"uncertain","reasoning":"LLM call
# failed: <exc>"}). That terminal verdict re-flagged the claim, so the queue
# could never drain. A transient 401/429 must NOT become a verdict.
#
# `call_with_retry` wraps `client.messages.create(...)` with bounded
# retry/backoff and, on EXHAUSTION, raises the DISTINCT `TransientLLMError`
# (not a generic Exception) so call sites can tell "transient failure,
# requeue the job" apart from "the LLM answered UNCERTAIN". Call sites re-
# raise TransientLLMError past their broad `except Exception` handlers; the
# worker (drain_one_job) leaves the job PENDING (attempt++, honouring
# grounding.max_attempts) instead of writing a terminal row.


class TransientLLMError(Exception):
    """Raised by call_with_retry when a transient transport failure (401 auth
    mid-refresh, 429 rate-limit, connection/timeout) survived all retries.

    Distinct from a generic Exception ON PURPOSE: the grounding worker treats
    it as a REQUEUE signal (leave the job pending, attempt++), never as an
    'uncertain' verdict. `status_code` is the HTTP status when known (401/429),
    else None."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def _retry_after_seconds(exc: Any, default: float) -> float:
    """Best-effort parse of a Retry-After header off an anthropic error's
    response. Returns `default` when absent/unparseable."""
    try:
        resp = getattr(exc, "response", None)
        headers = getattr(resp, "headers", None)
        if headers:
            val = headers.get("retry-after") or headers.get("Retry-After")
            if val:
                return max(0.0, float(val))
    except Exception:
        pass
    return default


def call_with_retry(client: Any, *, model, max_tokens, messages, **kwargs) -> Any:
    """Invoke `client.messages.create(...)` with bounded resilience against the
    two transient failures that churn the grounding loop, plus connection/
    timeout blips. Returns the response on success; raises TransientLLMError
    when retries are exhausted.

    Retry policy (only these classes are retried — every other exception,
    including a genuine 400/malformed request, propagates unchanged so callers
    still see real errors):

      401 AuthenticationError  — the OAuth token was mid-refresh. Invalidate
        the cached oauth client so the NEXT attempt re-reads the token via
        _read_oauth_token(), sleep briefly, retry ONCE.
      429 RateLimitError       — exponential backoff (2s, 4s, 8s) up to 3
        tries, honouring a Retry-After header when present.
      APIConnectionError /     — one retry after a short sleep.
        APITimeoutError

    NOT retried here: min_call_interval pacing (that's the worker's job), the
    endpoint, the provider, or the model — all unchanged per the fix scope.
    """
    if not _HAVE_ANTHROPIC:
        # Non-anthropic providers (openai/ollama adapters) don't raise the
        # anthropic exception types; just call through. A transport failure
        # there surfaces as its own exception to the caller (unchanged
        # behaviour for those providers).
        return client.messages.create(model=model, max_tokens=max_tokens, messages=messages, **kwargs)

    _AuthErr = _anthropic_mod.AuthenticationError
    _RateErr = _anthropic_mod.RateLimitError
    _ConnErr = _anthropic_mod.APIConnectionError
    _TimeoutErr = _anthropic_mod.APITimeoutError

    max_429_tries = 3
    attempt_429 = 0
    tried_401 = False
    tried_conn = False

    while True:
        try:
            return client.messages.create(
                model=model, max_tokens=max_tokens, messages=messages, **kwargs
            )
        except _AuthErr as exc:
            # 401: token likely mid-refresh. Drop the cached client so the next
            # attempt re-reads the (possibly refreshed) token, then retry ONCE.
            if tried_401:
                raise TransientLLMError(
                    f"401 authentication_error persisted after client-refresh retry: {exc}",
                    status_code=401,
                ) from exc
            tried_401 = True
            invalidate_oauth_client()
            logger.warning(
                "llm_client: 401 auth error (token likely mid-refresh) — "
                "invalidated cached oauth client, retrying once"
            )
            time.sleep(1.0)
            # Rebuild the client so the retry uses the freshly-read token, not
            # the stale one this `client` was pinned to. Fall back to the same
            # client if the rebuild yields None (e.g. token file vanished).
            refreshed = get_client()
            if refreshed is not None:
                client = refreshed
        except _RateErr as exc:
            attempt_429 += 1
            if attempt_429 >= max_429_tries:
                raise TransientLLMError(
                    f"429 rate_limit_error after {attempt_429} tries: {exc}",
                    status_code=429,
                ) from exc
            backoff = _retry_after_seconds(exc, default=2.0 * (2 ** (attempt_429 - 1)))
            logger.warning(
                "llm_client: 429 rate_limit — backoff %.1fs (try %d/%d)",
                backoff, attempt_429, max_429_tries,
            )
            time.sleep(backoff)
        except (_ConnErr, _TimeoutErr) as exc:
            if tried_conn:
                raise TransientLLMError(
                    f"connection/timeout error persisted after retry: {exc}",
                    status_code=None,
                ) from exc
            tried_conn = True
            logger.warning("llm_client: connection/timeout error — retrying once: %s", exc)
            time.sleep(1.0)

try:
    import openai as _openai_mod
    _HAVE_OPENAI = True
except ImportError:
    _HAVE_OPENAI = False

# ---------------------------------------------------------------------------
# Config (backward-compat module attrs — every value now SOURCED from
# grounding_config.py / stack.toml, never hardcoded here. Kept as module
# attributes because grounding_author.py / grounding_resolve.py import
# `GROUNDING_MODEL` directly; new code should prefer
# `grounding_config.get_config()` instead.)
# ---------------------------------------------------------------------------

_cfg = grounding_config.get_config()

_CREDS_FILE = Path(os.environ.get(
    "CLAUDE_CREDENTIALS_FILE",
    str(Path.home() / ".claude" / ".credentials.json"),
))

PROXY_BASE_URL: str = _cfg.base_url
GROUNDING_MODEL: str = _cfg.model

# ---------------------------------------------------------------------------
# Per-call usage sidecar — thread-local so concurrent grounding workers each
# see only their own most recent call. Lets author_recipe/adjudicate/
# draft_correction report token usage + the model actually used (which may
# differ from GROUNDING_MODEL when escalation fired) back to their callers
# WITHOUT changing those functions' existing return contracts (multiple
# tests unpack `result, reason = author_recipe(...)` / `adj = adjudicate(...)`
# as fixed-shape values; adding a return element would break every one of
# them). Callers that care about cost call `get_last_usage()` immediately
# after invoking one of those functions, in the same thread, before any
# other LLM call — this is safe because drain_one_job() processes one job
# to completion per worker thread, never interleaving two LLM calls from the
# same thread.
# ---------------------------------------------------------------------------

_usage_local = threading.local()


def record_usage(resp: Any, model: Optional[str] = None, provider: Optional[str] = None) -> None:
    """Stash token usage + the model/provider actually used for the LAST LLM
    call made on this thread. Best-effort: a stub/fake client in tests may
    return a response with no `.usage` attribute — defaults to 0 tokens
    rather than raising."""
    usage = getattr(resp, "usage", None)
    _usage_local.tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
    _usage_local.tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
    _usage_local.model = model
    _usage_local.provider = provider


def get_last_usage() -> dict:
    """Return the usage sidecar recorded by the most recent record_usage()
    call on THIS thread. All fields default to falsy/None if nothing has
    been recorded yet on this thread (e.g. the LLM call failed before a
    response was received, or llm was None)."""
    return {
        "tokens_in": getattr(_usage_local, "tokens_in", 0),
        "tokens_out": getattr(_usage_local, "tokens_out", 0),
        "model": getattr(_usage_local, "model", None),
        "provider": getattr(_usage_local, "provider", None),
    }


def clear_last_usage() -> None:
    """Reset the usage sidecar for this thread. Callers that record cost per
    JOB (grounding_queue_v2.drain_one_job) call this once at the start of
    each job so that a job which makes no LLM call (e.g. an auto-verify
    resolve with no draft_correction) doesn't inherit — and double-record —
    a PRIOR job's usage left over on the same worker thread. `model=None`
    after this call is the signal callers check before recording a cost row."""
    _usage_local.tokens_in = 0
    _usage_local.tokens_out = 0
    _usage_local.model = None
    _usage_local.provider = None


def _read_oauth_token() -> Optional[str]:
    """Read the live OAuth access token from claudex's credentials cache.

    Called once per LLM request (cheap — a small JSON read) so that if
    claudex refreshes the token, the crag engine daemon picks up the new one on
    the next call without needing its own refresh logic.
    Returns None if file is missing/malformed.
    """
    try:
        with _CREDS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        token = (data.get("claudeAiOauth") or {}).get("accessToken")
        return token.strip() if token else None
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning("llm_client: failed to read OAuth token from %s: %s", _CREDS_FILE, exc)
        return None


# ---------------------------------------------------------------------------
# OpenAI-compat adapter — presents the Anthropic-shaped `.messages.create()`
# interface over an OpenAI-SDK-shaped client (real OpenAI, or any
# OpenAI-compatible endpoint such as Ollama's /v1 route), so grounding call
# sites never need to know which provider is active.
# ---------------------------------------------------------------------------

class _UsageShim:
    def __init__(self, input_tokens: int, output_tokens: int):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _ContentBlockShim:
    def __init__(self, text: str):
        self.text = text


class _AnthropicShapedResponse:
    """Wraps a plain completion string + usage counts into the shape every
    grounding call site expects: `resp.content[0].text`, `resp.usage.*`."""
    def __init__(self, text: str, input_tokens: int = 0, output_tokens: int = 0):
        self.content = [_ContentBlockShim(text)]
        self.usage = _UsageShim(input_tokens, output_tokens)


class _OpenAICompatClient:
    """Adapts an OpenAI-SDK client (real OpenAI, or Ollama's OpenAI-compat
    endpoint) to the `.messages.create(...)` interface grounding call sites
    use, so a provider swap requires zero call-site changes."""

    def __init__(self, raw_client: Any, timeout_sec: float):
        self._raw = raw_client
        self._timeout = timeout_sec
        self.messages = self  # so `client.messages.create(...)` resolves here

    def create(
        self,
        model: str,
        max_tokens: int,
        system: Optional[str] = None,
        messages: Optional[list] = None,
        temperature: float = 0.0,
        **_ignored: Any,
    ) -> _AnthropicShapedResponse:
        chat_messages = []
        if system:
            chat_messages.append({"role": "system", "content": system})
        chat_messages.extend(messages or [])

        resp = self._raw.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=chat_messages,
            timeout=self._timeout,
        )
        choice_text = ""
        if getattr(resp, "choices", None):
            msg = resp.choices[0].message
            choice_text = getattr(msg, "content", "") or ""
        usage = getattr(resp, "usage", None)
        tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "completion_tokens", 0) or 0)
        return _AnthropicShapedResponse(choice_text, tokens_in, tokens_out)


# ---------------------------------------------------------------------------
# Per-provider lazy singletons (module-level; per-process — daemon is
# single-process). Cached separately per provider so a test/CLI that swaps
# CRAG_ENGINE_GROUNDING_PROVIDER mid-process (via grounding_config.reload_config())
# doesn't reuse a stale client built for a different backend.
# ---------------------------------------------------------------------------

_clients_by_provider: dict = {}
_client_token: Optional[str] = None  # anthropic-oauth cache-invalidation key
_warned_providers: set = set()


def _warn_once(provider: str, msg: str) -> None:
    if provider not in _warned_providers:
        logger.warning(msg)
        _warned_providers.add(provider)


def invalidate_oauth_client() -> None:
    """Drop the cached anthropic-oauth client + its cache key so the NEXT
    get_client() call rebuilds it, re-reading the (possibly just-refreshed)
    OAuth token via _read_oauth_token(). Called by call_with_retry on a 401 —
    the token was likely mid-refresh, and the cached client is pinned to the
    stale token value until this clears it."""
    global _client_token
    _clients_by_provider.pop("anthropic-oauth", None)
    _client_token = None


def _get_anthropic_oauth_client(cfg: "grounding_config.GroundingLLMConfig"):
    global _client_token
    if not _HAVE_ANTHROPIC:
        _warn_once("anthropic-oauth", "llm_client: 'anthropic' package not installed — LLM features disabled")
        return None

    token = _read_oauth_token()
    if not token:
        _warn_once(
            "anthropic-oauth",
            f"llm_client: no OAuth token at {_CREDS_FILE} — LLM features disabled "
            "(run `claudex` once to authenticate)",
        )
        return None

    cached = _clients_by_provider.get("anthropic-oauth")
    if cached is not None and _client_token == token:
        return cached

    try:
        client = _anthropic_mod.Anthropic(auth_token=token, base_url=cfg.base_url)
        _clients_by_provider["anthropic-oauth"] = client
        _client_token = token
        logger.info(
            "llm_client: anthropic-oauth client initialized via proxy %s, model=%s",
            cfg.base_url, cfg.model,
        )
        return client
    except Exception as exc:
        logger.warning("llm_client: anthropic-oauth client init failed: %s", exc)
        return None


def _get_anthropic_api_client(cfg: "grounding_config.GroundingLLMConfig"):
    if not _HAVE_ANTHROPIC:
        _warn_once("anthropic-api", "llm_client: 'anthropic' package not installed — LLM features disabled")
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        _warn_once("anthropic-api", "llm_client: provider=anthropic-api but $ANTHROPIC_API_KEY is unset — LLM features disabled")
        return None

    cached = _clients_by_provider.get("anthropic-api")
    if cached is not None:
        return cached
    try:
        client = _anthropic_mod.Anthropic(api_key=api_key)
        _clients_by_provider["anthropic-api"] = client
        logger.info("llm_client: anthropic-api client initialized, model=%s", cfg.model)
        return client
    except Exception as exc:
        logger.warning("llm_client: anthropic-api client init failed: %s", exc)
        return None


def _get_openai_client(cfg: "grounding_config.GroundingLLMConfig"):
    if not _HAVE_OPENAI:
        _warn_once("openai", "llm_client: 'openai' package not installed — LLM features disabled")
        return None
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        _warn_once("openai", "llm_client: provider=openai but $OPENAI_API_KEY is unset — LLM features disabled")
        return None

    cached = _clients_by_provider.get("openai")
    if cached is not None:
        return cached
    try:
        raw = _openai_mod.OpenAI(api_key=api_key)
        client = _OpenAICompatClient(raw, cfg.timeout_sec)
        _clients_by_provider["openai"] = client
        logger.info("llm_client: openai client initialized, model=%s", cfg.model)
        return client
    except Exception as exc:
        logger.warning("llm_client: openai client init failed: %s", exc)
        return None


def _get_ollama_client(cfg: "grounding_config.GroundingLLMConfig"):
    if not _HAVE_OPENAI:
        _warn_once("ollama-local", "llm_client: 'openai' package not installed (required for ollama-local's OpenAI-compat endpoint) — LLM features disabled")
        return None

    cached = _clients_by_provider.get("ollama-local")
    if cached is not None:
        return cached
    try:
        # Ollama's OpenAI-compat endpoint ignores the API key but the SDK
        # requires a non-empty string.
        raw = _openai_mod.OpenAI(api_key="ollama-local", base_url=cfg.base_url)
        client = _OpenAICompatClient(raw, cfg.timeout_sec)
        _clients_by_provider["ollama-local"] = client
        logger.info("llm_client: ollama-local client initialized via %s, model=%s", cfg.base_url, cfg.model)
        return client
    except Exception as exc:
        logger.warning("llm_client: ollama-local client init failed: %s", exc)
        return None


_PROVIDER_FACTORIES = {
    "anthropic-oauth": _get_anthropic_oauth_client,
    "anthropic-api": _get_anthropic_api_client,
    "openai": _get_openai_client,
    "ollama-local": _get_ollama_client,
}


def get_client(provider: Optional[str] = None) -> Optional[Any]:
    """Return an LLM client wired per `db/stack.toml` (or an explicit
    override). Returns None (fail-open) if the configured provider is
    unknown, its SDK is missing, or credentials are unavailable.

    Args:
        provider: override the configured provider for this call (used by
            tests only; production always omits this and uses stack.toml).
    """
    cfg = grounding_config.get_config()
    active_provider = provider or cfg.provider

    factory = _PROVIDER_FACTORIES.get(active_provider)
    if factory is None:
        _warn_once(
            active_provider or "<empty>",
            f"llm_client: unknown provider {active_provider!r} — LLM features disabled "
            f"(valid: {', '.join(_PROVIDER_FACTORIES)})",
        )
        return None
    return factory(cfg)
