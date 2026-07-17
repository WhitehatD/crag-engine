# coding: utf-8
"""Grounding LLM configuration — single source of truth (migration-free;
Phase 1b / insight #3339).

MANDATE: nothing about the grounding LLM (provider, model, tokens,
temperature, timeouts, retries, concurrency, budget) is hardcoded in Python.
Every parameter lives in `db/stack.toml` under `[grounding]` /
`[grounding.llm]` / `[grounding.budget]`, and every parameter can be
overridden by an environment variable at process start (env always wins).
Deleting stack.toml entirely must not break the daemon — every key falls
back to the default baked into `_DEFAULTS` below, which mirrors the values
that were hardcoded across llm_client.py / grounding_author.py /
grounding_resolve.py / grounding_queue_v2.py / engine_daemon.py before this
module existed.

House style: pure module, no I/O beyond reading stack.toml once. The config
is loaded lazily and cached (`get_config()`); call `reload_config()` to force
a re-read (tests only — the daemon does not hot-reload mid-process, a
restart is required to pick up a stack.toml edit, same as every other daemon
constant today).
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_THIS_DIR = Path(__file__).resolve().parent
_STACK_TOML = Path(os.environ.get("CRAG_ENGINE_STACK_TOML", str(_THIS_DIR / "stack.toml")))

# Mirrors the values hardcoded in the pre-Phase-1b modules — the fallback
# path if stack.toml is missing/unreadable/malformed.
_DEFAULTS: dict = {
    "grounding": {
        "max_attempts": 3,
        "worker_concurrency": 2,
        "poll_interval_sec": 5,
        "sweep_interval_sec": 60,
        "sweep_batch": 10,
        # Minimum seconds between successive LLM-calling drains (global pace on
        # the anthropic-oauth path so the loop can't burst-429 the shared
        # Claude subscription the interactive session uses).
        # 0 = no spacing.
        "min_call_interval_sec": 8,
    },
    "grounding.llm": {
        "provider": "anthropic-oauth",
        "model": "claude-haiku-4-5-20251001",
        "escalation_model": "claude-sonnet-5",
        "escalation_enabled": True,
        "base_url": "https://api.anthropic.com",
        "auth_source": "oauth-credentials",
        "author_max_tokens": 4096,
        "adjudicate_max_tokens": 4096,
        "correction_max_tokens": 4096,
        "temperature": 0.0,
        "send_temperature": False,
        "timeout_sec": 30,
    },
    "grounding.budget": {
        "daily_budget_calls": 500,
        "daily_budget_tokens": 2_000_000,
        "pause_on_budget": True,
    },
    # Grounding v3 claim-layer feature flags.
    "claims": {
        # Master switch for the claim pipeline (decompose/verify). Off by default
        # until the operator runs the backfill; the daemon is safe either way.
        "enabled": True,
        # v3 claim-level contradiction detector. When on, the old insight-level
        # detector is disabled (kept behind the flag one release for rollback).
        "claim_contradiction_enabled": False,
        # Assertion-antipodality thresholds for the claim-level detector.
        "contradiction_cosine": 0.80,
        # Worker concurrency for claim verify jobs (defaults to grounding value).
        "claim_worker_concurrency": 2,
    },
}

# Env var name -> (toml_section, toml_key, caster). Env always wins over
# stack.toml. Names preserve the pre-existing CRAG_ENGINE_GROUNDING_MODEL /
# CRAG_ENGINE_LLM_BASE_URL / CRAG_ENGINE_GROUNDING_WORKER_CONCURRENCY / _WORKER_SLEEP
# spellings so nothing that already sets those env vars (tests, ops scripts)
# breaks.
def _bool(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


_ENV_OVERRIDES: list[tuple[str, str, str, type]] = [
    ("CRAG_ENGINE_GROUNDING_MAX_ATTEMPTS", "grounding", "max_attempts", int),
    ("CRAG_ENGINE_GROUNDING_WORKER_CONCURRENCY", "grounding", "worker_concurrency", int),
    ("CRAG_ENGINE_GROUNDING_WORKER_SLEEP", "grounding", "poll_interval_sec", float),
    ("CRAG_ENGINE_GROUNDING_SWEEP_INTERVAL", "grounding", "sweep_interval_sec", float),
    ("CRAG_ENGINE_GROUNDING_SWEEP_BATCH", "grounding", "sweep_batch", int),
    ("CRAG_ENGINE_GROUNDING_MIN_CALL_INTERVAL", "grounding", "min_call_interval_sec", float),
    ("CRAG_ENGINE_GROUNDING_PROVIDER", "grounding.llm", "provider", str),
    ("CRAG_ENGINE_GROUNDING_MODEL", "grounding.llm", "model", str),
    ("CRAG_ENGINE_GROUNDING_ESCALATION_MODEL", "grounding.llm", "escalation_model", str),
    ("CRAG_ENGINE_GROUNDING_ESCALATION_ENABLED", "grounding.llm", "escalation_enabled", _bool),
    ("CRAG_ENGINE_LLM_BASE_URL", "grounding.llm", "base_url", str),
    ("CRAG_ENGINE_GROUNDING_AUTH_SOURCE", "grounding.llm", "auth_source", str),
    ("CRAG_ENGINE_GROUNDING_AUTHOR_MAX_TOKENS", "grounding.llm", "author_max_tokens", int),
    ("CRAG_ENGINE_GROUNDING_ADJUDICATE_MAX_TOKENS", "grounding.llm", "adjudicate_max_tokens", int),
    ("CRAG_ENGINE_GROUNDING_CORRECTION_MAX_TOKENS", "grounding.llm", "correction_max_tokens", int),
    ("CRAG_ENGINE_GROUNDING_TEMPERATURE", "grounding.llm", "temperature", float),
    ("CRAG_ENGINE_GROUNDING_SEND_TEMPERATURE", "grounding.llm", "send_temperature", _bool),
    ("CRAG_ENGINE_GROUNDING_TIMEOUT_SEC", "grounding.llm", "timeout_sec", float),
    ("CRAG_ENGINE_GROUNDING_DAILY_BUDGET_CALLS", "grounding.budget", "daily_budget_calls", int),
    ("CRAG_ENGINE_GROUNDING_DAILY_BUDGET_TOKENS", "grounding.budget", "daily_budget_tokens", int),
    ("CRAG_ENGINE_GROUNDING_PAUSE_ON_BUDGET", "grounding.budget", "pause_on_budget", _bool),
]

# Backward-compat: CRAG_ENGINE_CONTRA_BASE_URL was the original name before
# CRAG_ENGINE_LLM_BASE_URL existed (see llm_client.py history). Honored only if
# CRAG_ENGINE_LLM_BASE_URL itself is unset.
_LEGACY_BASE_URL_ENV = "CRAG_ENGINE_CONTRA_BASE_URL"


@dataclass(frozen=True)
class GroundingLLMConfig:
    # [grounding]
    max_attempts: int
    worker_concurrency: int
    poll_interval_sec: float
    sweep_interval_sec: float
    sweep_batch: int
    min_call_interval_sec: float
    # [grounding.llm]
    provider: str
    model: str
    escalation_model: str
    escalation_enabled: bool
    base_url: str
    auth_source: str
    author_max_tokens: int
    adjudicate_max_tokens: int
    correction_max_tokens: int
    temperature: float
    send_temperature: bool
    timeout_sec: float
    # [grounding.budget]
    daily_budget_calls: int
    daily_budget_tokens: int
    pause_on_budget: bool

    def sampling_kwargs(self) -> dict:
        """Sampling params to pass to messages.create(). Modern Anthropic
        models DEPRECATE `temperature` (a 400 'temperature is deprecated for
        this model' error), so it is omitted unless `send_temperature=true`
        (valid for openai / ollama providers where the caller opts in)."""
        return {"temperature": self.temperature} if self.send_temperature else {}


def _load_toml() -> dict:
    if not _STACK_TOML.exists():
        return {}
    try:
        with _STACK_TOML.open("rb") as f:
            return tomllib.load(f)
    except Exception:
        # Malformed stack.toml must not crash the daemon — fall back to
        # defaults + env overrides, same as a missing file.
        return {}


def _section(doc: dict, dotted: str) -> dict:
    node = doc
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return {}
        node = node.get(part, {})
    return node if isinstance(node, dict) else {}


def _build_config() -> GroundingLLMConfig:
    doc = _load_toml()

    merged: dict = {}
    for section, defaults in _DEFAULTS.items():
        toml_section = _section(doc, section)
        merged[section] = {**defaults, **toml_section}

    for env_name, section, key, caster in _ENV_OVERRIDES:
        raw = os.environ.get(env_name)
        if raw is not None and raw != "":
            try:
                merged[section][key] = caster(raw)
            except Exception:
                pass  # keep toml/default value on a malformed env var

    # Legacy base_url env fallback (only if the canonical name is unset).
    if os.environ.get("CRAG_ENGINE_LLM_BASE_URL") is None:
        legacy = os.environ.get(_LEGACY_BASE_URL_ENV)
        if legacy:
            merged["grounding.llm"]["base_url"] = legacy

    g = merged["grounding"]
    llm = merged["grounding.llm"]
    budget = merged["grounding.budget"]

    return GroundingLLMConfig(
        max_attempts=int(g["max_attempts"]),
        worker_concurrency=int(g["worker_concurrency"]),
        poll_interval_sec=float(g["poll_interval_sec"]),
        sweep_interval_sec=float(g["sweep_interval_sec"]),
        sweep_batch=int(g["sweep_batch"]),
        min_call_interval_sec=float(g.get("min_call_interval_sec", 0) or 0),
        provider=str(llm["provider"]),
        model=str(llm["model"]),
        escalation_model=str(llm["escalation_model"]),
        escalation_enabled=bool(llm["escalation_enabled"]),
        base_url=str(llm["base_url"]),
        auth_source=str(llm["auth_source"]),
        author_max_tokens=int(llm["author_max_tokens"]),
        adjudicate_max_tokens=int(llm["adjudicate_max_tokens"]),
        correction_max_tokens=int(llm["correction_max_tokens"]),
        temperature=float(llm["temperature"]),
        send_temperature=bool(llm["send_temperature"]),
        timeout_sec=float(llm["timeout_sec"]),
        daily_budget_calls=int(budget["daily_budget_calls"]),
        daily_budget_tokens=int(budget["daily_budget_tokens"]),
        pause_on_budget=bool(budget["pause_on_budget"]),
    )


_cached: Optional[GroundingLLMConfig] = None


def get_config() -> GroundingLLMConfig:
    """Return the cached grounding LLM config, building it on first call."""
    global _cached
    if _cached is None:
        _cached = _build_config()
    return _cached


def reload_config() -> GroundingLLMConfig:
    """Force a re-read of stack.toml + env. Tests only — the daemon does not
    hot-reload config mid-process (restart to pick up a stack.toml edit)."""
    global _cached
    _cached = _build_config()
    return _cached


def get_claims_config() -> dict:
    """Grounding v3 [claims] flags merged over _DEFAULTS['claims'], with env
    overrides for the two switches operators flip most:
      CRAG_ENGINE_CLAIMS_ENABLED (1/0)
      CRAG_ENGINE_CLAIM_CONTRADICTION_ENABLED (1/0)
    Returns a plain dict (not a dataclass) — this section is small + advisory."""
    doc = _load_toml()
    merged = {**_DEFAULTS["claims"], **_section(doc, "claims")}
    for env_name, key in (
        ("CRAG_ENGINE_CLAIMS_ENABLED", "enabled"),
        ("CRAG_ENGINE_CLAIM_CONTRADICTION_ENABLED", "claim_contradiction_enabled"),
    ):
        raw = os.environ.get(env_name)
        if raw is not None and raw != "":
            merged[key] = raw not in ("0", "false", "False", "no")
    return merged
