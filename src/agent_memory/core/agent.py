import json
import time
import uuid

from agent_memory.core.decompose import DEFAULT_MAX_SUBQUESTIONS, decompose
from agent_memory.core.llm import LLMBackend, LLMError, ToolCall
from agent_memory.core.logging_setup import get_logger
from agent_memory.toon import encode as toon_encode
from agent_memory.core.outcome import Outcome
from agent_memory.memory.store import QdrantMemoryStore
from agent_memory.orchestrator.retrieval import DEFAULT_TOP_K, retrieve_context
from agent_memory.orchestrator.traces import log_event
from agent_memory.tools.registry import ToolRegistry

MAX_STEPS = 10
DECOMPOSE_TOOL_NAME = "decompose"

logger = get_logger()


class AgentProtocolError(RuntimeError):
    """Raised when an LLMBackend returns no tool calls and no usable final text
    (None or empty) -- a malformed response that would otherwise silently burn
    every remaining step before declaring INCOMPLETE with no diagnostic of why.
    (An unrecoverable *provider* error is a different thing: core.llm.LLMError,
    which the loop maps to Outcome.FAILED rather than raising.)"""


DEFAULT_AGENT_IDENTITY = "You are an autonomous agent. Use tools when needed, and stop once the task is done."

TOON_RESULT_NOTE = (
    "Tool results are given to you in TOON, a compact format: objects are `key: value` "
    "lines; arrays are `name[N]: v1,v2,...`, or a table `name[N]{f1,f2}:` followed by one "
    "comma-separated row per element. Read it as structured data; keep calling tools and "
    "answering normally."
)


def build_system_prompt(
    context_block: str,
    tool_descriptions: str,
    *,
    system_prompt: str = "",
    system_prompt_prefix: str = "",
    result_format: str = "json",
) -> str:
    # `system_prompt` (when set) replaces the framework's generic identity line
    # entirely -- domain agents use this to inject "You are the story_agent..."
    # with format pointers and few-shots. `system_prompt_prefix` (when set) is
    # prepended verbatim; useful for global notes that apply across many configs.
    # Both empty -> identical to pre-patch behavior.
    identity = system_prompt or DEFAULT_AGENT_IDENTITY
    parts = [
        system_prompt_prefix,
        identity,
        f"Available tools:\n{tool_descriptions}" if tool_descriptions else "",
        TOON_RESULT_NOTE if result_format == "toon" else "",
        context_block,
    ]
    return "\n\n".join(p for p in parts if p)


def _encode_result(result: dict, result_format: str) -> str:
    """Serialize a tool result for the model. TOON saves input tokens; we never
    let an encode error break the loop -- fall back to JSON."""
    if result_format == "toon":
        try:
            return toon_encode(result)
        except Exception:  # pragma: no cover - defensive, encode handles all JSON shapes
            logger.warning("toon encode failed for a tool result; falling back to JSON")
    return json.dumps(result)


def run_agent(
    task: str,
    llm: LLMBackend,
    tools: ToolRegistry,
    store: QdrantMemoryStore,
    session_id: str | None = None,
    max_steps: int = MAX_STEPS,
    top_k: int = DEFAULT_TOP_K,
    max_subquestions: int = DEFAULT_MAX_SUBQUESTIONS,
    system_prompt: str = "",
    system_prompt_prefix: str = "",
    result_format: str = "toon",
    output_format: str = "json",
) -> dict:
    # result_format: how tool results are serialized INTO the model's context
    #   (toon = fewer input tokens; we control this end so it's reliability-free).
    # output_format: the format the model is asked to PRODUCE structured output in
    #   (decompose). Defaults to json for reliability; toon is opt-in and always
    #   falls back to json parsing. Native tool-call args are always provider JSON.
    session_id = session_id or str(uuid.uuid4())
    logger.info("agent run starting: task=%r session_id=%s", task, session_id)

    context_block = retrieve_context(store, task, top_k=top_k, tool_names=[t.name for t in tools.list()])
    tool_descriptions = "\n".join(f"- {t.name}: {t.description}" for t in tools.list())
    system = build_system_prompt(
        context_block,
        tool_descriptions,
        system_prompt=system_prompt,
        system_prompt_prefix=system_prompt_prefix,
        result_format=result_format,
    )

    log_event(session_id, {"type": "start", "task": task, "context_block": context_block})

    messages = [{"role": "user", "content": task}]
    final_text = None
    success = False
    failed = False
    error_reason = None
    had_tool_failure = False

    for step_num in range(max_steps):
        logger.debug("step %d: calling llm", step_num)
        t0 = time.monotonic()
        try:
            response = llm.step(system, messages)
            if response.tool_calls:
                had_tool_failure |= _dispatch_tool_calls(
                    response.tool_calls, tools, llm, task, max_subquestions,
                    session_id, messages, step_num, result_format, output_format,
                )
                continue
        except LLMError as e:
            # Unrecoverable provider problem (API error, rate limit, truncated
            # response), including one raised by a decompose() sub-call. End the
            # run as FAILED rather than crashing the caller with a raw traceback.
            failed = True
            error_reason = str(e)
            logger.error("step %d: unrecoverable llm error: %s", step_num, e)
            log_event(session_id, {"type": "error", "reason": error_reason, "outcome": Outcome.FAILED.value})
            break
        logger.debug("step %d: llm responded in %.2fs", step_num, time.monotonic() - t0)

        if response.final_text:
            final_text = response.final_text
            success = True
            logger.info("step %d: final answer produced", step_num)
            break

        # No tool calls and no usable final text (empty or structurally
        # malformed): raising beats silently burning every remaining step and
        # then reporting an empty answer as success.
        reason = "llm response had no tool calls and no final text"
        logger.error("step %d: %s", step_num, reason)
        log_event(session_id, {"type": "error", "reason": reason, "outcome": Outcome.FAILED.value})
        raise AgentProtocolError(reason)

    outcome = _classify_outcome(success, had_tool_failure, failed)
    logger.info("agent run finished: outcome=%s session_id=%s", outcome.value, session_id)

    if success:
        log_event(session_id, {"type": "final", "text": final_text, "outcome": outcome.value})
    elif failed:
        log_event(session_id, {"type": "failed", "reason": error_reason, "outcome": outcome.value})
    else:
        log_event(session_id, {"type": "incomplete", "reason": "max steps reached", "outcome": outcome.value})

    return {"session_id": session_id, "success": success, "final_text": final_text, "outcome": outcome.value}


def _dispatch_tool_calls(
    calls: list[ToolCall],
    tools: ToolRegistry,
    llm: LLMBackend,
    default_task: str,
    max_subquestions: int,
    session_id: str,
    messages: list[dict],
    step_num: int,
    result_format: str = "toon",
    output_format: str = "json",
) -> bool:
    """Run every tool call the model emitted this turn (a model may emit
    several in parallel), then append exactly one assistant turn echoing all the
    tool_use blocks and one user turn with all the matching tool_result blocks --
    the Anthropic API rejects a tool_use that isn't paired with a tool_result.
    Returns whether any call failed."""
    had_failure = False
    real_ids = all(c.id is not None for c in calls)
    assistant_blocks, result_blocks = [], []
    simple_calls, simple_results = [], []

    for call in calls:
        logger.info("step %d: dispatching tool %s args=%s", step_num, call.name, call.args)
        t0 = time.monotonic()
        if call.name == DECOMPOSE_TOOL_NAME:
            result = decompose(
                llm, call.args.get("task", default_task),
                max_subquestions=max_subquestions, output_format=output_format,
            )
        else:
            result = tools.invoke(call.name, call.args)
        elapsed = time.monotonic() - t0
        logger.info("step %d: tool %s returned ok=%s in %.2fs", step_num, call.name, result.get("ok"), elapsed)
        if not result.get("ok"):
            had_failure = True
            logger.warning("step %d: tool %s failed: %s", step_num, call.name, result.get("error"))

        log_event(session_id, {"type": "tool_call", "name": call.name, "args": call.args, "result": result})

        # The trace keeps the raw dict; only the copy entering the model's context
        # is TOON-encoded (or JSON), so token savings don't cost us a readable log.
        encoded = _encode_result(result, result_format)
        assistant_blocks.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.args})
        result_blocks.append({"type": "tool_result", "tool_use_id": call.id, "content": encoded})
        simple_calls.append(f"[tool_call:{call.name}]")
        simple_results.append(f"[tool_result] {encoded}")

    if real_ids:
        # Real provider tool-use: echo the assistant's tool_use blocks verbatim,
        # then one user turn carrying every correlated tool_result.
        messages.append({"role": "assistant", "content": assistant_blocks})
        messages.append({"role": "user", "content": result_blocks})
    else:
        # ScriptedLLMBackend / id-less path: plain strings are enough.
        messages.append({"role": "assistant", "content": " ".join(simple_calls)})
        messages.append({"role": "user", "content": " ".join(simple_results)})
    return had_failure


def _classify_outcome(success: bool, had_tool_failure: bool, failed: bool) -> Outcome:
    if failed:
        return Outcome.FAILED
    if not success:
        return Outcome.INCOMPLETE
    return Outcome.SUCCESS_WITH_RECOVERY if had_tool_failure else Outcome.SUCCESS
