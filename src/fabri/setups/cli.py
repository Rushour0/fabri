"""`fabri setup ...` (alias `fabri setups`), `fabri setups tools <add|list|...>` and
`fabri tools <list|sync|register|...>` commands."""
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
                          policy=policy, tool_set=tools)
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


# ---------------------------------------------------------------- setups tools (by scope)


def _scope_text(value: str) -> str:
    """`org:acme/q3` is short for `org:acme/project:q3`."""
    head, sep, tail = value.partition("/")
    if sep and head.startswith("org:") and not tail.startswith("project:"):
        return f"{head}/project:{tail}"
    return value


def _target(args: argparse.Namespace) -> tuple[Path, list, ToolPolicy, str]:
    """(layer dir, layer chain, policy, scope text) for --scope or --layer."""
    from fabri.setups.layers import Layer

    if args.layer:
        layer_dir = Path(args.layer)
        if not layer_dir.is_dir():
            raise SystemExit(f"layer directory {layer_dir} does not exist")
        return layer_dir, [Layer(layer_dir.name, layer_dir)], _policy(args.global_allow), layer_dir.name
    if not args.scope:
        raise SystemExit("give --scope (with --config or --root) or --layer DIR")
    from fabri.service.setups_routes import ApiError, SetupsApi, parse_scope
    from fabri.setups.layers import SetupError

    config = args.config or os.environ.get("FABRI_SETUPS_CONFIG")
    root = args.root or os.environ.get("FABRI_SETUPS_ROOT")
    if not config and not root:
        raise SystemExit("--scope needs --config FILE or --root DIR (or FABRI_SETUPS_CONFIG / FABRI_SETUPS_ROOT)")
    try:
        api = SetupsApi.from_files(root, config)
        scope = parse_scope(_scope_text(args.scope))
        layer_dir = api.layer_path(scope)
        chain = api.chain(scope)
    except ApiError as exc:
        raise SystemExit(exc.message) from None
    except SetupError as exc:
        raise SystemExit(str(exc)) from None
    if not layer_dir.is_dir():
        raise SystemExit(f"scope {scope.text} has no directory yet ({layer_dir}); create it first")
    policy = _policy(args.global_allow, strict=api.policy_name == "strict") if args.global_allow else api.policy
    return layer_dir, chain, policy, scope.text


def _print_entry_report(report, *, as_json: bool, dry_run: bool) -> None:
    if as_json:
        _print_json(report.to_dict())
        return
    counts = report.counts()
    if report.errors:
        print(f"not added: {report.file}")
        for error in report.errors:
            print(f"  error: {error}")
    else:
        verb = "would add" if dry_run else ("added" if report.written else "checked")
        print(f"{verb} {report.file} ({', '.join(report.kinds)})")
    if report.tools:
        print(f"  {len(report.tools)} tools: {counts['read']} read, {counts['write']} write, "
              f"{counts['always_approve']} always_approve")
        rows = [[t["approval"], t["name"], f"{t['method'] or ''} {t['path'] or ''}".strip() or t["kind"]]
                for t in sorted(report.tools, key=lambda t: (t["approval"], t["name"]))]
        for line in _table(rows, ["APPROVAL", "TOOL", "ENDPOINT"]).splitlines():
            print(f"  {line}")
    elif not report.connected and not report.errors:
        print("  tools are listed when the server is reached (run `fabri setups tools list`)")
    if report.hidden:
        print(f"  left out by allow or the global allowlist: {', '.join(sorted(report.hidden))}")
    for warning in report.warnings:
        print(f"  warning: {warning}")
    for finding in report.lint:
        print(f"  lint {finding['severity']}: {finding['message']}")


def _add(args: argparse.Namespace, build) -> None:
    import logging

    from fabri.setups.tool_entries import EntryError, add_entry

    logging.getLogger("fabri.setups.tools").setLevel(logging.ERROR)
    layer_dir, chain, policy, scope = _target(args)
    try:
        entry = build()
        report = add_entry(layer_dir, entry, chain=chain, policy=policy, replace=args.replace,
                           dry_run=args.dry_run, actor=_actor(args), scope=scope)
    except EntryError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        sys.exit(1)
    _print_entry_report(report, as_json=args.json, dry_run=args.dry_run)
    if report.errors or any(f["severity"] == "error" for f in report.lint):
        sys.exit(1)


def _headers_from(args: argparse.Namespace) -> dict[str, Any]:
    from fabri.setups.tool_entries import auth_headers

    return auth_headers(auth_env=args.auth_env, secret_ref=args.secret_ref, auth_header=args.auth_header,
                        auth_prefix=args.auth_prefix, header_env=args.header)


def cmd_setups_tools_add_openapi(args: argparse.Namespace) -> None:
    from fabri.setups.tool_entries import openapi_entry

    _add(args, lambda: openapi_entry(
        args.spec, name=args.name, include=args.include, exclude=args.exclude, methods=args.methods,
        allow_delete=args.allow_delete, approval=args.approval, base_url=args.base_url,
        headers=_headers_from(args), snapshot=args.snapshot, strip_prefixes=args.strip_prefix))


def cmd_setups_tools_add_mcp(args: argparse.Namespace) -> None:
    from fabri.setups.tool_entries import mcp_entry

    _add(args, lambda: mcp_entry(
        args.name, command=args.command, url=args.url, env=args.env, headers=_headers_from(args),
        include=args.include, approval=args.approval, connect=not args.no_connect))


def cmd_setups_tools_add_connector(args: argparse.Namespace) -> None:
    from fabri.setups.tool_entries import connector_tool_entry

    _add(args, lambda: connector_tool_entry(
        args.connector, name=args.name, token_env=args.token_env, workspace=args.workspace,
        remote=args.remote, approval=args.approval, connect=args.connect))


def cmd_setups_tools_list(args: argparse.Namespace) -> None:
    from fabri.setups import resolve_setup, resolve_tools
    from fabri.setups.tool_entries import list_entries, stack_summary

    layer_dir, chain, policy, scope = _target(args)
    entries = list_entries(layer_dir)
    summary, errors = {}, []
    if not args.no_resolve:
        tools = resolve_tools(resolve_setup(chain, use_cache=False), policy=policy, use_cache=False)
        summary = stack_summary(tools, layer_name=chain[-1].name)
        errors = list(tools.errors)
    for entry in entries:
        rows = [summary.get(s) for s in entry["sources"] if summary.get(s)]
        entry["tools"] = sum(r["tools"] for r in rows) if rows else None
        entry["approvals"] = {c: sum(r["approvals"][c] for r in rows) for c in ("read", "write", "always_approve")} \
            if rows else None
    if args.json:
        _print_json({"scope": scope, "entries": entries, "errors": errors})
        return
    table = []
    for e in entries:
        counts = "-" if e["approvals"] is None else \
            f"{e['tools']} ({e['approvals']['read']}r {e['approvals']['write']}w {e['approvals']['always_approve']}a)"
        table.append([e["name"], ",".join(e["kinds"]), "on" if e["enabled"] else "off", counts, e["file"]])
    print(_table(table, ["NAME", "KIND", "STATE", "TOOLS", "FILE"]) if table else f"no tool sources in {scope}")
    for error in errors:
        print(f"error: {error}", file=sys.stderr)


def cmd_setups_tools_toggle(args: argparse.Namespace) -> None:
    from fabri.setups.tool_entries import EntryError, set_entry_enabled

    layer_dir, _, _, scope = _target(args)
    enabled = args.setups_tools_command == "enable"
    try:
        changed = set_entry_enabled(layer_dir, args.name, enabled, actor=_actor(args), scope=scope)
    except EntryError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    state = "enabled" if enabled else "disabled"
    print(f"{state} {args.name}" if changed else f"{args.name} was already {state}")


def cmd_setups_tools_remove(args: argparse.Namespace) -> None:
    from fabri.setups.tool_entries import EntryError, remove_entry

    layer_dir, _, _, scope = _target(args)
    try:
        removed = remove_entry(layer_dir, args.name, actor=_actor(args), scope=scope)
    except EntryError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    print(f"removed {args.name}: {', '.join(removed)}")


def cmd_setups_tools_probe(args: argparse.Namespace) -> None:
    from fabri.tools.probe import probe_tools

    _, chain, policy, _ = _target(args)
    report = probe_tools(chain, policy=policy, ops=args.ops or None, max_calls_per_op=args.max_calls,
                         timeout_s=args.timeout)
    report.pop("results", None)
    _print_json(report)


def cmd_setups_tools_sync(args: argparse.Namespace) -> None:
    from fabri.setups import diff_snapshots, resolve_setup, resolve_tools
    from fabri.setups.toolset import default_state_dir
    from fabri.tools.catalog import read_state, save_catalog, write_state

    _, chain, policy, _ = _target(args)
    resolved = resolve_setup(chain, use_cache=False)
    tools = resolve_tools(resolved, policy=policy, use_cache=False)
    state_dir = default_state_dir(resolved)
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


def cmd_setups_tools_connectors(args: argparse.Namespace) -> None:
    from fabri.tools.connectors import list_connectors

    rows = list_connectors()
    if args.json:
        _print_json(rows)
        return
    print(_table([[c["name"], ",".join(c["kinds"]), c["env_var"], c["summary"]] for c in rows],
                 ["NAME", "KIND", "TOKEN ENV", "WHAT"]))


# ---------------------------------------------------------------- parser


def _layers(p: argparse.ArgumentParser) -> None:
    p.add_argument("--layers", nargs="+", required=True, metavar="DIR",
                   help="Layer directories, shallowest first. NAME=DIR sets the layer's name.")


def _target_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--scope", default=None, help="baseline, pack:<p>, org:<org> or org:<org>/<project>")
    p.add_argument("--config", default=None, metavar="FILE",
                   help="Setups config (as for fabri serve --setups-config); default $FABRI_SETUPS_CONFIG")
    p.add_argument("--root", default=None, metavar="DIR", help="Layer root; default $FABRI_SETUPS_ROOT")
    p.add_argument("--layer", default=None, metavar="DIR", help="Write into this layer directory instead of a scope")
    p.add_argument("--global-allow", default=None, metavar="FILE", help="Policy file; overrides the config's")
    p.add_argument("--actor", default=None)


def _add_args(p: argparse.ArgumentParser) -> None:
    _target_args(p)
    p.add_argument("--approval", choices=["read", "write", "always_approve"], default=None,
                   help="Floor: every tool gets at least this class, never less than its own default")
    p.add_argument("--dry-run", action="store_true", help="Check and print, write nothing")
    p.add_argument("--replace", action="store_true", help="Overwrite an existing entry with the same name")
    p.add_argument("--json", action="store_true")


def _auth_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--auth-env", default=None, metavar="VAR", help="Env var holding the token (name only)")
    p.add_argument("--secret-ref", default=None, metavar="PROVIDER:HANDLE", help="Credential store ref instead")
    p.add_argument("--auth-header", default=None, help="Header for the token (default Authorization)")
    p.add_argument("--auth-prefix", default="Bearer ", help="Scheme before the token (default 'Bearer ')")
    p.add_argument("--header", action="append", default=None, metavar="NAME=VAR",
                   help="Extra header read from an env var; repeatable")


def _add_setups_tools(setup_sub: argparse._SubParsersAction) -> None:
    p_st = setup_sub.add_parser("tools", help="Add and manage tool sources in a scope's tools/ layer")
    st_sub = p_st.add_subparsers(dest="setups_tools_command", required=True)

    p_add = st_sub.add_parser("add", help="Add an OpenAPI spec, an MCP server or a connector")
    add_sub = p_add.add_subparsers(dest="add_kind", required=True)

    p = add_sub.add_parser("openapi", help="Every operation of an OpenAPI 3 spec (file or URL) as a tool")
    p.add_argument("spec", help="Spec file path or http(s) URL")
    p.add_argument("--name", default=None, help="Source name (default from the spec title)")
    p.add_argument("--include", nargs="+", default=None, metavar="PATTERN", help="Only these tools (fnmatch)")
    p.add_argument("--exclude", nargs="+", default=None, metavar="PATTERN")
    p.add_argument("--methods", nargs="+", default=None, metavar="METHOD")
    p.add_argument("--allow-delete", action="store_true", help="Include DELETE operations (always_approve)")
    p.add_argument("--base-url", default=None)
    p.add_argument("--strip-prefix", nargs="+", default=None, metavar="SEGMENT",
                   help="Path segments dropped from tool names (default api)")
    p.add_argument("--snapshot", action="store_true", help="Save a URL spec into tools/specs/ instead of linking it")
    _auth_args(p)
    _add_args(p)
    p.set_defaults(func=cmd_setups_tools_add_openapi)

    p = add_sub.add_parser("mcp", help="An MCP server over stdio (--command) or Streamable HTTP (--url)")
    p.add_argument("name", help="Source name; tools are named mcp_<name>_<tool>")
    p.add_argument("--command", default=None, help='The server command line, for example "npx -y @scope/server"')
    p.add_argument("--url", default=None)
    p.add_argument("--env", action="append", default=None, metavar="NAME[=VAR]",
                   help="Env for a stdio server, read from this environment; repeatable")
    p.add_argument("--include", nargs="+", default=None, metavar="PATTERN", help="Only these server tools (fnmatch)")
    p.add_argument("--no-connect", action="store_true", help="Write without starting the server to list tools")
    _auth_args(p)
    _add_args(p)
    p.set_defaults(func=cmd_setups_tools_add_mcp)

    p = add_sub.add_parser("connector", help="A ready-made source: github, linear or slack")
    p.add_argument("connector", help="See `fabri setups tools connectors`")
    p.add_argument("--name", default=None)
    p.add_argument("--token-env", default=None, metavar="VAR", help="Env var holding the token")
    p.add_argument("--workspace", default=None, help="Use the credential store ref <provider>:<workspace>")
    p.add_argument("--remote", action="store_true", help="github: the hosted MCP server instead of Docker")
    p.add_argument("--connect", action="store_true", help="Reach the server now to list its tools")
    _add_args(p)
    p.set_defaults(func=cmd_setups_tools_add_connector)

    p = st_sub.add_parser("list", help="Sources in the scope's layer, with tool counts by approval class")
    _target_args(p)
    p.add_argument("--no-resolve", action="store_true", help="Read the files only; do not build tools")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_setups_tools_list)

    for verb in ("enable", "disable"):
        p = st_sub.add_parser(verb, help=f"{verb.capitalize()} a source")
        p.add_argument("name")
        _target_args(p)
        p.set_defaults(func=cmd_setups_tools_toggle)

    p = st_sub.add_parser("remove", help="Delete a source file and its spec copy")
    p.add_argument("name")
    _target_args(p)
    p.set_defaults(func=cmd_setups_tools_remove)

    p = st_sub.add_parser("probe", help="Call safe GET operations and save options and examples")
    _target_args(p)
    p.add_argument("--ops", nargs="*", default=None)
    p.add_argument("--max-calls", type=int, default=3)
    p.add_argument("--timeout", type=float, default=5.0)
    p.set_defaults(func=cmd_setups_tools_probe)

    p = st_sub.add_parser("sync", help="Print added/removed/changed tools and save the catalog")
    _target_args(p)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_setups_tools_sync)

    p = st_sub.add_parser("connectors", help="List the ready-made connectors")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_setups_tools_connectors)


def add_setup_commands(sub: argparse._SubParsersAction, p_tools: argparse.ArgumentParser) -> None:
    p_setup = sub.add_parser("setup", aliases=["setups"], help="Layered setups: show, import, lint and tools")
    setup_sub = p_setup.add_subparsers(dest="setup_command", required=True)
    _add_setups_tools(setup_sub)

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
