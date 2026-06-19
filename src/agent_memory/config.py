from pathlib import Path

import yaml

from agent_memory.core.decompose import DEFAULT_MAX_SUBQUESTIONS
from agent_memory.memory.compress import DEFAULT_MAX_TOKENS
from agent_memory.memory.pruning import PROMOTION_THRESHOLD_SESSIONS, SIMILARITY_THRESHOLD
from agent_memory.memory.store import COLLECTION_NAME
from agent_memory.orchestrator.retrieval import DEFAULT_TOP_K

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


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str | None) -> dict:
    """Load an agent.yaml config, merged on top of DEFAULT_CONFIG so omitted
    fields fall back to today's hardcoded behavior unchanged. `path=None`
    returns the framework defaults as-is -- the same shape a consuming
    project's own agent.yaml would produce, so callers don't special-case it."""
    if path is None:
        return DEFAULT_CONFIG
    with open(path) as f:
        user_config = yaml.safe_load(f) or {}
    return _deep_merge(DEFAULT_CONFIG, user_config)
