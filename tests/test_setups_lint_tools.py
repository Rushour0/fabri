"""Tool-count and schema-size lint. No network."""
from __future__ import annotations

import textwrap
from pathlib import Path

from fabri.setups import lint_setup, resolve_setup, resolve_tools
from fabri.setups.lint import TOOL_COUNT_WARN, TOOL_SCHEMA_TOKENS_WARN, tool_set_tokens


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip())
    return path


def make_layer(root: Path, count: int, description: str = "Fetch one thing.") -> list:
    lines = ["sources:"]
    for i in range(count):
        lines += [f"  - kind: http", f"    name: t{i}", f"    url: https://api.test/t{i}",
                  f"    description: {description}"]
    names = ", ".join(f"t{i}" for i in range(count))
    lines.append(f"allow: [{names}]")
    write(root / "tools/tools.yaml", "\n".join(lines) + "\n")
    return [("project", root)]


def run_lint(layers):
    resolved = resolve_setup(layers)
    tools = resolve_tools(resolved, use_cache=False, catalog=False)
    assert not tools.errors
    return tools, lint_setup(resolved, tool_set=tools)


def count_findings(findings):
    return [f for f in findings if f.item == "tools" and "tools resolve" in f.message]


def test_lint_warns_above_tool_count(tmp_path):
    tools, findings = run_lint(make_layer(tmp_path, TOOL_COUNT_WARN + 1))
    hits = count_findings(findings)
    assert len(tools.tools) == 41
    assert len(hits) == 1
    assert hits[0].level == "warning" and hits[0].layer == ""
    assert hits[0].message.startswith("41 tools resolve for this setup, about ")
    assert "turn on tool retrieval or narrow allow" in hits[0].message


def test_lint_quiet_below_limits(tmp_path):
    tools, findings = run_lint(make_layer(tmp_path, 5))
    assert count_findings(findings) == []
    assert tool_set_tokens(tools) < TOOL_SCHEMA_TOKENS_WARN


def test_lint_warns_on_schema_tokens_alone(tmp_path):
    big = "word " * 6000
    tools, findings = run_lint(make_layer(tmp_path, 3, description=big.strip()))
    assert len(tools.tools) <= TOOL_COUNT_WARN
    assert tool_set_tokens(tools) > TOOL_SCHEMA_TOKENS_WARN
    assert len(count_findings(findings)) == 1


def test_tool_set_tokens_monotonic(tmp_path):
    small = resolve_tools(make_layer(tmp_path / "a", 3), use_cache=False, catalog=False)
    more = resolve_tools(make_layer(tmp_path / "b", 6), use_cache=False, catalog=False)
    longer = resolve_tools(make_layer(tmp_path / "c", 3, description="Fetch one thing. " * 50),
                           use_cache=False, catalog=False)
    assert 0 < tool_set_tokens(small) < tool_set_tokens(more)
    assert tool_set_tokens(small) < tool_set_tokens(longer)
