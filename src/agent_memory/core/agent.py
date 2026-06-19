import json
import time
import uuid

from agent_memory.core.decompose import DEFAULT_MAX_SUBQUESTIONS, decompose
from agent_memory.core.llm import LLMBackend
from agent_memory.core.logging_setup import get_logger
from agent_memory.core.outcome import Outcome
from agent_memory.memory.store import QdrantMemoryStore
from agent_memory.orchestrator.retrieval import DEFAULT_TOP_K, retrieve_context
from agent_memory.orchestrator.traces import log_event
from agent_memory.tools.registry import ToolRegistry

MAX_STEPS = 10
DECOMPOSE_TOOL_NAME = "decompose"

logger = get_logger()


class AgentProtocolError(RuntimeError):
    """Raised when an LLMBackend returns neither a tool_call nor final_text --
    a malformed response that would otherwise silently burn every remaining
    step before declaring INCOMPLETE with no diagnostic of why."""


def build_system_prompt(context_block: str, tool_descriptions: str) -> str:
    parts = [
        "You are an autonomous agent. Use tools when needed, and stop once the task is done.",
        f"Available tools:\n{tool_descriptions}" if tool_descriptions else "",
        context_block,
    ]
    return "\n\n".join(p for p in parts if p)


def run_agent(
    task: str,
    llm: LLMBackend,
    tools: ToolRegistry,
    store: QdrantMemoryStore,
    session_id: str | None = None,
    max_steps: int = MAX_STEPS,
    top_k: int = DEFAULT_TOP_K,
    max_subquestions: int = DEFAULT_MAX_SUBQUESTIONS,
) -> dict:
    session_id = session_id or str(uuid.uuid4())
    logger.info("agent run starting: task=%r session_id=%s", task, session_id)

    context_block = retrieve_context(store, task, top_k=top_k, tool_names=[t.name for t in tools.list()])
    tool_descriptions = "\n".join(f"- {t.name}: {t.description}" for t in tools.list())
    system = build_system_prompt(context_block, tool_descriptions)

    log_event(session_id, {"type": "start", "task": task, "context_block": context_block})

    messages = [{"role": "user", "content": task}]
    final_text = None
    success = False
    had_tool_failure = False

    for step_num in range(max_steps):
        logger.debug("step %d: calling llm", step_num)
        t0 = time.monotonic()
        response = llm.step(system, messages)
        logger.debug("step %d: llm responded in %.2fs", step_num, time.monotonic() - t0)

        if response.tool_call is not None:
            call = response.tool_call
            logger.info("step %d: dispatching tool %s args=%s", step_num, call.name, call.args)
            t0 = time.monotonic()
            if call.name == DECOMPOSE_TOOL_NAME:
                result = decompose(llm, call.args.get("task", task), max_subquestions=max_subquestions)
            else:
                result = tools.invoke(call.name, call.args)
            elapsed = time.monotonic() - t0
            logger.info(
                "step %d: tool %s returned ok=%s in %.2fs", step_num, call.name, result.get("ok"), elapsed
            )
            if not result.get("ok"):
                had_tool_failure = True
                logger.warning("step %d: tool %s failed: %s", step_num, call.name, result.get("error"))

            log_event(session_id, {"type": "tool_call", "name": call.name, "args": call.args, "result": result})

            if call.id is not None:
                # Real Anthropic tool-use requires the assistant's tool_use block to be
                # echoed back verbatim, followed by a tool_result block correlated by id --
                # plain strings (fine for the ScriptedLLMBackend) would 400 against the API.
                messages.append(
                    {
                        "role": "assistant",
                        "content": [{"type": "tool_use", "id": call.id, "name": call.name, "input": call.args}],
                    }
                )
                messages.append(
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": call.id, "content": json.dumps(result)}],
                    }
                )
            else:
                messages.append({"role": "assistant", "content": f"[tool_call:{call.name}]"})
                messages.append({"role": "user", "content": f"[tool_result] {result}"})
            continue

        if response.final_text is not None:
            final_text = response.final_text
            success = True
            logger.info("step %d: final answer produced", step_num)
            break

        # Neither tool_call nor final_text set: a malformed backend response that
        # would otherwise spin silently through every remaining step.
        reason = "llm response had neither tool_call nor final_text"
        logger.error("step %d: %s", step_num, reason)
        log_event(session_id, {"type": "error", "reason": reason, "outcome": Outcome.FAILED.value})
        raise AgentProtocolError(reason)

    outcome = _classify_outcome(success, had_tool_failure)
    logger.info("agent run finished: outcome=%s session_id=%s", outcome.value, session_id)

    if success:
        log_event(session_id, {"type": "final", "text": final_text, "outcome": outcome.value})
    else:
        log_event(session_id, {"type": "incomplete", "reason": "max steps reached", "outcome": outcome.value})

    return {"session_id": session_id, "success": success, "final_text": final_text, "outcome": outcome.value}


def _classify_outcome(success: bool, had_tool_failure: bool) -> Outcome:
    if not success:
        return Outcome.INCOMPLETE
    return Outcome.SUCCESS_WITH_RECOVERY if had_tool_failure else Outcome.SUCCESS
