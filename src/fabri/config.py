from pathlib import Path

import yaml

from fabri.core.decompose import DEFAULT_MAX_SUBQUESTIONS
from fabri.memory.compress import DEFAULT_MAX_TOKENS
from fabri.memory.pruning import PROMOTION_THRESHOLD_SESSIONS, SIMILARITY_THRESHOLD
from fabri.memory.store import COLLECTION_NAME
from fabri.orchestrator.retrieval import DEFAULT_TOP_K

DEFAULT_TOOLS_DIR = Path(__file__).resolve().parent / "tools" / "examples"

DEFAULT_CONFIG = {
    "agent": {
        "name": "default",
        "max_steps": 10,
        # If `system_prompt` is set, it REPLACES the framework's generic
        # boilerplate ("You are an autonomous agent..."). If `system_prompt_prefix`
        # is set, it is prepended to whatever follows. Both empty = original
        # behavior. Consuming projects use these to inject domain-specific
        # identity, format contracts, few-shots, etc.
        "system_prompt": "",
        "system_prompt_prefix": "",
        # Format the model is asked to PRODUCE structured output in (decompose).
        # "json" is the reliable default; "toon" is opt-in (always json-fallback).
        # Native tool-call arguments are always provider JSON regardless.
        "output_format": "json",
        # A2: planner/executor split. `off` (default) keeps the historical
        # single-loop behaviour. `auto` runs the planner only on tasks long
        # enough to benefit. `force` always runs it. `model` overrides which
        # LLM does the plan call; falls back to llm.decompose_model, then
        # the main llm.
        "planner": {
            "enabled": False,
            "mode": "off",
            "max_items": 8,
            "auto_token_threshold": 80,
        },
    },
    "llm": {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "max_tokens": 1024,
        "api_key_env": "ANTHROPIC_API_KEY",
    },
    "tools": {
        "manifest_dir": str(DEFAULT_TOOLS_DIR),
        "enabled": None,
        "sandbox_root": ".",
        "agents": [],  # other agent.yaml configs exposed as tools -- see tools/agent_tool.py
        # How tool results are serialized INTO the model's context. "toon" (default)
        # saves input tokens; the framework encodes this end, so there's no model
        # reliability risk. Set "json" to opt out.
        "result_format": "toon",
        "decompose": {"enabled": False, "max_subquestions": DEFAULT_MAX_SUBQUESTIONS},
        # A1: narrow the system prompt + provider tool list to a task-relevant
        # subset via cosine similarity against each tool's description. Default
        # off for back-compat. `always_include` lists tools the orchestrator
        # prompt assumes exist regardless of how the task is worded
        # (`spawn_subagent`, `ask_user`, `decompose`).
        "retrieval": {
            "enabled": False,
            "top_k": 6,
            "always_include": ["spawn_subagent", "ask_user", "decompose"],
        },
    },
    "memory": {
        "collection": COLLECTION_NAME,
        "qdrant_url": "http://localhost:6333",
        "top_k": DEFAULT_TOP_K,
        "similarity_threshold": SIMILARITY_THRESHOLD,
        "promotion_threshold_sessions": PROMOTION_THRESHOLD_SESSIONS,
        "guideline_max_tokens": DEFAULT_MAX_TOKENS,
    },
}


class ConfigError(ValueError):
    """Raised when an agent.yaml is missing, malformed, or overrides a section
    with the wrong shape. cli.py catches this and prints a clean stderr
    message + exit 1, rather than letting the raw yaml/KeyError traceback out."""


def _deep_merge(base: dict, override: dict, *, path: str = "") -> dict:
    merged = dict(base)
    for key, value in override.items():
        here = f"{path}.{key}" if path else key
        base_val = merged.get(key)
        if isinstance(base_val, dict):
            # Refuse to silently drop a whole subtree because the user wrote a
            # scalar where a dict belongs. The pre-fix behavior overwrote and
            # surfaced as a KeyError several layers deeper, which is opaque.
            if not isinstance(value, dict):
                raise ConfigError(
                    f"config key {here!r} must be a mapping (got {type(value).__name__}); "
                    f"this overrides a default that is itself a mapping."
                )
            merged[key] = _deep_merge(base_val, value, path=here)
        else:
            merged[key] = value
    return merged


def load_config(path: str | None) -> dict:
    """Load an agent.yaml config, merged on top of DEFAULT_CONFIG so omitted
    fields fall back to today's hardcoded behavior unchanged. `path=None`
    returns the framework defaults as-is -- the same shape a consuming
    project's own agent.yaml would produce, so callers don't special-case it.
    Raises ConfigError on missing file, malformed YAML, or a shape mismatch."""
    if path is None:
        return DEFAULT_CONFIG
    try:
        with open(path) as f:
            user_config = yaml.safe_load(f) or {}
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {path}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"malformed YAML in {path}: {e}") from e
    if not isinstance(user_config, dict):
        raise ConfigError(
            f"top-level of {path} must be a mapping (got {type(user_config).__name__})."
        )
    return _deep_merge(DEFAULT_CONFIG, user_config)
