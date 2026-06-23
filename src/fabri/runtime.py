"""Shared composition helpers for turning a loaded config into the objects
run_agent() needs. Used by both cli.py and tools/agent_runner_tool.py (the
agent-as-tool adapter) so the two entry points build agents identically."""
import os
from pathlib import Path

from fabri.config import DEFAULT_TOOLS_DIR
from fabri.core.agent import DECOMPOSE_TOOL_NAME
from fabri.core.llm import AnthropicLLMBackend, OpenAILLMBackend
from fabri.memory.store import QdrantMemoryStore
from fabri.tools.agent_tool import make_agent_tool_manifest
from fabri.tools.registry import ToolRegistry


def build_memory_store(mem_cfg: dict):
    """Pick the memory backend by `memory.backend`. "qdrant" (networked,
    multi-process safe) or "sqlite" (in-process, single-file). Both expose
    the same interface so the agent loop is backend-agnostic."""
    backend = (mem_cfg.get("backend") or "qdrant").lower()
    if backend == "qdrant":
        return QdrantMemoryStore(
            url=mem_cfg["qdrant_url"], collection=mem_cfg["collection"]
        )
    if backend == "sqlite":
        # Lazy import so a qdrant-only user doesn't pay sqlite-vec's
        # extension-load cost.
        from fabri.memory.embedded_store import SqliteMemoryStore
        return SqliteMemoryStore(
            path=mem_cfg.get("sqlite_path", ".fabri/memory.db"),
            collection=mem_cfg.get("collection", "fabri"),
        )
    raise ValueError(
        f"unknown memory.backend: {backend!r} (expected 'qdrant' or 'sqlite')"
    )

# Sentinel value for `tools.manifest_dir` — resolves to the framework's
# bundled tools regardless of where fabri is installed.
BUILTIN_TOOLS_TOKENS = {"builtin", "builtin:tools"}


def _resolve_manifest_dir(d: str) -> Path:
    return DEFAULT_TOOLS_DIR if d in BUILTIN_TOOLS_TOKENS else Path(d)


def build_tool_defs(registry: ToolRegistry, decompose_cfg: dict) -> list[dict]:
    defs = [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema or {"type": "object"}}
        for t in registry.list()
    ]
    if decompose_cfg.get("enabled"):
        defs.append(
            {
                "name": DECOMPOSE_TOOL_NAME,
                "description": "Break the current task into concrete sub-questions to research separately.",
                "input_schema": {"type": "object", "properties": {"task": {"type": "string"}}},
            }
        )
    return defs


# Endpoint pinned by the `openrouter` provider sugar. OpenRouter speaks
# OpenAI's chat-completions + tools wire format, so the OpenAI SDK
# (via base_url) is the cleanest client; no separate backend needed.
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Roles that may resolve to a separate backend. "main" is always present;
# the others are None when the user hasn't configured them (the agent loop
# falls back to main for decompose/planner, silences narration for narrator).
ROLES = ("main", "decompose", "planner", "narrator")


def _resolve_role_cfg(config: dict, role: str) -> dict | None:
    """Look up a role's fully-merged backend config from the normalized
    `llm.roles` dict produced by config._normalize_llm_roles. Returns None
    when the role is explicitly disabled (e.g. `llm.narrator: null`) or
    absent. `main` is always present.

    Tolerates a config that bypassed `load_config` (e.g. tests / programmatic
    use that hand-built a flat dict): if `llm.roles` is missing, normalize
    on the fly so the caller doesn't have to know about the new key."""
    llm = config.get("llm") or {}
    roles = llm.get("roles")
    if not roles:
        from fabri.config import _normalize_llm_roles
        roles = _normalize_llm_roles(config)["llm"]["roles"]
    return roles.get(role)


def _instantiate(rcfg: dict, tool_defs: list[dict]):
    """Single point of provider dispatch -- adding a fourth provider later
    (Vertex, Bedrock, Groq, ...) means one new branch here and nothing
    else."""
    provider = (rcfg.get("provider") or "anthropic").lower()
    model = rcfg["model"]
    max_tokens = int(rcfg.get("max_tokens") or 1024)
    api_key_env = rcfg.get("api_key_env") or "ANTHROPIC_API_KEY"
    if provider == "anthropic":
        return AnthropicLLMBackend(
            model=model,
            tools=tool_defs,
            max_tokens=max_tokens,
            api_key_env=api_key_env,
            cache_messages=bool(rcfg.get("cache_messages", False)),
        )
    if provider in ("openai", "openrouter"):
        base_url = rcfg.get("base_url") or (
            _OPENROUTER_BASE_URL if provider == "openrouter" else None
        )
        return OpenAILLMBackend(
            model=model,
            tools=tool_defs,
            max_tokens=max_tokens,
            api_key_env=api_key_env,
            base_url=base_url,
        )
    raise ValueError(f"unknown llm provider: {provider!r}")


def build_role_llm(config: dict, role: str, tool_defs: list[dict] | None = None):
    """Build the LLM backend for one role. `tool_defs` is the universal
    Anthropic-shaped tool list and should only be passed for `main`; the
    other roles run with no tools (decompose/planner emit JSON; narrator
    emits one short string). Returns None when the role is disabled or
    unset."""
    rcfg = _resolve_role_cfg(config, role)
    if rcfg is None or not rcfg.get("model"):
        return None
    return _instantiate(rcfg, tool_defs or [])


def build_llm(config: dict, tools_defs: list[dict], *, model_override: str | None = None):
    """Build the orchestrator (main) backend. `model_override`, when given,
    swaps just the model id while keeping the rest of the main role config
    intact -- used by tests / one-off scripts. Returning a real backend
    (not None) is invariant for `main`: the resolver guarantees a config."""
    rcfg = dict(_resolve_role_cfg(config, "main") or {})
    if model_override:
        rcfg["model"] = model_override
    return _instantiate(rcfg, tools_defs)


def build_decompose_llm(config: dict):
    """Returns a separate backend for the decompose meta-step, or None when
    `llm.decompose` is unset (the agent loop then reuses the main backend).
    Honors per-role provider, so a Sonnet orchestrator can run decompose on
    OpenRouter or OpenAI without affecting the main loop."""
    return build_role_llm(config, "decompose")


def find_missing_role_api_keys(config: dict) -> dict[str, list[str]]:
    """Walk every configured role; collect distinct `api_key_env` values for
    roles that will actually instantiate; return {env_var: [roles using it]}
    for the ones that aren't set in the current process environment. Empty
    dict means every required key is present."""
    needed: dict[str, list[str]] = {}
    for role in ROLES:
        rcfg = _resolve_role_cfg(config, role)
        if rcfg is None or not rcfg.get("model"):
            continue
        env = rcfg.get("api_key_env")
        if not env:
            continue
        needed.setdefault(env, []).append(role)
    return {env: roles for env, roles in needed.items() if not os.environ.get(env)}


def build_narrator_llm(config: dict):
    """Returns a cheap backend that emits short user-facing status updates
    between tool steps, or None when `llm.narrator` is set to null. Defaults
    to Haiku via the DEFAULT_CONFIG entry, and inherits any per-role
    provider override (anthropic / openai / openrouter)."""
    return build_role_llm(config, "narrator")


def build_tools(tools_cfg: dict) -> ToolRegistry:
    # sandbox_root is threaded to each tool spawn via env= (see
    # registry.invoke) rather than os.environ, so a parent registry and a
    # sub-agent registry with a tighter sandbox can coexist.
    sandbox_root = str(Path(tools_cfg["sandbox_root"]).resolve())
    manifest_dirs = tools_cfg["manifest_dir"]
    if isinstance(manifest_dirs, str):
        manifest_dirs = [manifest_dirs]
    registry = ToolRegistry(
        [_resolve_manifest_dir(d) for d in manifest_dirs], sandbox_root=sandbox_root
    )
    # `decompose` is a synthetic meta-tool the agent loop injects; a
    # user-shipped tool of the same name would shadow it. Refuse loudly.
    if DECOMPOSE_TOOL_NAME in registry.tools:
        raise ValueError(
            f"tool name {DECOMPOSE_TOOL_NAME!r} is reserved for the framework "
            f"meta-tool. Rename your tool (e.g. {DECOMPOSE_TOOL_NAME}_my)."
        )
    for entry in tools_cfg.get("agents", []):
        registry.register(make_agent_tool_manifest(entry))
    # Connection failures are logged but don't kill the build — one bad
    # MCP server shouldn't take down an otherwise-working agent.
    for server_cfg in tools_cfg.get("mcp_servers", []) or []:
        try:
            from fabri.tools.mcp_client import build_mcp_tools
            client, pairs = build_mcp_tools(server_cfg)
            for manifest, handler in pairs:
                registry.register_callable(manifest, handler, owns=client)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "MCP server %r failed to start (skipping): %s",
                server_cfg.get("name") or "?", e,
            )
    if tools_cfg["enabled"] is not None:
        registry.tools = {name: m for name, m in registry.tools.items() if name in tools_cfg["enabled"]}
    return registry
