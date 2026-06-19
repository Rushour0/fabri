"""Shared composition helpers for turning a loaded config into the objects
run_agent() needs. Used by both cli.py and tools/agent_runner_tool.py (the
agent-as-tool adapter) so the two entry points build agents identically."""
import os
from pathlib import Path

from agent_memory.config import DEFAULT_TOOLS_DIR
from agent_memory.core.agent import DECOMPOSE_TOOL_NAME
from agent_memory.core.llm import AnthropicLLMBackend, OpenAILLMBackend
from agent_memory.tools.agent_tool import make_agent_tool_manifest
from agent_memory.tools.registry import ToolRegistry

# Sentinel a config can put in tools.manifest_dir to pull in the framework's
# bundled tools (read_file/write_file/web_search/...) without naming a
# filesystem path -- so a consuming project never hardcodes where agent-memory
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


def build_llm(config: dict, tools_defs: list[dict]):
    llm_cfg = config["llm"]
    provider = llm_cfg["provider"]
    if provider == "anthropic":
        return AnthropicLLMBackend(model=llm_cfg["model"], tools=tools_defs, max_tokens=llm_cfg["max_tokens"])
    if provider == "openai":
        return OpenAILLMBackend(
            model=llm_cfg["model"],
            tools=tools_defs,
            max_tokens=llm_cfg["max_tokens"],
            api_key_env=llm_cfg["api_key_env"],
        )
    raise ValueError(f"unknown llm provider: {provider}")


def build_tools(tools_cfg: dict) -> ToolRegistry:
    # file_read/file_write enforce this against AGENT_SANDBOX_ROOT themselves;
    # set here (inherited by every tool subprocess) rather than threading a new
    # parameter through ToolRegistry/run_tool for a constraint only two tools need.
    os.environ["AGENT_SANDBOX_ROOT"] = str(Path(tools_cfg["sandbox_root"]).resolve())
    manifest_dirs = tools_cfg["manifest_dir"]
    if isinstance(manifest_dirs, str):
        manifest_dirs = [manifest_dirs]
    registry = ToolRegistry([_resolve_manifest_dir(d) for d in manifest_dirs])
    for entry in tools_cfg.get("agents", []):
        registry.register(make_agent_tool_manifest(entry))
    if tools_cfg["enabled"] is not None:
        registry.tools = {name: m for name, m in registry.tools.items() if name in tools_cfg["enabled"]}
    return registry
