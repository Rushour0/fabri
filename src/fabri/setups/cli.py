"""`fabri setup ...` and `fabri tools <list|sync|register|...>` commands."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

from fabri.tools.policy import ToolPolicy


def _policy(path: str | None, *, strict: bool = False) -> ToolPolicy:
    base = ToolPolicy() if strict else ToolPolicy.permissive()
    return ToolPolicy.from_file(path, base=base) if path else base


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=False, default=str))


def _actor(args: argparse.Namespace) -> str:
    return args.actor or os.environ.get("USER") or "cli"


def _source_label(tool) -> str:
    if tool.origin == "registered":
        return "registered"
    return "openapi" if tool.kind == "openapi" else "declared"


def _table(rows: list[list[str]], header: list[str]) -> str:
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]
    lines = ["  ".join(str(c).ljust(w) for c, w in zip(header, widths)).rstrip()]
    lines += ["  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]
    return "\n".join(lines)


# ---------------------------------------------------------------- setup


def cmd_setup_show(args: argparse.Namespace) -> None:
    from fabri.setups import resolve_setup

    resolved = resolve_setup(args.layers, use_cache=False)
    _print_json(resolved.to_dict(args.type))


def cmd_setup_import(args: argparse.Namespace) -> None:
    from fabri.setups import import_skills

    report = import_skills(args.source, args.into, fmt=args.format, dry_run=args.dry_run,
                           policy=_policy(args.global_allow) if args.global_allow else None)
    _print_json(report.to_dict())
    if report.invalid:
        sys.exit(1)


def cmd_setup_lint(args: argparse.Namespace) -> None:
    from fabri.setups import has_errors, lint_setup, resolve_setup, resolve_tools

    policy = _policy(args.global_allow)
    resolved = resolve_setup(args.layers, use_cache=False)
    tools = resolve_tools(resolved, policy=policy, use_cache=False)
    findings = lint_setup(resolved, tool_names=[n for t in tools.tools.values() for n in [t.name, *t.aliases]],
                          policy=policy)
    for f in findings:
        print(f"{f.level:7} {f.layer:12} {f.item:28} {f.message}")
    for error in tools.errors:
        print(f"error   tools        {'':28} {error}")
    print(f"{sum(f.level == 'error' for f in findings)} errors, {sum(f.level == 'warning' for f in findings)} warnings")
    if has_errors(findings) or tools.errors:
        sys.exit(1)


# ---------------------------------------------------------------- tools


def cmd_tools_list(args: argparse.Namespace) -> None:
    from fabri.setups import resolve_setup, resolve_tools

    resolved = resolve_setup(args.layers, use_cache=False)
    tools = resolve_tools(resolved, policy=_policy(args.global_allow), use_cache=False)
    chosen = tools.for_type(resolved, args.type) if args.type else list(tools.tools.values())
    if args.json:
        _print_json([{**t.row(), "title": t.manifest.title, "origin_label": _source_label(t)} for t in chosen])
        return
    rows = [[t.name, t.approval, _source_label(t), f"{t.method or ''} {t.path or ''}".strip() or t.kind,
             t.layer, t.manifest.title or ""] for t in sorted(chosen, key=lambda t: t.name)]
    print(_table(rows, ["NAME", "APPROVAL", "SOURCE", "ENDPOINT", "LAYER", "TITLE"]))
    for error in tools.errors:
        print(f"error: {error}", file=sys.stderr)


def cmd_tools_sync(args: argparse.Namespace) -> None:
    from fabri.setups import diff_snapshots, resolve_setup, resolve_tools
    from fabri.tools.catalog import read_state, save_catalog, write_state

    resolved = resolve_setup(args.layers, use_cache=False)
    tools = resolve_tools(resolved, policy=_policy(args.global_allow), use_cache=False,
                          state_dir=args.state)
    state_dir = Path(args.state) if args.state else resolved.layers[-1].path / "tools" / ".fabri-state"
    previous = read_state(state_dir, "sync.json").get("tools") or {}
    current = tools.snapshot()
    diff = diff_snapshots(previous, current)
    for label in ("added", "removed", "changed"):
        for name in diff[label]:
            print(f"{label:8} {name}")
    print(f"{len(diff['added'])} added, {len(diff['removed'])} removed, {len(diff['changed'])} changed, "
          f"{len(current)} total")
    for error in tools.errors:
        print(f"error: {error}", file=sys.stderr)
    if not args.dry_run:
        write_state(state_dir, "sync.json", {"version": 1, "content_hash": resolved.content_hash, "tools": current})
        save_catalog(tools.catalog, state_dir)
    if tools.errors:
        sys.exit(1)


def _load_decl(path: str) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    data = json.loads(text) if path.endswith(".json") else yaml.safe_load(text)
    if not isinstance(data, dict):
        raise SystemExit("the declaration file must hold one mapping")
    return data


def cmd_tools_register(args: argparse.Namespace) -> None:
    from fabri.tools.policy import PolicyError
    from fabri.tools.registration import RegistrationError, register_tool

    try:
        tool = register_tool(args.layer, _load_decl(args.file), actor=_actor(args),
                             policy=_policy(args.policy, strict=True))
    except (RegistrationError, PolicyError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        sys.exit(1)
    state = "registered" if tool.changed else "unchanged"
    print(f"{state} {tool.name} version {tool.version} ({tool.kind}, {tool.approval}) at {tool.path}")


def cmd_tools_unregister(args: argparse.Namespace) -> None:
    from fabri.tools.registration import unregister_tool

    if unregister_tool(args.layer, args.name, actor=_actor(args)):
        print(f"unregistered {args.name}")
    else:
        print(f"no registered tool named {args.name}", file=sys.stderr)
        sys.exit(1)


def cmd_tools_toggle(args: argparse.Namespace) -> None:
    from fabri.tools.registration import RegistrationError, set_enabled

    enabled = args.tools_command == "enable"
    try:
        tool = set_enabled(args.layer, args.name, enabled, actor=_actor(args))
    except RegistrationError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    print(f"{'enabled' if tool.enabled else 'disabled'} {tool.name}")


def cmd_tools_probe(args: argparse.Namespace) -> None:
    from fabri.tools.probe import probe_tools

    report = probe_tools(args.layers, policy=_policy(args.global_allow), ops=args.ops or None,
                         max_calls_per_op=args.max_calls, timeout_s=args.timeout, state_dir=args.state)
    report.pop("results", None)
    _print_json(report)


def cmd_tools_describe(args: argparse.Namespace) -> None:
    from fabri.tools.describe import describe_with_llm

    llm = args.llm
    llm_cfg = yaml.safe_load(Path(llm).read_text(encoding="utf-8")) if Path(llm).is_file() else yaml.safe_load(llm)
    if isinstance(llm_cfg, dict) and isinstance(llm_cfg.get("llm"), dict):
        llm_cfg = llm_cfg["llm"]
    if not isinstance(llm_cfg, dict):
        raise SystemExit("--llm must be a YAML or JSON llm block, or a file holding one")
    _print_json(describe_with_llm(args.layers, llm_cfg, policy=_policy(args.global_allow),
                                  state_dir=args.state, force=args.force))


# ---------------------------------------------------------------- parser


def _layers(p: argparse.ArgumentParser) -> None:
    p.add_argument("--layers", nargs="+", required=True, metavar="DIR",
                   help="Layer directories, shallowest first. NAME=DIR sets the layer's name.")


def add_setup_commands(sub: argparse._SubParsersAction, p_tools: argparse.ArgumentParser) -> None:
    p_setup = sub.add_parser("setup", help="Layered setups: show, import and lint")
    setup_sub = p_setup.add_subparsers(dest="setup_command", required=True)

    p = setup_sub.add_parser("show", help="Print the resolved setup with the source layer of every item")
    _layers(p)
    p.add_argument("--type", default=None, help="Narrow to what one agent type loads")
    p.set_defaults(func=cmd_setup_show)

    p = setup_sub.add_parser("import", help="Import skills into a layer (idempotent)")
    p.add_argument("--from", dest="source", required=True, metavar="DIR")
    p.add_argument("--format", choices=["skill-md", "fabri"], default="skill-md")
    p.add_argument("--into", required=True, metavar="LAYER_DIR")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--global-allow", default=None, metavar="FILE")
    p.set_defaults(func=cmd_setup_import)

    p = setup_sub.add_parser("lint", help="Budgets, writing tells, missing tools and unknown keys")
    _layers(p)
    p.add_argument("--global-allow", default=None, metavar="FILE")
    p.set_defaults(func=cmd_setup_lint)

    tools_sub = p_tools.add_subparsers(dest="tools_command", required=False)
    p = tools_sub.add_parser("list", help="List the tools a layer stack resolves to")
    _layers(p)
    p.add_argument("--type", default=None)
    p.add_argument("--global-allow", default=None, metavar="FILE")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_tools_list)

    p = tools_sub.add_parser("sync", help="Resolve tools, print added/removed/changed, save the catalog")
    _layers(p)
    p.add_argument("--global-allow", required=True, metavar="FILE")
    p.add_argument("--state", default=None, metavar="DIR",
                   help="State directory (default: <deepest layer>/tools/.fabri-state)")
    p.add_argument("--dry-run", action="store_true", help="Print the diff without saving")
    p.set_defaults(func=cmd_tools_sync)

    p = tools_sub.add_parser("register", help="Register one tool into a layer")
    p.add_argument("--layer", required=True, metavar="DIR")
    p.add_argument("--file", required=True, help="Declaration as JSON or YAML")
    p.add_argument("--policy", default=None, metavar="FILE",
                   help="Policy YAML (default: http and openapi only, no DELETE, no python)")
    p.add_argument("--actor", default=None)
    p.set_defaults(func=cmd_tools_register)

    p = tools_sub.add_parser("unregister", help="Remove a registered tool from a layer")
    p.add_argument("--layer", required=True, metavar="DIR")
    p.add_argument("--name", required=True)
    p.add_argument("--actor", default=None)
    p.set_defaults(func=cmd_tools_unregister)

    for verb in ("enable", "disable"):
        p = tools_sub.add_parser(verb, help=f"{verb.capitalize()} a registered tool")
        p.add_argument("--layer", required=True, metavar="DIR")
        p.add_argument("--name", required=True)
        p.add_argument("--actor", default=None)
        p.set_defaults(func=cmd_tools_toggle)

    p = tools_sub.add_parser("probe", help="Call safe GET operations and save options and examples")
    _layers(p)
    p.add_argument("--ops", nargs="*", default=None, help="Only these tools (names or operation ids)")
    p.add_argument("--global-allow", default=None, metavar="FILE")
    p.add_argument("--max-calls", type=int, default=3)
    p.add_argument("--timeout", type=float, default=5.0)
    p.add_argument("--state", default=None, metavar="DIR")
    p.set_defaults(func=cmd_tools_probe)

    p = tools_sub.add_parser("describe", help="Rewrite tool titles and descriptions with an LLM (cached)")
    _layers(p)
    p.add_argument("--llm", required=True, help="An llm block as YAML/JSON, or a file holding one")
    p.add_argument("--global-allow", default=None, metavar="FILE")
    p.add_argument("--state", default=None, metavar="DIR")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_tools_describe)
