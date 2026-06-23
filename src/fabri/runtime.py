"""Shared composition helpers for turning a loaded config into the objects
run_agent() needs. Used by both cli.py and tools/agent_runner_tool.py (the
agent-as-tool adapter) so the two entry points build agents identically."""
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


def build_llm(config: dict, tools_defs: list[dict], *, model_override: str | None = None):
    llm_cfg = config["llm"]
    provider = llm_cfg["provider"]
    model = model_override or llm_cfg["model"]
    if provider == "anthropic":
        return AnthropicLLMBackend(
            model=model,
            tools=tools_defs,
            max_tokens=llm_cfg["max_tokens"],
            api_key_env=llm_cfg["api_key_env"],
            cache_messages=bool(llm_cfg.get("cache_messages", False)),
        )
    if provider == "openai":
        return OpenAILLMBackend(
            model=model,
            tools=tools_defs,
            max_tokens=llm_cfg["max_tokens"],
            api_key_env=llm_cfg["api_key_env"],
        )
    raise ValueError(f"unknown llm provider: {provider}")


def build_decompose_llm(config: dict):
    """Returns a separate LLM backend bound to `llm.decompose_model` so a
    Sonnet orchestrator can run decompose on Haiku. No tool defs — decompose
    only asks for a plain string list. None when unset; run_agent then
    reuses the main backend."""
    decompose_model = config["llm"].get("decompose_model")
    if not decompose_model:
        return None
    return build_llm(config, [], model_override=decompose_model)


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
