"""Turns another agent.yaml into a ToolManifest, so one agent can call another
as a tool -- composition instead of a dispatcher/orchestration layer. The
sub-agent runs as an ordinary tool subprocess (agent_runner_tool.py), so it
goes through the exact same {ok, error?, result?} contract as any other tool."""
from pathlib import Path

from fabri.tools.manifest_schema import ToolManifest

AGENT_RUNNER_SCRIPT = Path(__file__).resolve().parent / "agent_runner_tool.py"

DEFAULT_TIMEOUT_S = 120.0


def make_agent_tool_manifest(entry: dict) -> ToolManifest:
    """`entry` is one item from a config's `tools.agents` list:
    {name, description, config, timeout_s?}. `config` is a path to the
    sub-agent's own agent.yaml, resolved relative to the cwd the parent agent
    is run from (same convention as tools.manifest_dir/sandbox_root)."""
    config_path = str(Path(entry["config"]).resolve())
    return ToolManifest(
        name=entry["name"],
        description=entry["description"],
        command=["python3", str(AGENT_RUNNER_SCRIPT), config_path],
        input_schema={
            "type": "object",
            "properties": {"task": {"type": "string"}},
            "required": ["task"],
        },
        output_schema={
            "type": "object",
            "properties": {"final_text": {"type": "string"}, "outcome": {"type": "string"}},
        },
        timeout_s=entry.get("timeout_s", DEFAULT_TIMEOUT_S),
    )
