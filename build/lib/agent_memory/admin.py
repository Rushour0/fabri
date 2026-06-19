"""Admin-only surface: config inspection and a dashboard summary, usable from
either the CLI (`cli.py admin ...`) or directly as a library call. There is no
real auth backend yet -- AGENT_ADMIN_TOKEN is a placeholder seam, not a
security boundary. Every admin entry point funnels through require_admin() so
real auth (SSO, an API gateway, whatever the deployment needs) has exactly one
place to be wired in later, instead of being scattered across call sites."""
import os

from agent_memory.memory.store import QdrantMemoryStore
from agent_memory.tools.agent_tool import AGENT_RUNNER_SCRIPT
from agent_memory.tools.registry import ToolRegistry

ADMIN_TOKEN_ENV = "AGENT_ADMIN_TOKEN"


class AdminAuthError(RuntimeError):
    pass


def require_admin(token: str | None) -> None:
    """If AGENT_ADMIN_TOKEN is unset, the gate is open -- there's no auth
    backend yet, so refusing to run at all would just be theater. Set it to
    start enforcing a shared-secret check; swap this function's body for real
    auth whenever that's available, since every admin command already calls
    it before doing anything."""
    expected = os.environ.get(ADMIN_TOKEN_ENV)
    if expected is None:
        return
    if token != expected:
        raise AdminAuthError(f"admin token required (pass --admin-token, must match ${ADMIN_TOKEN_ENV})")


def describe_config(config: dict, tools: ToolRegistry) -> dict:
    """Merged config plus the resolved tool registry, in a shape safe to
    print or serve: which tools are plain subprocess tools vs. another agent
    wired in via tools.agents (agent-as-tool, see tools/agent_tool.py)."""
    tool_rows = [
        {
            "name": t.name,
            "description": t.description,
            "is_agent_tool": str(AGENT_RUNNER_SCRIPT) in t.command,
            "command": t.command,
        }
        for t in tools.list()
    ]
    return {
        "agent": config["agent"],
        "llm": config["llm"],
        "sandbox_root": config["tools"]["sandbox_root"],
        "decompose": config["tools"]["decompose"],
        "tools": tool_rows,
    }


def memory_summary(store: QdrantMemoryStore) -> dict:
    return {"tactical": store.count(kind="tactical"), "strategic": store.count(kind="strategic")}


def render_dashboard(config: dict, tools: ToolRegistry, store: QdrantMemoryStore) -> str:
    desc = describe_config(config, tools)
    mem = memory_summary(store)
    lines = [
        f"agent:    {desc['agent']['name']}  (max_steps={desc['agent']['max_steps']})",
        f"llm:      {desc['llm']['provider']}/{desc['llm']['model']}",
        f"sandbox:  {desc['sandbox_root']}",
        f"decompose: {'on' if desc['decompose']['enabled'] else 'off'}",
        f"memory:   {mem['tactical']} tactical / {mem['strategic']} strategic guidelines",
        "tools:",
    ]
    for t in desc["tools"]:
        kind = "agent" if t["is_agent_tool"] else "tool"
        lines.append(f"  - [{kind}] {t['name']}: {t['description'].strip().splitlines()[0]}")
    return "\n".join(lines)
