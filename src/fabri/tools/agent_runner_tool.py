"""The executable side of the agent-as-tool adapter (see agent_tool.py).
Invoked as: python3 agent_runner_tool.py <config_path>, with {"task": "..."}
on stdin -- the same stdin-JSON-in/stdout-JSON-out contract every other tool
uses, so the calling agent's ToolRegistry/runner doesn't need to know this
"tool" is itself a whole agent."""
import argparse
import json
import os
import sys

from fabri.config import load_config
from fabri.core.agent import run_agent
from fabri.core.outcome import Outcome
from fabri.core.run_config import AgentRunConfig
from fabri.memory.action_mining import (
    action_candidate_text,
    build_truncation_action_candidate,
    observed_max_token_retries,
)
from fabri.memory.pruning import ingest_guideline
from fabri.memory.recurrence import fingerprint
from fabri.orchestrator.action_detection import detect_proposed_actions, resolve_action_scope
from fabri.orchestrator.action_execution import apply_safe_memory_actions
from fabri.orchestrator.pipeline import process_trace
from fabri.orchestrator.traces import trace_path
from fabri.runtime import build_llm, build_memory_store, build_run_llms, build_tool_defs, build_tools


class _JSONArgumentParser(argparse.ArgumentParser):
    """argparse exits 2 + stderr on bad args; this tool's contract is JSON on
    stdout, exit 1. Override `error` so usage failures stay on-contract."""

    def error(self, message: str) -> None:  # noqa: D401 - argparse override
        print(json.dumps({"error": f"usage: {self.format_usage().strip()} ({message})"}))
        sys.exit(1)


def main() -> int:
    parser = _JSONArgumentParser(prog="agent_runner_tool")
    parser.add_argument("config_path")
    parser.add_argument("--model", default=None, help="Override sub-agent llm.model")
    parser.add_argument("--max-tokens", dest="max_tokens", type=int, default=None,
                        help="Override sub-agent llm.max_tokens")
    parser.add_argument("--qdrant-url", dest="qdrant_url", default=None,
                        help="Override sub-agent memory.qdrant_url")
    parser.add_argument("--memory-collection", dest="memory_collection", default=None,
                        help="Override sub-agent memory.collection")
    parser.add_argument("--system-prompt", dest="system_prompt", default=None,
                        help="Override sub-agent agent.system_prompt (inline string). F1.")
    parser.add_argument("--system-prompt-file", dest="system_prompt_file", default=None,
                        help="Override sub-agent agent.system_prompt with file contents. F1.")
    parser.add_argument("--ask-user-socket", dest="ask_user_socket", default=None,
                        help="Path to a Unix socket the ask_user tool routes questions to (A1).")
    cli_args = parser.parse_args()
    if cli_args.ask_user_socket is not None:
        # Tools inherit this via os.environ; run_tool's extra_env only
        # carries sandbox-root, so plain inheritance is enough here.
        os.environ["FABRI_ASK_USER_SOCKET"] = cli_args.ask_user_socket
    args = json.loads(sys.stdin.read())
    config = load_config(cli_args.config_path)
    if cli_args.model is not None:
        config["llm"]["model"] = cli_args.model
    if cli_args.max_tokens is not None:
        config["llm"]["max_tokens"] = cli_args.max_tokens
    if cli_args.qdrant_url is not None:
        config["memory"]["qdrant_url"] = cli_args.qdrant_url
    if cli_args.memory_collection is not None:
        config["memory"]["collection"] = cli_args.memory_collection
    if cli_args.system_prompt is not None and cli_args.system_prompt_file is not None:
        print(json.dumps({"error": "pass --system-prompt OR --system-prompt-file, not both"}))
        return 1
    if cli_args.system_prompt is not None:
        config.setdefault("agent", {})["system_prompt"] = cli_args.system_prompt
    elif cli_args.system_prompt_file is not None:
        from pathlib import Path as _P
        try:
            config.setdefault("agent", {})["system_prompt"] = _P(cli_args.system_prompt_file).read_text()
        except OSError as e:
            print(json.dumps({"error": f"failed to read --system-prompt-file: {e}"}))
            return 1

    mem_cfg = config["memory"]
    store = build_memory_store(mem_cfg)
    tools_cfg = config["tools"]
    applied_memory_actions: list[dict[str, object]] = []
    if mem_cfg.get("memory_action_enabled", False):
        try:
            company, agency = resolve_action_scope(mem_cfg)
            proposals = detect_proposed_actions(
                store,
                tools_cfg.get("agents") or [],
                args["task"],
                company=company or "",
                agency=agency or "",
                top_n=8,
            )
            if proposals and mem_cfg.get("memory_action_apply_enabled", False):
                applied_memory_actions = [
                    action.to_dict()
                    for action in apply_safe_memory_actions(
                        proposals,
                        tools_cfg.get("agents") or [],
                    )
                ]
            elif proposals:
                print(
                    f"[fabri] shadow ActionMemory proposals detected: {len(proposals)}",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(f"[fabri] ActionMemory preparation failed closed: {exc}", file=sys.stderr)

    # Build delegated tools only after applicable safe actions have updated
    # their compiled configs. This makes the very next child invocation use the
    # remembered recovery without mutating source rosters.
    tools = build_tools(tools_cfg)
    decompose_cfg = tools_cfg["decompose"]
    llms = build_run_llms(config, build_tool_defs(tools, decompose_cfg))

    # agent.subagent.{max_steps,max_cost_usd} override the parent budget
    # for this child only; absent fields fall back to the parent values.
    # This entrypoint always runs as a sub-agent, so the override fires
    # unconditionally here. Every other knob (planner, retrieval, prompts)
    # is inherited so a child orchestrates the way the parent config says.
    subagent_cfg = config["agent"].get("subagent") or {}
    sub_max_steps = subagent_cfg.get("max_steps")
    if sub_max_steps is None:
        sub_max_steps = config["agent"]["max_steps"]
    sub_max_cost = subagent_cfg.get("max_cost_usd")
    if sub_max_cost is None:
        sub_max_cost = config["agent"].get("max_cost_usd")

    run_cfg = AgentRunConfig.from_config(config).for_subagent(sub_max_steps, sub_max_cost)

    result = run_agent(
        args["task"],
        llms["llm"],
        tools,
        store,
        decompose_llm=llms["decompose_llm"],
        planner_llm=llms["planner_llm"],
        narrator_llm=llms["narrator_llm"],
        **run_cfg.as_kwargs(),
    )
    if applied_memory_actions:
        result["memory_actions_applied"] = applied_memory_actions

    # Root-cause fix: mine THIS child's own trace into the same memory store,
    # mirroring cli.py's cmd_run path -- otherwise a delegated sub-agent's
    # trace is discarded and only the parent's thin trace ever gets mined,
    # capping the guidelines a company can ever learn.
    mining_enabled = bool(mem_cfg.get("mining_enabled", True)) and not os.environ.get(
        "FABRI_DISABLE_SUBAGENT_MINING"
    )
    if not mining_enabled:
        pass  # EXPERIMENT-ONLY escape hatch for a mining-off benchmark arm
    else:
        try:
            # Dedicated compression LLM, empty tool list -- do NOT reuse
            # `llms["llm"]`, which carries the full tool schema on every call.
            compress_llm = build_llm(config, [])
            _eviction_half_life = (
                mem_cfg.get("eviction_half_life_days")
                or mem_cfg.get("temporal_half_life_days", 30.0)
            )
            process_trace(
                result["session_id"],
                store,
                compress_llm,
                guideline_max_tokens=mem_cfg["guideline_max_tokens"],
                similarity_threshold=mem_cfg["similarity_threshold"],
                promotion_threshold_sessions=mem_cfg["promotion_threshold_sessions"],
                record_postmortem=mem_cfg.get("record_postmortems", False),
                success_pattern_requires_evidence=mem_cfg.get("success_pattern_requires_evidence", False),
                max_entries=mem_cfg.get("max_entries"),
                eviction_half_life_days=float(_eviction_half_life),
                eviction_strategy=mem_cfg.get("eviction_strategy", "delete"),
                producer_agent_id=config.get("agent", {}).get("name"),
                memory_scope=mem_cfg.get("scope", "agent"),
            )
        except Exception as e:
            # Deliberate deviation from cli.py's cmd_run: there, a mining
            # failure is allowed to propagate (it's the whole `fabri run`
            # process). Here mining is a bonus side-effect of spawn_subagent,
            # so a mining bug must never flip this tool's success/exit signal
            # -- log to stderr and continue.
            print(f"[fabri] subagent trace mining failed: {e}", file=sys.stderr)

        try:
            candidate = build_truncation_action_candidate(
                config,
                observed_max_token_retries(result),
                str(result.get("outcome", "unknown")),
                args["task"],
            )
            if candidate is not None:
                evidence = candidate.get("evidence")
                if isinstance(evidence, dict):
                    evidence["source_session_ids"] = [result["session_id"]]
                ingest_guideline(
                    store,
                    action_candidate_text(candidate),
                    session_id=result["session_id"],
                    kind="success_pattern",
                    tier="quarantine",
                    resolution=candidate,
                    dedup_key=fingerprint(candidate["problem_signature"]),
                    similarity_threshold=1.01,
                    producer_agent_id=config.get("agent", {}).get("name"),
                    scope=mem_cfg.get("scope", "agent"),
                )
        except Exception as exc:
            print(f"[fabri] subagent ActionMemory mining failed: {exc}", file=sys.stderr)

    # Surface session_id + trace path so a parent agent / human reader can
    # find the child's JSONL when a sub-agent fails. `usage.total_cost_usd`
    # carries own tokens + grandchildren; the parent's dispatch loop reads
    # it to roll this subtree into its own COGS.
    print(json.dumps({
        "final_text": result["final_text"],
        "structured_output": result.get("structured_output"),
        "outcome": result["outcome"],
        "session_id": result["session_id"],
        "trace_path": str(trace_path(result["session_id"])),
        "usage": result.get("usage"),
        "memory_actions_applied": result.get("memory_actions_applied", []),
    }))
    return 0 if result["outcome"] in (Outcome.SUCCESS.value, Outcome.SUCCESS_WITH_RECOVERY.value) else 1


if __name__ == "__main__":
    sys.exit(main())
