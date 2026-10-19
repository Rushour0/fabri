"""Lint a layer stack: budgets, writing tells, missing tools, unknown keys.

``lint_setup(layers, tool_names=...)`` re-runs every write-time check on the
files already on disk (so a hand edit gets the same review as a PUT), then
checks the merged result:

- each agent type's merged prompt and its context against the budgets
- skills and agent types that name tools the resolved tool set lacks
- skills imported with ``types: []``, which load for every type
- ``slop_report`` tells in prompts and context (warnings, never errors)
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from fabri.setups.layers import LayerLike, SetupError, as_layers, layer_budgets
from fabri.setups.resolver import ResolvedSetup, resolve_setup
from fabri.setups.store import (
    DIR_FILES,
    validate_agent,
    validate_context,
    validate_data,
    validate_play,
    validate_setup_yaml,
    validate_skill,
    validate_tools_yaml,
)
from fabri.tools.policy import ToolPolicy


@dataclass
class LintFinding:
    level: str  # error | warning
    layer: str
    item: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _files(directory: Path, names: Iterable[str]) -> dict[str, str]:
    return {n: (directory / n).read_text(encoding="utf-8") for n in names if (directory / n).is_file()}


def lint_setup(
    layers: Iterable[LayerLike] | ResolvedSetup,
    *,
    tool_names: Iterable[str] | None = None,
    policy: ToolPolicy | None = None,
) -> list[LintFinding]:
    resolved = layers if isinstance(layers, ResolvedSetup) else resolve_setup(as_layers(layers))
    findings: list[LintFinding] = []

    def run(layer: str, item: str, fn, *args) -> None:
        try:
            for message in fn(*args) or []:
                findings.append(LintFinding("warning", layer, item, message))
        except SetupError as exc:
            findings.append(LintFinding("error", layer, item, str(exc)))

    for layer in resolved.layers:
        root = layer.path
        budgets = layer_budgets(root)
        if (root / "setup.yaml").is_file():
            run(layer.name, "setup.yaml", validate_setup_yaml, (root / "setup.yaml").read_text(encoding="utf-8"))
        for kind, check in (("agents", validate_agent), ("skills", validate_skill)):
            for directory in sorted((root / kind).iterdir()) if (root / kind).is_dir() else []:
                if directory.is_dir() and not directory.name.startswith("."):
                    run(layer.name, f"{kind}/{directory.name}", check, directory.name,
                        _files(directory, DIR_FILES[kind]), budgets, policy)
        for directory in sorted((root / "plays").iterdir()) if (root / "plays").is_dir() else []:
            if directory.is_dir() and not directory.name.startswith("."):
                run(layer.name, f"plays/{directory.name}", validate_play, directory.name,
                    _files(directory, ("play.yaml",)))
        for path in sorted((root / "context").glob("*.md")) if (root / "context").is_dir() else []:
            run(layer.name, f"context/{path.stem}", validate_context, path.stem,
                path.read_text(encoding="utf-8"), budgets)
        for path in sorted((root / "data").iterdir()) if (root / "data").is_dir() else []:
            if path.is_file() and not path.name.startswith(".") and path.suffix != ".disabled":
                run(layer.name, f"data/{path.name}", validate_data, path.name, path.read_bytes(), budgets)
        if (root / "tools" / "tools.yaml").is_file():
            run(layer.name, "tools/tools.yaml", validate_tools_yaml,
                (root / "tools" / "tools.yaml").read_text(encoding="utf-8"), policy)

    known = set(tool_names) if tool_names is not None else None
    budgets = resolved.budgets
    for agent_type in resolved.agent_types():
        agent = resolved.agents[agent_type]
        prompt, _ = resolved.system_prompt(agent_type)
        if len(agent.get("prompt", "")) > budgets["prompt_chars"]:
            findings.append(LintFinding("error", agent["source"], f"agents/{agent_type}",
                                        f"merged prompt is {len(agent['prompt'])} chars, budget {budgets['prompt_chars']}"))
        context_chars = sum(len(c["text"]) for c in resolved.context_for(agent_type).values())
        if context_chars > budgets["context_chars"]:
            findings.append(LintFinding("warning", agent["source"], f"agents/{agent_type}",
                                        f"context for this type is {context_chars} chars, budget "
                                        f"{budgets['context_chars']}; it will be cut at the last whole file"))
        if known is not None:
            for tool in agent["config"].get("tools") or []:
                if tool not in known:
                    findings.append(LintFinding("error", agent["source"], f"agents/{agent_type}",
                                                f"tool {tool!r} is not in the resolved tool set"))
    for name, skill in resolved.skills.items():
        if known is not None:
            for tool in skill.get("tools") or []:
                if tool not in known:
                    findings.append(LintFinding("error", skill["source"], f"skills/{name}",
                                                f"tool {tool!r} is not in the resolved tool set"))
    return _dedupe(findings)


def _dedupe(findings: list[LintFinding]) -> list[LintFinding]:
    seen, out = set(), []
    for f in findings:
        key = (f.level, f.layer, f.item, f.message)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def has_errors(findings: Iterable[LintFinding]) -> bool:
    return any(f.level == "error" for f in findings)


__all__ = ["LintFinding", "has_errors", "lint_setup"]
