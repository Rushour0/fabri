"""Print the schema token cost of every tool a layer stack resolves.

    python scripts/measure_tool_tokens.py --layers DIR [DIR ...]

Layers are given lowest first (baseline, then pack, then company). Tools are
resolved with the permissive policy, no cache and no catalog, so the numbers
are the raw schemas. Output: one line per tool, `tokens name approval`,
biggest first, then the totals and the lint thresholds.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from fabri.memory.compress import count_tokens
from fabri.setups.lint import TOOL_COUNT_WARN, TOOL_SCHEMA_TOKENS_WARN
from fabri.setups.toolset import resolve_tools
from fabri.tools.policy import ToolPolicy


def measure(layer_dirs: list[Path]) -> tuple[list[tuple[int, str, str]], list[str]]:
    layers = [(d.name, d) for d in layer_dirs]
    tool_set = resolve_tools(layers, policy=ToolPolicy.permissive(), use_cache=False, catalog=False)
    rows = []
    for tool in tool_set.tools.values():
        payload = {"name": tool.name, "description": tool.manifest.description,
                   "input_schema": tool.manifest.input_schema}
        rows.append((count_tokens(json.dumps(payload, sort_keys=True, default=str)), tool.name, tool.approval))
    rows.sort(key=lambda r: (-r[0], r[1]))
    return rows, list(tool_set.errors)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--layers", nargs="+", required=True, type=Path, metavar="DIR")
    args = parser.parse_args(argv)
    missing = [str(d) for d in args.layers if not d.is_dir()]
    if missing:
        print(f"not a directory: {', '.join(missing)}", file=sys.stderr)
        return 2
    rows, errors = measure(args.layers)
    for tokens, name, approval in rows:
        print(f"{tokens:6d} {name} {approval}")
    total = sum(r[0] for r in rows)
    print(f"total: {len(rows)} tools, {total} tokens")
    print(f"thresholds: {TOOL_COUNT_WARN} tools, {TOOL_SCHEMA_TOKENS_WARN} tokens")
    for err in errors:
        print(f"build error: {err}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
