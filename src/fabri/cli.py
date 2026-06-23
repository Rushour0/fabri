import argparse
import importlib.metadata
import json
import os
import sys
import uuid

from fabri.admin import AdminAuthError, describe_config, render_dashboard, require_admin
from fabri.config import ConfigError, load_config
from fabri.core.agent import run_agent
from fabri.core.logging_setup import configure_logging
from fabri.core.outcome import Outcome
from fabri.orchestrator.pipeline import process_trace
from fabri.runtime import (
    build_decompose_llm,
    build_llm,
    build_memory_store,
    build_tool_defs,
    build_tools,
)
from fabri.scaffold import SCAFFOLD_TEMPLATES, next_steps, scaffold
from fabri.tool_scaffold import SUPPORTED_LANGUAGES, scaffold_tool


def cmd_init(args: argparse.Namespace) -> None:
    template = getattr(args, "template", "default") or "default"
    try:
        result = scaffold(args.dir, force=args.force, template=template)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    where = "current directory" if args.dir in (".", "") else args.dir
    if result["created"]:
        print(f"Scaffolded a {template!r} fabri project in {where}:")
        for rel in result["created"]:
            print(f"  + {rel}")
    if result["skipped"]:
        print("\nLeft existing files untouched (pass --force to overwrite):")
        for rel in result["skipped"]:
            print(f"  . {rel}")
    print("\n" + next_steps(args.dir, template=template))


def _planner_mode_from_cfg(planner_cfg: dict) -> str:
    """Translate the agent.planner block (which carries both an enabled flag and
    a mode string for back-compat) into the run_agent.planner_mode argument."""
    mode = planner_cfg.get("mode", "off")
    if mode in ("auto", "force", "off"):
        if not planner_cfg.get("enabled", False) and mode != "off":
            # `enabled: false` wins over any non-off mode -- so a stale `mode`
            # value in a config can't surprise-activate the planner.
            return "off"
        return mode
    return "off"


def _require_api_key(api_key_env: str) -> None:
    if not os.environ.get(api_key_env):
        print(
            f"{api_key_env} is not set. Export it before running the live agent, "
            f"e.g.: export {api_key_env}=<your-api-key>",
            file=sys.stderr,
        )
        sys.exit(1)


def _open_store(mem_cfg: dict):
    """Open the configured memory backend with a user-friendly error message
    if it's unreachable, instead of leaking a raw qdrant/grpc/sqlite traceback.

    Picks Qdrant or sqlite-vec based on `memory.backend` — see
    `runtime.build_memory_store`.
    """
    backend = (mem_cfg.get("backend") or "qdrant").lower()
    try:
        return build_memory_store(mem_cfg)
    except Exception as e:
        if backend == "qdrant":
            print(
                f"Could not reach Qdrant at {mem_cfg.get('qdrant_url')}: {e}\n"
                "Start it with: docker compose up -d  (or `docker run -p 6333:6333 qdrant/qdrant`).\n"
                "Or switch to the embedded backend: set memory.backend: sqlite in agent.yaml "
                "(pip install 'fabri[sqlite]').",
                file=sys.stderr,
            )
        else:
            print(f"Could not open memory backend {backend!r}: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_run(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    _require_api_key(config["llm"]["api_key_env"])
    session_id = args.session_id or str(uuid.uuid4())
    configure_logging(session_id, verbose=args.verbose)
    if getattr(args, "ask_user_socket", None):
        os.environ["FABRI_ASK_USER_SOCKET"] = args.ask_user_socket

    mem_cfg = config["memory"]
    store = _open_store(mem_cfg)

    tools_cfg = config["tools"]
    tools = build_tools(tools_cfg)

    decompose_cfg = tools_cfg["decompose"]
    llm = build_llm(config, build_tool_defs(tools, decompose_cfg))

    result = run_agent(
        args.task,
        llm,
        tools,
        store,
        session_id=session_id,
        max_steps=config["agent"]["max_steps"],
        top_k=mem_cfg["top_k"],
        max_subquestions=decompose_cfg["max_subquestions"],
        system_prompt=config["agent"].get("system_prompt", ""),
        system_prompt_prefix=config["agent"].get("system_prompt_prefix", ""),
        result_format=tools_cfg.get("result_format", "toon"),
        output_format=config["agent"].get("output_format", "json"),
        decompose_llm=build_decompose_llm(config),
        planner_llm=build_decompose_llm(config),
        planner_mode=_planner_mode_from_cfg(config["agent"].get("planner", {})),
        planner_max_items=config["agent"].get("planner", {}).get("max_items", 8),
        planner_auto_token_threshold=config["agent"].get("planner", {}).get("auto_token_threshold", 80),
        tool_retrieval_enabled=tools_cfg.get("retrieval", {}).get("enabled", False),
        tool_retrieval_top_k=tools_cfg.get("retrieval", {}).get("top_k", 6),
        tool_retrieval_always_include=tuple(
            tools_cfg.get("retrieval", {}).get("always_include", [])
        ),
        max_cost_usd=config["agent"].get("max_cost_usd"),
    )
    print(json.dumps(result, indent=2))
    # Surface a non-success outcome via exit code — host services dispatch
    # on `fabri run`'s returncode. Without this, a rate-limit failure exits
    # 0 and downstream ledgers mark the run succeeded.
    # BUDGET_EXCEEDED is a deliberate halt, not a SUCCESS — it flows into
    # the failure branch below via the outcome check.
    success_outcomes = {Outcome.SUCCESS.value, Outcome.SUCCESS_WITH_RECOVERY.value}
    run_failed = not result.get("success") or result.get("outcome") not in success_outcomes

    compress_llm = build_llm(config, [])
    entries = process_trace(
        session_id,
        store,
        compress_llm,
        guideline_max_tokens=mem_cfg["guideline_max_tokens"],
        similarity_threshold=mem_cfg["similarity_threshold"],
        promotion_threshold_sessions=mem_cfg["promotion_threshold_sessions"],
    )
    if entries:
        print(f"\nSynthesized {len(entries)} guideline(s) from this run:")
        for e in entries:
            print(f"  [{e.kind}] {e.text}")

    if run_failed:
        sys.exit(1)


def cmd_ingest_traces(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    _require_api_key(config["llm"]["api_key_env"])
    configure_logging(args.session_id, verbose=args.verbose)
    mem_cfg = config["memory"]
    store = _open_store(mem_cfg)
    llm = build_llm(config, [])
    entries = process_trace(
        args.session_id,
        store,
        llm,
        guideline_max_tokens=mem_cfg["guideline_max_tokens"],
        similarity_threshold=mem_cfg["similarity_threshold"],
        promotion_threshold_sessions=mem_cfg["promotion_threshold_sessions"],
    )
    print(json.dumps([e.to_payload() for e in entries], indent=2))


def cmd_inspect_memory(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    store = _open_store(config["memory"])
    print(f"tactical: {store.count(kind='tactical')}")
    print(f"strategic: {store.count(kind='strategic')}")
    if args.query:
        for entry, score in store.query(args.query, top_k=args.top_k):
            print(f"  [{entry.kind}] ({score:.2f}) {entry.text}")


def cmd_memory_show(args: argparse.Namespace) -> None:
    """G2: list guidelines in the store, filterable by kind, with --markdown
    output suitable for pasting into a deck/X/blog. By default both tactical
    and strategic are shown."""
    config = load_config(args.config)
    store = _open_store(config["memory"])
    kinds: list[str | None]
    if args.strategic:
        kinds = ["strategic"]
    elif args.tactical:
        kinds = ["tactical"]
    else:
        kinds = ["strategic", "tactical"]

    counts = {k: store.count(kind=k) for k in ("strategic", "tactical")}
    total = counts["strategic"] + counts["tactical"]

    if args.markdown:
        print(f"# fabri memory ({total} guidelines: "
              f"{counts['strategic']} strategic + {counts['tactical']} tactical)\n")
    else:
        print(f"{total} guidelines total "
              f"({counts['strategic']} strategic + {counts['tactical']} tactical)\n")

    for kind in kinds:
        entries = store.iterate(kind=kind, limit=args.limit)
        if not entries:
            continue
        if args.markdown:
            print(f"## {kind} ({len(entries)} shown)\n")
            for e in entries:
                age = ""
                if e.session_ids:
                    age = f"  _(seen in {len(e.session_ids)} session(s), hit_count={e.hit_count})_"
                tools = ""
                if e.tools:
                    tools = f"  `tools: {', '.join(e.tools)}`"
                print(f"- {e.text}{age}{tools}")
            print()
        else:
            print(f"--- {kind} ({len(entries)} shown) ---")
            for e in entries:
                hint = f"hit_count={e.hit_count}, sessions={len(e.session_ids or [])}"
                tools = f", tools={','.join(e.tools)}" if e.tools else ""
                print(f"  • {e.text}\n    ({hint}{tools})")


def cmd_memory_list(args: argparse.Namespace) -> None:
    """Lower-level cousin of memory show: just dump entries as JSONL so a
    pipeline can grep / jq / pipe them. Mirrors `kubectl get -o json` shape."""
    config = load_config(args.config)
    store = _open_store(config["memory"])
    kind = None
    if args.strategic:
        kind = "strategic"
    elif args.tactical:
        kind = "tactical"
    entries = store.iterate(kind=kind, limit=args.limit)
    for e in entries:
        print(json.dumps(e.to_payload()))


def cmd_memory_diff(args: argparse.Namespace) -> None:
    """G3: compare what the memory store learned between two sessions.

    Each guideline carries a `session_ids` list — the sessions that contributed
    to it. We partition every entry into three groups by membership in
    {session_a, session_b}:

    - **shared**: both A and B in session_ids (the guideline recurred in both)
    - **new_in_b**: B in session_ids, A not — what session B taught the agent
    - **only_in_a**: A in session_ids, B not — what session A taught that B
      didn't surface (either A's lesson didn't apply to B, or it's already
      strategic and was retrieved without contributing again)

    Useful as a demo: "look what fabri learned in this 30-minute run."
    """
    config = load_config(args.config)
    store = _open_store(config["memory"])
    a, b = args.session_a, args.session_b
    shared, new_in_b, only_in_a = [], [], []
    for entry in store.iterate():
        sids = set(entry.session_ids or [])
        in_a = a in sids
        in_b = b in sids
        if in_a and in_b:
            shared.append(entry)
        elif in_b:
            new_in_b.append(entry)
        elif in_a:
            only_in_a.append(entry)

    def _render(label: str, entries: list) -> None:
        if args.markdown:
            print(f"## {label} ({len(entries)})\n")
            for e in entries:
                print(f"- [{e.kind}] {e.text}")
            print()
        else:
            print(f"--- {label} ({len(entries)}) ---")
            for e in entries:
                print(f"  [{e.kind}] {e.text}")

    if args.markdown:
        print(f"# memory diff: {a[:8]} → {b[:8]}\n")
    _render(f"new in {b[:8]}", new_in_b)
    _render(f"shared between {a[:8]} and {b[:8]}", shared)
    _render(f"only in {a[:8]}", only_in_a)


def cmd_replay(args: argparse.Namespace) -> None:
    """G5: re-run the task from a prior session against the *current* memory
    state. Prints a before/after table so the user can see whether the memory
    loop actually changed the agent's behaviour.

    Caveats: the LLM is non-deterministic. Outcome / step_count / cost can
    differ even with no memory change. Read the comparison as a directional
    signal, not proof; pair with `fabri.benchmarks.session_delta` for a
    statistically meaningful answer.
    """
    from fabri.orchestrator.traces import read_trace

    original_events = read_trace(args.session_id)
    if not original_events:
        print(f"no trace at .fabri/traces/{args.session_id}.jsonl", file=sys.stderr)
        sys.exit(1)
    start = next((e for e in original_events if e.get("type") == "start"), None)
    if start is None or not start.get("task"):
        print("trace has no start event with a task", file=sys.stderr)
        sys.exit(1)
    task = start["task"]
    final = next((e for e in original_events if e.get("type") == "final"), None)
    usage_evt = next((e for e in original_events if e.get("type") == "usage"), None)
    original_summary = {
        "outcome": (final or {}).get("outcome", "?"),
        "cost_usd": (usage_evt or {}).get("cost_usd"),
        "step_count": (usage_evt or {}).get("step_count", "?"),
    }

    config = load_config(args.config)
    _require_api_key(config["llm"]["api_key_env"])
    new_session_id = str(uuid.uuid4())
    configure_logging(new_session_id, verbose=args.verbose)
    mem_cfg = config["memory"]
    store = _open_store(mem_cfg)
    tools_cfg = config["tools"]
    tools = build_tools(tools_cfg)
    decompose_cfg = tools_cfg["decompose"]
    llm = build_llm(config, build_tool_defs(tools, decompose_cfg))

    print(f"replay task: {task!r}", file=sys.stderr)
    print(f"  original session: {args.session_id[:8]} "
          f"outcome={original_summary['outcome']} "
          f"cost={_fmt_usd_or_dash(original_summary['cost_usd'])} "
          f"steps={original_summary['step_count']}", file=sys.stderr)

    result = run_agent(
        task, llm, tools, store,
        session_id=new_session_id,
        max_steps=config["agent"]["max_steps"],
        top_k=mem_cfg["top_k"],
        max_subquestions=decompose_cfg["max_subquestions"],
        system_prompt=config["agent"].get("system_prompt", ""),
        system_prompt_prefix=config["agent"].get("system_prompt_prefix", ""),
        result_format=tools_cfg.get("result_format", "toon"),
        output_format=config["agent"].get("output_format", "json"),
        decompose_llm=build_decompose_llm(config),
    )
    new_summary = {
        "outcome": result.get("outcome", "?"),
        "cost_usd": (result.get("usage") or {}).get("cost_usd"),
        "step_count": (result.get("usage") or {}).get("step_count", "?"),
    }
    print(f"  replay session:   {new_session_id[:8]} "
          f"outcome={new_summary['outcome']} "
          f"cost={_fmt_usd_or_dash(new_summary['cost_usd'])} "
          f"steps={new_summary['step_count']}", file=sys.stderr)

    if (original_summary["cost_usd"] is not None
            and new_summary["cost_usd"] is not None):
        delta = new_summary["cost_usd"] - original_summary["cost_usd"]
        pct = (delta / original_summary["cost_usd"] * 100.0) if original_summary["cost_usd"] else 0.0
        arrow = "↓" if delta < 0 else "↑" if delta > 0 else "→"
        print(f"\ncost delta: {arrow}{abs(pct):.0f}%", file=sys.stderr)

    print(json.dumps({
        "task": task,
        "original": {**original_summary, "session_id": args.session_id},
        "replay": {**new_summary, "session_id": new_session_id},
    }, indent=2))


def _fmt_usd_or_dash(v) -> str:
    return f"${v:.4f}" if isinstance(v, (int, float)) else "—"


def cmd_tool_init(args: argparse.Namespace) -> None:
    """G14: scaffold a new tool (manifest + executable stub) in the picked
    language. Default target dir: tools/agent_tools next to the cwd."""
    from pathlib import Path as _P
    target = _P(args.dir)
    try:
        result = scaffold_tool(args.language, args.name, target, force=args.force)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    if result["created"]:
        print(f"Scaffolded a {args.language!r} tool {args.name!r} in {target}:")
        for f in result["created"]:
            print(f"  + {f}")
    if result["skipped"]:
        print("\nLeft existing files untouched (pass --force to overwrite):")
        for f in result["skipped"]:
            print(f"  . {f}")
    print(f"\nNext: edit {args.name}.* to your liking, then list this dir in "
          f"`tools.manifest_dir` of your agent.yaml.")


def cmd_report(args: argparse.Namespace) -> None:
    """G6/G7/G8/G20: aggregate JSONL traces into a usage report. Output in
    markdown (default), json, or self-contained HTML."""
    from fabri.reports import aggregate, collect_sessions, render_html, render_json, render_markdown

    since_seconds = None
    if args.since:
        # Accept "7d", "24h", "30m" — humane shorthand.
        unit = args.since[-1].lower()
        try:
            n = float(args.since[:-1])
        except ValueError:
            print(f"--since: expected like '7d', '24h', '30m', got {args.since!r}",
                  file=sys.stderr)
            sys.exit(1)
        multipliers = {"d": 86400, "h": 3600, "m": 60, "s": 1}
        if unit not in multipliers:
            print(f"--since: unknown unit {unit!r} (expected d/h/m/s)", file=sys.stderr)
            sys.exit(1)
        since_seconds = n * multipliers[unit]

    sessions = collect_sessions(since_seconds=since_seconds, limit=args.limit)
    report = aggregate(sessions)

    if args.format == "json":
        output = render_json(report)
    elif args.format == "html":
        output = render_html(report)
    else:
        output = render_markdown(report)

    if args.output:
        from pathlib import Path
        Path(args.output).write_text(output)
        print(f"wrote {args.output} ({len(output)} bytes, {report.session_count} sessions)",
              file=sys.stderr)
    else:
        print(output)


def cmd_admin_config(args: argparse.Namespace) -> None:
    try:
        require_admin(args.admin_token)
    except AdminAuthError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    config = load_config(args.config)
    tools = build_tools(config["tools"])
    print(json.dumps(describe_config(config, tools), indent=2))


def _ts_prefix(ev: dict, t0: float) -> str:
    """Wallclock + relative-delta prefix used by every rendered trace line.
    Trace events always carry `ts` (orchestrator/traces.py), so this is safe
    by default -- "time should just work" without extra flags."""
    import datetime as _dt
    ts = ev.get("ts", t0)
    wall = _dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
    dt = ts - t0
    return f"  {wall} (+{dt:6.2f}s)"


def _wrap_block(text: str, indent: str = "    ", width: int | None = None) -> str:
    """Wrap a (possibly multi-line) text block under a fixed indent. Preserves
    intentional newlines; only the long lines get wrapped."""
    import shutil
    import textwrap
    if width is None:
        width = max(60, shutil.get_terminal_size((100, 20)).columns - len(indent))
    out = []
    for line in text.splitlines() or [text]:
        if not line.strip():
            out.append("")
            continue
        out.extend(textwrap.wrap(line, width=width) or [""])
    return "\n".join(indent + l for l in out)


def _looks_like_code(text: str) -> bool:
    first = next((l for l in text.splitlines() if l.strip()), "")
    return first.lstrip().startswith(("def ", "class ", "import ", "from ", "{", "[", "```"))


def _format_payload(value, max_lines: int = 40) -> str:
    """Pretty-print a JSON-ish payload, truncating to `max_lines` so a giant
    tool result doesn't blow up the viewer (the full payload is still in the
    JSONL on disk)."""
    try:
        s = json.dumps(value, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = repr(value)
    lines = s.splitlines()
    if len(lines) > max_lines:
        omitted = len(lines) - max_lines
        lines = lines[:max_lines] + [f"... ({omitted} more lines truncated; see raw JSONL)"]
    return "\n".join(lines)


def _format_thought_body(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            pretty = _format_payload(json.loads(stripped))
            return "\n".join("    " + l for l in pretty.splitlines())
        except json.JSONDecodeError:
            pass
    if _looks_like_code(stripped):
        return "\n".join("    ┃ " + l for l in stripped.splitlines())
    return _wrap_block(text)


def _render_event(ev: dict, t0: float) -> str:
    kind = ev.get("type", "?")
    prefix = _ts_prefix(ev, t0)
    if kind == "tool_call":
        name = ev.get("name", "?")
        result = ev.get("result", {}) or {}
        ok = result.get("ok")
        tag = ev.get("parallel_group")
        tag_str = f" [{tag}]" if tag else ""
        header = f"{prefix} tool_call {name}{tag_str} ok={ok}"
        parts = [header]
        if ev.get("args"):
            parts.append("    args:")
            parts.append(_wrap_block(_format_payload(ev["args"]), indent="      "))
        if result:
            parts.append("    result:")
            parts.append(_wrap_block(_format_payload(result), indent="      "))
        return "\n".join(parts)
    if kind == "thought":
        body = _format_thought_body(ev.get("text", ""))
        return f"{prefix} thought\n{body}"
    if kind == "step_started":
        return f"{prefix} ── step {ev.get('step')} ──"
    if kind == "step_finished":
        bits = [f"step {ev.get('step')} done"]
        for k in ("elapsed_s", "reason", "tool_count", "tool_failure"):
            if k in ev:
                bits.append(f"{k}={ev[k]}")
        return f"{prefix} ── {' '.join(bits)} ──"
    if kind == "start":
        return f"{prefix} start task={ev.get('task', '')!r}"
    if kind == "final":
        return f"{prefix} final outcome={ev.get('outcome')}\n{_wrap_block(ev.get('text', ''))}"
    if kind in ("failed", "llm_error"):
        return f"{prefix} {kind} reason={ev.get('reason', '')!r}"
    if kind == "ask_user":
        return f"{prefix} ask_user q={ev.get('question', '')!r}"
    if kind == "discrepancy":
        return (
            f"{prefix} discrepancy path={ev.get('path', '')!r} "
            f"reason={ev.get('reason', '')!r}"
        )
    rest = {k: v for k, v in ev.items() if k != "ts"}
    return f"{prefix} {kind} {json.dumps(rest, default=str)[:200]}"


def cmd_traces_show(args: argparse.Namespace) -> None:
    """Pretty-print a session's JSONL trace. The framework already writes
    every step (start / tool_call / thought / final / failed) to
    .fabri/traces/<sid>.jsonl; this is the human-readable reader."""
    from fabri.orchestrator.traces import read_trace, trace_path

    events = read_trace(args.session_id)
    if not events:
        print(f"no events at {trace_path(args.session_id)}", file=sys.stderr)
        sys.exit(1)
    t0 = events[0].get("ts", 0.0)
    for ev in events:
        print(_render_event(ev, t0))


def cmd_traces_tail(args: argparse.Namespace) -> None:
    """Follow a trace file (like `tail -f`), pretty-printing new events as
    they arrive. Useful when the agent is running in another shell and you
    want a live view of its tool calls."""
    import time as _time
    from fabri.orchestrator.traces import trace_path

    path = trace_path(args.session_id)
    if not path.exists():
        path.touch()
    with path.open("r") as f:
        # Seek to end so we only see new events, not historical.
        f.seek(0, 2)
        t_start = _time.time()
        try:
            while True:
                line = f.readline()
                if not line:
                    _time.sleep(0.2)
                    continue
                try:
                    ev = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                print(_render_event(ev, t_start), flush=True)
        except KeyboardInterrupt:
            pass


def cmd_traces_list(args: argparse.Namespace) -> None:
    """List recent traces under $FABRI_HOME/traces (or .fabri/traces) so the
    user can find a session_id without remembering the UUID."""
    from fabri.paths import traces_dir

    d = traces_dir()
    if not d.exists():
        print(f"no traces dir at {d}", file=sys.stderr)
        return
    entries = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in entries[: args.limit]:
        size = p.stat().st_size
        print(f"  {p.stem}  ({size} B)")


def cmd_admin_dashboard(args: argparse.Namespace) -> None:
    try:
        require_admin(args.admin_token)
    except AdminAuthError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    config = load_config(args.config)
    tools = build_tools(config["tools"])
    mem_cfg = config["memory"]
    store = _open_store(mem_cfg)
    print(render_dashboard(config, tools, store))


def main() -> None:
    parser = argparse.ArgumentParser(prog="fabri")
    parser.add_argument(
        "--version",
        action="version",
        version=f"fabri {importlib.metadata.version('fabri')}",
    )
    parser.add_argument("--verbose", action="store_true", help="Log at DEBUG level to the console")
    parser.add_argument("--config", dest="config", default=None, help="Path to an agent.yaml config")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Scaffold a starter fabri project (agent.yaml, tools, docker-compose)")
    p_init.add_argument("dir", nargs="?", default=".", help="Target directory (default: current)")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing files")
    p_init.add_argument(
        "--template",
        choices=sorted(SCAFFOLD_TEMPLATES.keys()),
        default="default",
        help="Starter pack: default (hello), research, code-review, data-cleanup. "
             "Non-default templates use the sqlite-vec backend (no docker required).",
    )
    p_init.set_defaults(func=cmd_init)

    p_run = sub.add_parser("run", help="Run the agent on a task")
    p_run.add_argument("task")
    p_run.add_argument("--session-id", dest="session_id", default=None)
    p_run.add_argument("--ask-user-socket", dest="ask_user_socket", default=None,
                       help="Path to a Unix socket the ask_user tool routes questions to (A1).")
    p_run.set_defaults(func=cmd_run)

    p_ingest = sub.add_parser("ingest-traces", help="Synthesize guidelines from a session's trace")
    p_ingest.add_argument("session_id")
    p_ingest.set_defaults(func=cmd_ingest_traces)

    p_inspect = sub.add_parser("inspect-memory", help="Inspect stored memory, optionally querying it")
    p_inspect.add_argument("query", nargs="?", default=None)
    p_inspect.add_argument("--top-k", dest="top_k", type=int, default=5)
    p_inspect.set_defaults(func=cmd_inspect_memory)

    p_mem = sub.add_parser("memory", help="Show or list stored guidelines")
    mem_sub = p_mem.add_subparsers(dest="memory_command", required=True)

    p_mem_show = mem_sub.add_parser("show", help="Human-readable listing of guidelines")
    p_mem_show.add_argument("--strategic", action="store_true", help="Only strategic guidelines")
    p_mem_show.add_argument("--tactical", action="store_true", help="Only tactical guidelines")
    p_mem_show.add_argument("--limit", type=int, default=50)
    p_mem_show.add_argument("--markdown", action="store_true", help="Render as markdown (paste into a deck)")
    p_mem_show.set_defaults(func=cmd_memory_show)

    p_mem_list = mem_sub.add_parser("list", help="JSONL listing of guidelines (pipeable)")
    p_mem_list.add_argument("--strategic", action="store_true")
    p_mem_list.add_argument("--tactical", action="store_true")
    p_mem_list.add_argument("--limit", type=int, default=None)
    p_mem_list.set_defaults(func=cmd_memory_list)

    p_mem_diff = mem_sub.add_parser("diff", help="Compare guidelines between two sessions")
    p_mem_diff.add_argument("session_a", help="The earlier session id")
    p_mem_diff.add_argument("session_b", help="The later session id")
    p_mem_diff.add_argument("--markdown", action="store_true", help="Render as markdown")
    p_mem_diff.set_defaults(func=cmd_memory_diff)

    p_replay = sub.add_parser("replay", help="Re-run a past session's task with current memory state")
    p_replay.add_argument("session_id")
    p_replay.set_defaults(func=cmd_replay)

    p_tool = sub.add_parser("tool", help="Tool-related helpers (scaffold a new tool)")
    tool_sub = p_tool.add_subparsers(dest="tool_command", required=True)

    p_tool_init = tool_sub.add_parser("init", help="Scaffold a new tool")
    p_tool_init.add_argument("language", choices=SUPPORTED_LANGUAGES,
                             help="Language of the new tool's executable")
    p_tool_init.add_argument("name", help="Tool name (alphanumeric + underscore)")
    p_tool_init.add_argument("--dir", default="tools/agent_tools",
                             help="Where to write the manifest + executable (default: tools/agent_tools)")
    p_tool_init.add_argument("--force", action="store_true",
                             help="Overwrite existing files")
    p_tool_init.set_defaults(func=cmd_tool_init)

    p_report = sub.add_parser("report", help="Aggregate cost/outcome across recent sessions")
    p_report.add_argument("--since", default=None,
                          help="Only sessions in the last X (e.g. 7d, 24h, 30m). Default: all.")
    p_report.add_argument("--limit", type=int, default=None,
                          help="Cap to the N most recent sessions after --since.")
    p_report.add_argument("--format", choices=["md", "json", "html"], default="md",
                          help="Output format. md (default) is human-readable; html is self-contained.")
    p_report.add_argument("--output", "-o", default=None,
                          help="Write to this file instead of stdout (always used with --format html).")
    p_report.set_defaults(func=cmd_report)

    # Gated by require_admin() — stub shared-secret check (FABRI_ADMIN_TOKEN),
    # not real auth. See admin.py.
    p_admin = sub.add_parser("admin", help="Admin-only: inspect a config and its resolved tools/memory")
    p_admin.add_argument("--admin-token", dest="admin_token", default=None)
    admin_sub = p_admin.add_subparsers(dest="admin_command", required=True)

    p_admin_config = admin_sub.add_parser("config", help="Print the merged config + resolved tool registry as JSON")
    p_admin_config.set_defaults(func=cmd_admin_config)

    p_admin_dash = admin_sub.add_parser("dashboard", help="Human-readable summary: agent, tools, memory counts")
    p_admin_dash.set_defaults(func=cmd_admin_dashboard)

    p_traces = sub.add_parser("traces", help="Inspect JSONL traces under $FABRI_HOME/traces (homegrown observability spine)")
    traces_sub = p_traces.add_subparsers(dest="traces_command", required=True)

    p_traces_show = traces_sub.add_parser("show", help="Pretty-print a session's trace")
    p_traces_show.add_argument("session_id")
    p_traces_show.set_defaults(func=cmd_traces_show)

    p_traces_tail = traces_sub.add_parser("tail", help="Follow a session's trace as it's written")
    p_traces_tail.add_argument("session_id")
    p_traces_tail.set_defaults(func=cmd_traces_tail)

    p_traces_list = traces_sub.add_parser("list", help="List recent session traces")
    p_traces_list.add_argument("--limit", type=int, default=20)
    p_traces_list.set_defaults(func=cmd_traces_list)

    args = parser.parse_args()
    try:
        args.func(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
