import argparse
import json
import os
import sys
import uuid

from agent_memory.admin import AdminAuthError, describe_config, render_dashboard, require_admin
from agent_memory.config import load_config
from agent_memory.core.agent import run_agent
from agent_memory.core.logging_setup import configure_logging
from agent_memory.memory.store import QdrantMemoryStore
from agent_memory.orchestrator.pipeline import process_trace
from agent_memory.runtime import build_llm, build_tool_defs, build_tools


def _require_api_key(api_key_env: str) -> None:
    if not os.environ.get(api_key_env):
        print(
            f"{api_key_env} is not set. Export it before running the live agent, "
            f"e.g.: export {api_key_env}=sk-...",
            file=sys.stderr,
        )
        sys.exit(1)


def cmd_run(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    _require_api_key(config["llm"]["api_key_env"])
    session_id = args.session_id or str(uuid.uuid4())
    configure_logging(session_id, verbose=args.verbose)

    mem_cfg = config["memory"]
    store = QdrantMemoryStore(url=mem_cfg["qdrant_url"], collection=mem_cfg["collection"])

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
    )
    print(json.dumps(result, indent=2))

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


def cmd_ingest_traces(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    _require_api_key(config["llm"]["api_key_env"])
    configure_logging(args.session_id, verbose=args.verbose)
    mem_cfg = config["memory"]
    store = QdrantMemoryStore(url=mem_cfg["qdrant_url"], collection=mem_cfg["collection"])
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
    mem_cfg = config["memory"]
    store = QdrantMemoryStore(url=mem_cfg["qdrant_url"], collection=mem_cfg["collection"])
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


def cmd_admin_dashboard(args: argparse.Namespace) -> None:
    try:
        require_admin(args.admin_token)
    except AdminAuthError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    config = load_config(args.config)
    tools = build_tools(config["tools"])
    mem_cfg = config["memory"]
    store = QdrantMemoryStore(url=mem_cfg["qdrant_url"], collection=mem_cfg["collection"])
    print(render_dashboard(config, tools, store))


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent-memory")
    parser.add_argument("--verbose", action="store_true", help="Log at DEBUG level to the console")
    parser.add_argument("--config", dest="config", default=None, help="Path to an agent.yaml config")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run the agent on a task")
    p_run.add_argument("task")
    p_run.add_argument("--session-id", dest="session_id", default=None)
    p_run.set_defaults(func=cmd_run)

    p_ingest = sub.add_parser("ingest-traces", help="Synthesize guidelines from a session's trace")
    p_ingest.add_argument("session_id")
    p_ingest.set_defaults(func=cmd_ingest_traces)

    p_inspect = sub.add_parser("inspect-memory", help="Inspect stored memory, optionally querying it")
    p_inspect.add_argument("query", nargs="?", default=None)
    p_inspect.add_argument("--top-k", dest="top_k", type=int, default=5)
    p_inspect.set_defaults(func=cmd_inspect_memory)

    # admin: config/dashboard inspection. Gated by require_admin() -- a stub
    # shared-secret check (AGENT_ADMIN_TOKEN), not real auth. See admin.py.
    p_admin = sub.add_parser("admin", help="Admin-only: inspect a config and its resolved tools/memory")
    p_admin.add_argument("--admin-token", dest="admin_token", default=None)
    admin_sub = p_admin.add_subparsers(dest="admin_command", required=True)

    p_admin_config = admin_sub.add_parser("config", help="Print the merged config + resolved tool registry as JSON")
    p_admin_config.set_defaults(func=cmd_admin_config)

    p_admin_dash = admin_sub.add_parser("dashboard", help="Human-readable summary: agent, tools, memory counts")
    p_admin_dash.set_defaults(func=cmd_admin_dashboard)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
