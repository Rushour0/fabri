import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from fabri.core.decompose import DEFAULT_MAX_SUBQUESTIONS, decompose
from fabri.core.llm import LLMBackend, LLMError, ToolCall
from fabri.core.logging_setup import get_logger
from fabri.toon import encode as toon_encode
from fabri.core.outcome import Outcome
from fabri.memory.store import QdrantMemoryStore
from fabri.orchestrator.retrieval import DEFAULT_TOP_K, retrieve_context
from fabri.orchestrator.traces import log_event
from fabri.tools.registry import ToolRegistry

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
    decompose_llm: LLMBackend | None = None,
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
                    response.tool_calls, tools, decompose_llm or llm, task, max_subquestions,
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


SPAWN_SUBAGENT_TOOL_NAME = "spawn_subagent"


def _index_parallel_groups(calls: list[ToolCall]) -> dict[str, list[int]]:
    """F2: group spawn_subagent call indices by their `parallel_group` arg.
    Only spawn_subagent participates; other tools stay serial. A group with
    one entry is still treated as 'parallel' (it just runs alone) so the
    trace gets the parallel_group tag uniformly.
    """
    groups: dict[str, list[int]] = {}
    for i, call in enumerate(calls):
        if call.name != SPAWN_SUBAGENT_TOOL_NAME:
            continue
        group = call.args.get("parallel_group")
        if not group:
            continue
        groups.setdefault(group, []).append(i)
    return groups


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

    # F2: spawn_subagent calls that share a `parallel_group` arg fan out
    # concurrently; everything else stays serial. Other tool kinds remain
    # serial regardless -- only sub-agent spawns are expected to be slow
    # enough that the overhead of a thread pool is worth it, and they're the
    # tool kind the agent's prompt expects to fan out anyway.
    parallel_indices = _index_parallel_groups(calls)
    parallel_index_set = {i for idxs in parallel_indices.values() for i in idxs}

    def _dispatch_one(call: ToolCall) -> dict:
        if call.name == DECOMPOSE_TOOL_NAME:
            return decompose(
                llm, call.args.get("task", default_task),
                max_subquestions=max_subquestions, output_format=output_format,
            )
        return tools.invoke(call.name, call.args)

    results: dict[int, dict] = {}

    # 1) Serial calls in original order.
    for i, call in enumerate(calls):
        if i in parallel_index_set:
            continue
        logger.info("step %d: dispatching tool %s args=%s", step_num, call.name, call.args)
        t0 = time.monotonic()
        results[i] = _dispatch_one(call)
        elapsed = time.monotonic() - t0
        logger.info("step %d: tool %s returned ok=%s in %.2fs", step_num, call.name, results[i].get("ok"), elapsed)

    # 2) Parallel-group calls, dispatched concurrently by group. Events are
    # logged as each future completes (chronological-completion order), so
    # the trace shows the actual interleaving -- which is the whole point of
    # the F2 acceptance criterion. The assistant/result blocks below are
    # still emitted in original call order so the Anthropic API stays happy.
    for group_name, idx_list in parallel_indices.items():
        if len(idx_list) == 1:
            i = idx_list[0]
            call = calls[i]
            logger.info("step %d: dispatching tool %s (parallel_group=%s) args=%s",
                        step_num, call.name, group_name, call.args)
            t0 = time.monotonic()
            results[i] = _dispatch_one(call)
            logger.info("step %d: tool %s returned ok=%s in %.2fs",
                        step_num, call.name, results[i].get("ok"), time.monotonic() - t0)
            continue
        with ThreadPoolExecutor(max_workers=len(idx_list)) as pool:
            future_to_idx = {
                pool.submit(_dispatch_one, calls[i]): i for i in idx_list
            }
            for future in future_to_idx:
                pass  # submission only; iteration below collects in completion order
            from concurrent.futures import as_completed

            for future in as_completed(future_to_idx):
                i = future_to_idx[future]
                results[i] = future.result()
                logger.info(
                    "step %d: tool %s (parallel_group=%s) returned ok=%s",
                    step_num, calls[i].name, group_name, results[i].get("ok"),
                )

    # 3) Build the message turn + log events in original call order.
    for i, call in enumerate(calls):
        result = results[i]
        if not result.get("ok"):
            had_failure = True
            logger.warning("step %d: tool %s failed: %s", step_num, call.name, result.get("error"))

        event = {"type": "tool_call", "name": call.name, "args": call.args, "result": result}
        if i in parallel_index_set and call.args.get("parallel_group"):
            # F2 tag so trace readers can spot the fan-out and a host UI can
            # group concurrent sub-agent activity.
            event["parallel_group"] = call.args["parallel_group"]
        log_event(session_id, event)

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
        # Distinguish "ran out of steps cleanly" from "every tool failed and we
        # ran out of steps trying" -- the latter is almost always the user's
        # actual bug (bad sandbox path, missing dep, wrong manifest), and
        # collapsing both into INCOMPLETE buries that signal.
        return Outcome.INCOMPLETE_WITH_TOOL_FAILURE if had_tool_failure else Outcome.INCOMPLETE
    return Outcome.SUCCESS_WITH_RECOVERY if had_tool_failure else Outcome.SUCCESS
