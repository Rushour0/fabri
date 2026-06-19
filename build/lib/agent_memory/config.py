from pathlib import Path

import yaml

from agent_memory.core.decompose import DEFAULT_MAX_SUBQUESTIONS
from agent_memory.memory.compress import DEFAULT_MAX_TOKENS
from agent_memory.memory.pruning import PROMOTION_THRESHOLD_SESSIONS, SIMILARITY_THRESHOLD
from agent_memory.memory.store import COLLECTION_NAME
from agent_memory.orchestrator.retrieval import DEFAULT_TOP_K

DEFAULT_TOOLS_DIR = Path(__file__).resolve().parent / "tools" / "examples"

DEFAULT_CONFIG = {
    "agent": {"name": "default", "max_steps": 10, "system_prompt_prefix": ""},
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
