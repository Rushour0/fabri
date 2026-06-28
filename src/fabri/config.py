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
        # O1: structured / typed output. When `response_schema` is a JSON
        # Schema (dict), the final answer is parsed as JSON and validated
        # against it; a mismatch re-prompts the model with the validation
        # errors up to `response_retries` times. After that, `error_strategy`
        # decides: "strict" fails the run (Outcome.INVALID_OUTPUT), "warn"
        # returns the unvalidated text as success, "fallback" returns
        # `response_fallback` (or {}) as success. None disables the whole path
        # (today's free-text behaviour, zero extra LLM calls).
        "response_schema": None,
        "response_retries": 1,
        "error_strategy": "strict",
        "response_fallback": None,
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
        # Default provider is Gemini: lowest cost + generous free tier, so a
        # fresh `fabri run` needs only a GEMINI_API_KEY. Switch to anthropic /
        # openai / openrouter per-config or per-role; all SDKs ship by default.
        "provider": "gemini",
        "model": "gemini-2.5-pro",
        "max_tokens": 1024,
        "api_key_env": "GEMINI_API_KEY",
        # Opt-in extended prompt caching. When true, marks the last
        # message's tail block with cache_control so the history prefix
        # reads from Anthropic's 5-min ephemeral cache on the next turn
        # (~0.1x input bill on the cached prefix). Anthropic-only; a no-op
        # on other providers.
        "cache_messages": False,
        # Per-role overrides. Each entry may be:
        #   - null / absent  -> the role inherits provider/model/api_key_env
        #                       from the parent llm.* defaults
        #   - a string       -> just a model id; provider+api_key_env inherit
        #   - a dict         -> any subset of {provider, model, api_key_env,
        #                        max_tokens, base_url, cache_messages}
        # `_normalize_llm_roles` resolves these into a fully-merged dict per
        # role before any downstream code (runtime.build_role_llm, cli
        # pre-flight, etc.) reads them.
        "decompose": None,
        "planner": None,
        # Narrator emits short user-facing status updates between tool steps.
        # Defaults to Gemini Flash-Lite because <100 tokens per update is
        # effectively free; set this dict to None to silence narration entirely.
        "narrator": {"model": "gemini-2.5-flash-lite", "max_tokens": 60},
        # Legacy flat keys -- still honored for backward compatibility.
        # `_normalize_llm_roles` lifts them into the corresponding role
        # dict above when the role dict is absent. Prefer the dict form in
        # new configs; these continue to work indefinitely.
        "decompose_model": None,
        "narrator_model": None,
        "narrator_max_tokens": None,
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
        # M1: when true, every run (any outcome) also writes one deterministic
        # whole-run postmortem to memory — task + outcome + retry/cost signal —
        # retrieved by task similarity so a similar future task sees "last time
        # this took N retries; tool X failed K times". Off keeps today's
        # failure/success-only mining (and unchanged entry counts).
        "record_postmortems": False,
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


# Roles whose backend is resolved from llm.<role>; "main" maps to the parent
# llm.* keys themselves rather than a nested entry.
_LLM_ROLES = ("decompose", "planner", "narrator")

# Per-role default api_key_env when the role's provider differs from the
# parent llm.provider. Lets a user write `narrator: {provider: openai}`
# without also having to spell out `api_key_env: OPENAI_API_KEY`.
_PROVIDER_DEFAULT_API_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


def _normalize_llm_roles(cfg: dict) -> dict:
    """Resolve `llm.<role>` and legacy flat keys into a single normalized
    `llm.roles` dict shaped {decompose|planner|narrator: <full role cfg>|None}.

    A role's full config is `{provider, model, api_key_env, max_tokens,
    base_url, cache_messages}`. Missing keys inherit from the parent `llm.*`
    defaults. A role entry that is None means the role is disabled (no
    backend built; the agent loop falls back to main for decompose/planner,
    and silences narration for narrator).

    Legacy flat keys (`decompose_model`, `narrator_model`,
    `narrator_max_tokens`) are lifted ONLY when the corresponding role dict
    is absent, so a user who has both the legacy and new shape gets the new
    shape (clean incremental migration).
    """
    llm = dict(cfg.get("llm") or {})
    parent_provider = (llm.get("provider") or "gemini").lower()
    parent_defaults = {
        "provider": parent_provider,
        "model": llm.get("model"),
        "api_key_env": llm.get("api_key_env") or _PROVIDER_DEFAULT_API_KEY_ENV.get(parent_provider),
        "max_tokens": llm.get("max_tokens"),
        "cache_messages": bool(llm.get("cache_messages", False)),
        "base_url": llm.get("base_url"),
    }

    # Lift legacy flat keys into a synthetic role override, only when the
    # matching role dict isn't already set. Lift wins nothing if both exist.
    legacy_map = {
        "decompose": llm.get("decompose_model"),
        "narrator": llm.get("narrator_model"),
    }
    for role, legacy_model in legacy_map.items():
        if legacy_model and llm.get(role) is None:
            llm[role] = {"model": legacy_model}
    if (llm.get("narrator") is not None
            and isinstance(llm.get("narrator"), dict)
            and llm["narrator"].get("max_tokens") is None
            and llm.get("narrator_max_tokens") is not None):
        llm["narrator"] = {**llm["narrator"], "max_tokens": llm["narrator_max_tokens"]}

    roles: dict[str, dict | None] = {}
    for role in _LLM_ROLES:
        raw = llm.get(role)
        if raw is None:
            roles[role] = None
            continue
        if isinstance(raw, str):
            raw = {"model": raw}
        if not isinstance(raw, dict):
            raise ConfigError(
                f"config key llm.{role} must be a mapping, a model-id string, "
                f"or null (got {type(raw).__name__})."
            )
        provider = (raw.get("provider") or parent_defaults["provider"]).lower()
        # When the role overrides the provider but not the api_key_env, pick
        # the provider's conventional env var instead of leaking the parent's.
        api_key_env = raw.get("api_key_env")
        if api_key_env is None:
            api_key_env = (
                parent_defaults["api_key_env"]
                if provider == parent_defaults["provider"]
                else _PROVIDER_DEFAULT_API_KEY_ENV.get(provider)
            )
        roles[role] = {
            "provider": provider,
            "model": raw.get("model") or parent_defaults["model"],
            "api_key_env": api_key_env,
            "max_tokens": raw.get("max_tokens") or parent_defaults["max_tokens"],
            "cache_messages": bool(raw.get("cache_messages", parent_defaults["cache_messages"])),
            "base_url": raw.get("base_url") or parent_defaults["base_url"],
        }

    # `main` is always present; it's the parent llm.* defaults verbatim.
    roles["main"] = parent_defaults
    llm["roles"] = roles
    return {**cfg, "llm": llm}


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
        return _apply_env_overrides(_normalize_llm_roles(DEFAULT_CONFIG))
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
    return _apply_env_overrides(_normalize_llm_roles(_deep_merge(DEFAULT_CONFIG, user_config)))
