import os
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
        # None = no budget. When set, the run breaks out with
        # Outcome.BUDGET_EXCEEDED before issuing an LLM call whose result
        # would push total COGS past this threshold.
        "max_cost_usd": None,
        # If set, REPLACES the framework's generic "You are an autonomous
        # agent..." boilerplate. `system_prompt_prefix` is prepended to
        # whatever follows.
        "system_prompt": "",
        "system_prompt_prefix": "",
        # Format the model is asked to PRODUCE structured output in
        # (decompose). Native tool-call arguments are always provider JSON
        # regardless. "toon" is opt-in with json fallback.
        "output_format": "json",
        # Planner/executor split. `off` keeps the single-loop behaviour;
        # `auto` runs the planner only on tasks long enough to benefit;
        # `force` always runs it.
        "planner": {
            "enabled": False,
            "mode": "off",
            "max_items": 8,
            "auto_token_threshold": 80,
        },
        # Independent budget for spawned sub-agents. Each non-None field
        # overrides agent.max_steps / agent.max_cost_usd for children only,
        # so a fan-out doesn't let every child loop on the parent's inflated
        # budget. Both None: children inherit the parent's agent.* values.
        "subagent": {
            "max_steps": None,
            "max_cost_usd": None,
        },
    },
    "llm": {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "max_tokens": 1024,
        "api_key_env": "ANTHROPIC_API_KEY",
        # Opt-in extended prompt caching. When true, marks the last
        # message's tail block with cache_control so the history prefix
        # reads from Anthropic's 5-min ephemeral cache on the next turn
        # (~0.1x input bill on the cached prefix).
        "cache_messages": False,
        # Cheap-model backend used to generate short user-facing status
        # updates between steps -- "Reading config.py", "Spawned 2 sub-agents",
        # etc. Emitted as `narration` trace events alongside step machinery so
        # a host UI can stream them. Defaults to Haiku because at <100 tokens
        # per update it's effectively free; set to None (yaml: `null`) to
        # silence narration. An OpenAI provider can override with e.g.
        # `gpt-4o-mini`. The narrator never participates in the agent's
        # decisions -- it only describes what just happened.
        "narrator_model": "claude-haiku-4-5",
        # Max tokens for one narration string. Kept tiny on purpose -- the
        # whole point is short status lines, not paragraphs.
        "narrator_max_tokens": 60,
    },
    "tools": {
        "manifest_dir": str(DEFAULT_TOOLS_DIR),
        "enabled": None,
        "sandbox_root": ".",
        "agents": [],
        # How tool results are serialized INTO the model's context. "toon"
        # saves input tokens; the framework encodes this end so there's no
        # model reliability risk. "json" to opt out.
        "result_format": "toon",
        "decompose": {"enabled": False, "max_subquestions": DEFAULT_MAX_SUBQUESTIONS},
        # Narrow the system prompt + provider tool list to a task-relevant
        # subset via cosine similarity against each tool's description.
        # `always_include` lists tools the orchestrator prompt assumes exist
        # regardless of how the task is worded.
        "retrieval": {
            "enabled": False,
            "top_k": 6,
            "always_include": ["spawn_subagent", "ask_user", "decompose"],
        },
        # Each entry is connected at agent-build time and its tools are
        # wrapped as fabri tools named `mcp_<server>_<remote_tool>`.
        # Connection failures are logged and skipped, not fatal.
        "mcp_servers": [],
    },
    "memory": {
        # "qdrant" (networked) or "sqlite" (in-process, file-backed, no
        # docker). The two are interchangeable from the agent's perspective.
        "backend": "qdrant",
        "collection": COLLECTION_NAME,
        "qdrant_url": "http://localhost:6333",
        "sqlite_path": ".fabri/memory.db",
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
            # Refuse a scalar overriding a dict — silently dropping the
            # subtree surfaces as an opaque KeyError several layers deeper.
            if not isinstance(value, dict):
                raise ConfigError(
                    f"config key {here!r} must be a mapping (got {type(value).__name__}); "
                    f"this overrides a default that is itself a mapping."
                )
            merged[key] = _deep_merge(base_val, value, path=here)
        else:
            merged[key] = value
    return merged


def _apply_env_overrides(cfg: dict) -> dict:
    """`QDRANT_URL` env var, if set, overrides `memory.qdrant_url` so a
    container host can propagate the in-cluster address (`qdrant:6333`)
    across the subprocess boundary without rewriting each on-disk yaml.
    Never mutates the shared DEFAULT_CONFIG."""
    url = os.environ.get("QDRANT_URL")
    if not url:
        return cfg
    mem = dict(cfg.get("memory") or {})
    if mem.get("qdrant_url") == url:
        return cfg
    mem["qdrant_url"] = url
    return {**cfg, "memory": mem}


def load_config(path: str | None) -> dict:
    """Load an agent.yaml config, merged on top of DEFAULT_CONFIG so omitted
    fields fall back to today's hardcoded behavior unchanged. `path=None`
    returns the framework defaults as-is -- the same shape a consuming
    project's own agent.yaml would produce, so callers don't special-case it.
    A `QDRANT_URL` env var, if set, overrides `memory.qdrant_url` (see
    `_apply_env_overrides`). Raises ConfigError on missing file, malformed YAML,
    or a shape mismatch."""
    if path is None:
        return _apply_env_overrides(DEFAULT_CONFIG)
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
    return _apply_env_overrides(_deep_merge(DEFAULT_CONFIG, user_config))
