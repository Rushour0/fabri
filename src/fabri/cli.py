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
from fabri.memory.store import QdrantMemoryStore
from fabri.orchestrator.pipeline import process_trace
from fabri.runtime import build_decompose_llm, build_llm, build_tool_defs, build_tools
from fabri.scaffold import next_steps, scaffold


def cmd_init(args: argparse.Namespace) -> None:
    result = scaffold(args.dir, force=args.force)
    where = "current directory" if args.dir in (".", "") else args.dir
    if result["created"]:
        print(f"Scaffolded a starter fabri project in {where}:")
        for rel in result["created"]:
            print(f"  + {rel}")
    if result["skipped"]:
        print("\nLeft existing files untouched (pass --force to overwrite):")
        for rel in result["skipped"]:
            print(f"  . {rel}")
    print("\n" + next_steps(args.dir))


def _require_api_key(api_key_env: str) -> None:
    if not os.environ.get(api_key_env):
        print(
            f"{api_key_env} is not set. Export it before running the live agent, "
            f"e.g.: export {api_key_env}=<your-api-key>",
            file=sys.stderr,
        )
        sys.exit(1)


def _open_store(mem_cfg: dict) -> QdrantMemoryStore:
    """Open Qdrant with a user-friendly error if it's unreachable, instead of
    leaking the raw qdrant_client/grpc traceback."""
    try:
        return QdrantMemoryStore(url=mem_cfg["qdrant_url"], collection=mem_cfg["collection"])
    except Exception as e:
        print(
            f"Could not reach Qdrant at {mem_cfg['qdrant_url']}: {e}\n"
            f"Start it with: docker compose up -d  (or `docker run -p 6333:6333 qdrant/qdrant`).",
            file=sys.stderr,
        )
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
    )
    print(json.dumps(result, indent=2))
    # An LLM-error / max-steps / no-final outcome must surface to the
    # process exit code -- host services dispatch on `fabri run`'s
    # returncode to decide whether to record a run as succeeded. Without
    # this, an Anthropic rate-limit failure still exits 0 and downstream
    # ledgers (e.g. ludexel's `runs` collection) mark the run succeeded
    # despite having no final_text.
    run_failed = not result.get("success") or result.get("outcome") != "succeeded"

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


def cmd_admin_config(args: argparse.Namespace) -> None:
    try:
        require_admin(args.admin_token)
    except AdminAuthError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    config = load_config(args.config)
    tools = build_tools(config["tools"])
    print(json.dumps(describe_config(config, tools), indent=2))


def cmd_traces_show(args: argparse.Namespace) -> None:
    """Pretty-print a session's JSONL trace. The framework already writes
    every step (start / tool_call / final / failed) to .fabri/traces/<sid>.jsonl;
    this is just the reader so a user can debug an agent run without
    grepping JSON by hand. Doubles as the 'khudka' observability spine."""
    from fabri.orchestrator.traces import read_trace, trace_path

    events = read_trace(args.session_id)
    if not events:
        print(f"no events at {trace_path(args.session_id)}", file=sys.stderr)
        sys.exit(1)
    t0 = events[0].get("ts", 0.0)
    for ev in events:
        dt = ev.get("ts", t0) - t0
        kind = ev.get("type", "?")
        if kind == "tool_call":
            name = ev.get("name", "?")
            ok = ev.get("result", {}).get("ok")
            tag = ev.get("parallel_group")
            tag_str = f" [{tag}]" if tag else ""
            print(f"  +{dt:6.2f}s tool_call {name}{tag_str} ok={ok}")
        elif kind == "start":
            print(f"  +{dt:6.2f}s start task={ev.get('task', '')[:80]!r}")
        elif kind == "final":
            print(f"  +{dt:6.2f}s final outcome={ev.get('outcome')} text={ev.get('text', '')[:120]!r}")
        elif kind == "failed":
            print(f"  +{dt:6.2f}s failed reason={ev.get('reason')!r}")
        elif kind == "ask_user":
            print(f"  +{dt:6.2f}s ask_user q={ev.get('question', '')[:80]!r}")
        else:
            print(f"  +{dt:6.2f}s {kind} {json.dumps({k: v for k, v in ev.items() if k != 'ts'})[:120]}")


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
                dt = ev.get("ts", t_start) - t_start
                kind = ev.get("type", "?")
                summary = ev.get("name") or ev.get("outcome") or ev.get("reason") or ""
                tag = ev.get("parallel_group")
                tag_str = f" [{tag}]" if tag else ""
                print(f"  +{dt:6.2f}s {kind}{tag_str} {summary}", flush=True)
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

    # admin: config/dashboard inspection. Gated by require_admin() -- a stub
    # shared-secret check (FABRI_ADMIN_TOKEN), not real auth. See admin.py.
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
