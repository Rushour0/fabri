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
    """G16: pick the memory backend based on `memory.backend`.

    - "qdrant" (default): networked, multi-process safe.
    - "sqlite":  in-process, single-file, no docker required.

    Both expose the same interface (upsert/get/query/query_by_vector/
    find_similar/delete/count/iterate). The agent loop and orchestrator never
    know which one they're talking to.
    """
    backend = (mem_cfg.get("backend") or "qdrant").lower()
    if backend == "qdrant":
        return QdrantMemoryStore(
            url=mem_cfg["qdrant_url"], collection=mem_cfg["collection"]
        )
    if backend == "sqlite":
        # Imported lazily so a qdrant-only user doesn't pay sqlite-vec's
        # extension-load cost (and so the import error message is friendlier).
        from fabri.memory.embedded_store import SqliteMemoryStore
        return SqliteMemoryStore(path=mem_cfg.get("sqlite_path", ".fabri/memory.db"))
    raise ValueError(
        f"unknown memory.backend: {backend!r} (expected 'qdrant' or 'sqlite')"
    )

# Sentinel a config can put in tools.manifest_dir to pull in the framework's
# bundled tools (read_file/write_file/web_search/...) without naming a
# filesystem path -- so a consuming project never hardcodes where fabri
# happens to be installed (sibling checkout, wheel in site-packages, etc.).
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
    """If `llm.decompose_model` is set, returns a separate LLM backend bound to
    that (typically cheaper) model so a Sonnet orchestrator can run decompose
    on Haiku without changing its main backend. No tool defs -- decompose only
    asks for a plain string list. Returns None when unset; run_agent then
    reuses the main backend, preserving prior behavior."""
    decompose_model = config["llm"].get("decompose_model")
    if not decompose_model:
        return None
    return build_llm(config, [], model_override=decompose_model)


def build_tools(tools_cfg: dict) -> ToolRegistry:
    # file_read/file_write enforce a path jail against FABRI_SANDBOX_ROOT in
    # their own subprocess. ToolRegistry passes that env var explicitly to each
    # tool spawn (see registry.invoke); we deliberately do NOT mutate the
    # parent's os.environ, so two concurrent registries -- a parent and a
    # sub-agent with a tighter sandbox -- can coexist without one clobbering
    # the other's root.
    sandbox_root = str(Path(tools_cfg["sandbox_root"]).resolve())
    manifest_dirs = tools_cfg["manifest_dir"]
    if isinstance(manifest_dirs, str):
        manifest_dirs = [manifest_dirs]
    registry = ToolRegistry(
        [_resolve_manifest_dir(d) for d in manifest_dirs], sandbox_root=sandbox_root
    )
    for entry in tools_cfg.get("agents", []):
        registry.register(make_agent_tool_manifest(entry))
    # G19: MCP servers — connect each, list its tools, register them as
    # callables on the registry. The agent loop then sees them like any
    # other tool. Connection failures are logged but don't kill the build:
    # one bad MCP server shouldn't take down an otherwise-working agent.
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
