import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from fabri.core.decompose import DEFAULT_MAX_SUBQUESTIONS, decompose
from fabri.core.llm import LLMBackend, LLMError, LLMUsage, ToolCall
from fabri.pricing import cost_for
from fabri.core.planner import DEFAULT_MAX_PLAN_ITEMS, PlanItem, plan as run_plan, topological_order
from fabri.core.logging_setup import get_logger
from fabri.events import EventType, StepReason
from fabri.toon import encode as toon_encode
from fabri.core.outcome import Outcome
from fabri.memory.store import QdrantMemoryStore
from fabri.orchestrator.retrieval import (
    DEFAULT_TOOL_TOP_K,
    DEFAULT_TOP_K,
    retrieve_context,
    retrieve_context_with_meta,
    retrieve_tools,
)
from fabri.orchestrator.traces import log_event
from fabri.tools.registry import ToolRegistry

MAX_STEPS = 10
DECOMPOSE_TOOL_NAME = "decompose"
# The orchestrator prompt references these by name, so they must survive
# tool-retrieval filtering even when the task wording doesn't match them.
DEFAULT_ALWAYS_INCLUDE_TOOLS = ("spawn_subagent", "ask_user", "decompose")

logger = get_logger()


class AgentProtocolError(RuntimeError):
    """Raised when an LLMBackend returns no tool calls and no usable final text
    (None or empty) -- a malformed response that would otherwise silently burn
    every remaining step before declaring INCOMPLETE with no diagnostic of why.
    (An unrecoverable *provider* error is a different thing: core.llm.LLMError,
    which the loop maps to Outcome.FAILED rather than raising.)"""


DEFAULT_AGENT_IDENTITY = "You are an autonomous agent. Complete the task, then stop."

# Frugality stance, appended to every run so cost discipline survives a
# custom system_prompt. References no tool names, so it's always safe.
FRUGALITY_POLICY = (
    "Be deliberate: every tool call re-sends the whole context, so prefer one "
    "decisive call over many exploratory ones, and if you can already answer or "
    "act, do that instead of probing. Your step budget is finite."
)

# Curbs gratuitous sub-agent spawning. Gated on spawn_subagent actually
# being in the registry so it never references an absent tool.
DELEGATION_POLICY = (
    "Do the work inline by default. Spawn a sub-agent ONLY for a subtask that is "
    "independent, parallelizable, AND too large for your own context -- never for "
    "sequential/dependent steps or just because the tool exists. A spawn re-runs "
    "the whole loop, so an unnecessary one multiplies cost."
)

# Gated on a code/batch tool actually being present in the registry.
CODE_ACTION_POLICY = (
    "Prefer code as action: when a job needs several operations, do them in one "
    "`python_exec` script (or one `batch` call) that branches over the results, "
    "rather than many separate tool calls."
)

# Appended whenever both edit_file and write_file are registered.
FILE_EDIT_POLICY = (
    "File edit policy: when modifying a file that already exists, prefer "
    "`edit_file` (a unique string replace) over `write_file`. Use `write_file` "
    "only when creating a new file or when more than half the file is "
    "changing. `read_file` supports `line_start`/`line_end` and "
    "`outline_only=true` -- read only the slice you need."
)

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
    identity = system_prompt or DEFAULT_AGENT_IDENTITY
    # Word-boundary match because tool_descriptions is a bullet list
    # ("- write_file: ...") — substring would over-match.
    has_edit_tools = (
        re.search(r"\bedit_file\b", tool_descriptions or "") is not None
        and re.search(r"\bwrite_file\b", tool_descriptions or "") is not None
    )
    desc = tool_descriptions or ""
    has_spawn = re.search(r"\bspawn_subagent\b", desc) is not None
    has_code_action = (
        re.search(r"\bpython_exec\b", desc) is not None
        or re.search(r"\bbatch\b", desc) is not None
    )
    parts = [
        system_prompt_prefix,
        identity,
        f"Available tools:\n{tool_descriptions}" if tool_descriptions else "",
        FILE_EDIT_POLICY if has_edit_tools else "",
        FRUGALITY_POLICY,
        DELEGATION_POLICY if has_spawn else "",
        CODE_ACTION_POLICY if has_code_action else "",
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
    planner_llm: LLMBackend | None = None,
    planner_mode: str = "off",
    planner_max_items: int = DEFAULT_MAX_PLAN_ITEMS,
    planner_auto_token_threshold: int = 80,
    tool_retrieval_enabled: bool = False,
    tool_retrieval_top_k: int = DEFAULT_TOOL_TOP_K,
    tool_retrieval_always_include: tuple[str, ...] = DEFAULT_ALWAYS_INCLUDE_TOOLS,
    max_cost_usd: float | None = None,
    narrator_llm: LLMBackend | None = None,
) -> dict:
    # result_format: tool results -> model context. toon saves input tokens.
    # output_format: the format the model is asked to produce structured
    # output in (decompose). Native tool-call args are always provider JSON.
    session_id = session_id or str(uuid.uuid4())
    logger.info("agent run starting: task=%r session_id=%s", task, session_id)

    context_block, retrieval_meta = retrieve_context_with_meta(
        store, task, top_k=top_k, tool_names=[t.name for t in tools.list()]
    )
    # When retrieval is on, the filtered subset stays constant for the whole
    # run so the prompt cache still hits across steps. The model is given the
    # exact list the backend will accept calls on — a mismatch would prompt
    # calls the backend can't dispatch.
    if tool_retrieval_enabled:
        visible_tools = retrieve_tools(
            task,
            tools,
            top_k=tool_retrieval_top_k,
            always_include=tool_retrieval_always_include,
        )
        filtered_defs = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema or {"type": "object"}}
            for t in visible_tools
        ]
        try:
            llm.set_tools(filtered_defs)
        except AttributeError:
            # Older backend without set_tools(); the system prompt still
            # narrows, but the provider tool list won't.
            logger.warning("backend %s lacks set_tools(); tool-list filtering will not propagate to the provider", type(llm).__name__)
    else:
        visible_tools = tools.list()
    tool_descriptions = "\n".join(f"- {t.name}: {t.description}" for t in visible_tools)
    system = build_system_prompt(
        context_block,
        tool_descriptions,
        system_prompt=system_prompt,
        system_prompt_prefix=system_prompt_prefix,
        result_format=result_format,
    )

    log_event(session_id, {"type": EventType.START.value, "task": task, "context_block": context_block})

    final_text = None
    success = False
    failed = False
    error_reason = None
    had_tool_failure = False
    # Surfaced on terminal INCOMPLETE / FAILED events so a host can show the
    # model's last words on a max-steps run instead of scraping `thought`
    # events. FINAL keeps its existing `text` field and isn't affected.
    last_assistant_text: list[str | None] = [None]
    usage_totals = LLMUsage()
    # Keyed by model id so a mixed-model run (Sonnet orchestrator + Haiku
    # decompose) prices each model at its own rate.
    usage_by_model: dict[str, LLMUsage] = {}
    # spawn_subagent runs in a separate subprocess with its own trace; the
    # only way the parent learns a child's cost is the number reported back
    # through the tool result. Rolling it up here makes the parent's
    # `total_cost_usd` the true end-to-end build cost.
    subagent_cost_total = [0.0]
    step_count = 0
    run_t0 = time.monotonic()

    def _accumulate(resp_usage: LLMUsage | None) -> None:
        if resp_usage is None:
            return
        usage_totals.input_tokens += resp_usage.input_tokens
        usage_totals.output_tokens += resp_usage.output_tokens
        usage_totals.cache_creation_input_tokens += resp_usage.cache_creation_input_tokens
        usage_totals.cache_read_input_tokens += resp_usage.cache_read_input_tokens
        bucket = usage_by_model.setdefault(resp_usage.model or "", LLMUsage(model=resp_usage.model))
        bucket.input_tokens += resp_usage.input_tokens
        bucket.output_tokens += resp_usage.output_tokens
        bucket.cache_creation_input_tokens += resp_usage.cache_creation_input_tokens
        bucket.cache_read_input_tokens += resp_usage.cache_read_input_tokens

    def _budget_breached() -> bool:
        if max_cost_usd is None:
            return False
        own = 0.0
        for bucket in usage_by_model.values():
            c = cost_for(bucket)
            if c is not None:
                own += c
        return (own + subagent_cost_total[0]) >= max_cost_usd

    def _accumulate_subagent_cost(cost: float) -> None:
        subagent_cost_total[0] += cost

    # Fan-out telemetry + delegation-regret detection. Surfaces whether the
    # "single-threaded by default" stance is actually being honoured.
    subagent_stats = {
        "count": 0,
        "successful_count": 0,
        "failed_count": 0,
        "max_subtree_cost_usd": 0.0,
        "regret_count": 0,
    }

    def _on_subagent_finished(call, ok: bool, child_usage: dict | None) -> None:
        subagent_stats["count"] += 1
        if ok:
            subagent_stats["successful_count"] += 1
        else:
            subagent_stats["failed_count"] += 1
        if not child_usage:
            return
        cost = child_usage.get("total_cost_usd")
        if cost is None:
            cost = child_usage.get("cost_usd")
        cost = float(cost or 0.0)
        if cost > subagent_stats["max_subtree_cost_usd"]:
            subagent_stats["max_subtree_cost_usd"] = round(cost, 6)
        # step_count<=1 + cost>0 means the child barely ran but still spent;
        # almost certainly inlinable. Successful spawns only, so a failed
        # spawn's cost isn't double-counted as regret.
        if ok and child_usage.get("step_count", 0) <= 1 and cost > 0:
            subagent_stats["regret_count"] += 1
            log_event(session_id, {
                "type": "delegation_regret",
                "tool": call.name,
                "child_step_count": child_usage.get("step_count", 0),
                "child_cost_usd": cost,
                "reason": "spawn ran <=1 step but cost >0; likely inlinable",
            })

    def _track_last_text(resp) -> None:
        # Only overwrite on a non-empty value so a tools-only step doesn't
        # clobber the prior step's recap.
        if getattr(resp, "final_text", None):
            last_assistant_text[0] = resp.final_text
        elif getattr(resp, "thinking_text", None):
            last_assistant_text[0] = resp.thinking_text

    # Stamped into the last user message on the final step to convert
    # "did the work, ran out of narration budget" runs into clean SUCCESS
    # instead of INCOMPLETE. Idempotent via sentinel substring.
    _FINAL_STEP_NUDGE = (
        "This is your FINAL step in this run. Do NOT call any more tools — "
        "even if the work feels unfinished. Reply now with your final answer "
        "summarising what you did and any open items, so the run ends cleanly."
    )

    def _inject_final_step_nudge(msgs: list[dict]) -> None:
        if not msgs:
            return
        last = msgs[-1]
        if last.get("role") != "user":
            return
        content = last.get("content")
        if isinstance(content, str):
            if _FINAL_STEP_NUDGE in content:
                return
            last["content"] = content + "\n\n" + _FINAL_STEP_NUDGE
            return
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text" \
                        and _FINAL_STEP_NUDGE in (block.get("text") or ""):
                    return
            content.append({"type": "text", "text": _FINAL_STEP_NUDGE})

    # State for the optional Haiku-class narrator. Holds the last emitted
    # update so the next call can reference it and avoid repeating itself.
    narration_state = {"last": None}

    _NARRATOR_SYSTEM = (
        "You are an agent's narrator. In one sentence (<= 12 words), tell the "
        "user what the agent is doing right now. Active voice, present tense, "
        "no preamble (no 'The agent is...'). Never repeat the previous update "
        "verbatim. Plain text only -- no markdown, no quotes."
    )

    def _summarize_call_for_narrator(call: ToolCall) -> str:
        # Trim args to keep the narrator prompt small. The narrator doesn't
        # need full payloads, just enough to pick a verb.
        args = call.args or {}
        snippet = json.dumps(args, default=str)
        if len(snippet) > 160:
            snippet = snippet[:160] + "..."
        return f"{call.name}({snippet})"

    def _emit_narration(calls: list[ToolCall] | None, step: int, kind: str) -> None:
        if narrator_llm is None:
            return
        try:
            if calls:
                tool_lines = "\n".join(f"- {_summarize_call_for_narrator(c)}" for c in calls)
                user = (
                    f"Task: {task[:200]}\n"
                    f"Previous update: {narration_state['last'] or '(none)'}\n"
                    f"Step {step} just dispatched these tools:\n{tool_lines}\n"
                    f"Tell the user what the agent is doing now."
                )
            else:
                user = (
                    f"Task: {task[:200]}\n"
                    f"Previous update: {narration_state['last'] or '(none)'}\n"
                    f"Trigger: {kind}\n"
                    f"Tell the user what the agent is doing now."
                )
            resp = narrator_llm.step(_NARRATOR_SYSTEM, [{"role": "user", "content": user}])
            text = (resp.final_text or "").strip()
            if not text:
                return
            if text == narration_state["last"]:
                return
            narration_state["last"] = text
            _accumulate(resp.usage)
            log_event(session_id, {
                "type": EventType.NARRATION.value,
                "step": step,
                "text": text,
                "trigger": kind,
            })
        except Exception as e:  # narration is best-effort; never break the run
            logger.debug("narrator skipped (%s): %s", type(e).__name__, e)

    def _emit_thought(thinking_text: str | None, step: int) -> None:
        # Whitespace-only thinking is dropped upstream by LLMResponse.
        if thinking_text:
            log_event(session_id, {
                "type": EventType.THOUGHT.value,
                "text": thinking_text,
                "step": step,
            })

    # `auto` engages when the task is long enough to benefit; `force` always
    # engages; `off` keeps the single-loop behaviour. The planner is one LLM
    # call ahead of the loop; the executor runs the step body once per plan
    # item with a minimal per-item user message.
    plan_engaged = planner_mode == "force" or (
        planner_mode == "auto" and len(task) >= planner_auto_token_threshold
    )

    def _run_executor_loop(
        item_messages: list[dict], item_task: str, item_max_steps: int, step_offset: int
    ) -> tuple[str | None, bool, bool, str | None, bool, int]:
        """Run the inner step loop against `item_messages` (mutated in place).
        Returns (final_text, success, failed, error_reason, had_failure, steps_used).
        Raises AgentProtocolError exactly like the historical loop on a
        no-tool-no-text response, so existing callers see no behaviour shift."""
        item_final = None
        item_success = False
        item_failed = False
        item_error = None
        item_had_failure = False
        steps_used = 0
        for inner_step in range(item_max_steps):
            global_step = step_offset + inner_step
            steps_used = inner_step + 1
            t0 = time.monotonic()
            if _budget_breached():
                log_event(session_id, {
                    "type": "budget_exceeded",
                    "step": global_step,
                    "max_cost_usd": max_cost_usd,
                })
                item_failed = True
                item_error = "cost budget exceeded"
                break
            log_event(session_id, {"type": EventType.STEP_STARTED.value, "step": global_step})
            if item_max_steps > 1 and inner_step == item_max_steps - 1:
                _inject_final_step_nudge(item_messages)
            try:
                response = llm.step(system, item_messages)
                _accumulate(response.usage)
                _track_last_text(response)
                if response.tool_calls:
                    _emit_thought(response.thinking_text, global_step)
                    _emit_narration(response.tool_calls, global_step, "tools")
                    step_had_failure = _dispatch_tool_calls(
                        response.tool_calls, tools, decompose_llm or llm, item_task, max_subquestions,
                        session_id, item_messages, global_step, result_format, output_format,
                        on_subagent_cost=_accumulate_subagent_cost,
                        on_subagent_finished=_on_subagent_finished,
                    )
                    item_had_failure |= step_had_failure
                    log_event(session_id, {
                        "type": EventType.STEP_FINISHED.value,
                        "step": global_step,
                        "elapsed_s": round(time.monotonic() - t0, 3),
                        "reason": StepReason.TOOLS.value,
                        "tool_failure": step_had_failure,
                        "tool_count": len(response.tool_calls),
                    })
                    continue
            except LLMError as e:
                item_failed = True
                item_error = str(e)
                logger.error("step %d: unrecoverable llm error: %s", global_step, e)
                log_event(session_id, {"type": EventType.ERROR.value, "reason": item_error, "outcome": Outcome.FAILED.value})
                log_event(session_id, {
                    "type": EventType.STEP_FINISHED.value,
                    "step": global_step,
                    "elapsed_s": round(time.monotonic() - t0, 3),
                    "reason": StepReason.LLM_ERROR.value,
                })
                return item_final, item_success, item_failed, item_error, item_had_failure, steps_used

            if response.final_text:
                _emit_thought(response.thinking_text, global_step)
                item_final = response.final_text
                item_success = True
                logger.info("step %d: final answer produced", global_step)
                log_event(session_id, {
                    "type": EventType.STEP_FINISHED.value,
                    "step": global_step,
                    "elapsed_s": round(time.monotonic() - t0, 3),
                    "reason": StepReason.FINAL.value,
                })
                return item_final, item_success, item_failed, item_error, item_had_failure, steps_used

            reason = "llm response had no tool calls and no final text"
            logger.error(
                "step %d: %s (prior_tool_failure_in_item=%s)",
                global_step, reason, item_had_failure,
            )
            log_event(session_id, {
                "type": EventType.ERROR.value,
                "reason": reason,
                "outcome": Outcome.FAILED.value,
                "had_tool_failure": item_had_failure,
            })
            log_event(session_id, {
                "type": EventType.STEP_FINISHED.value,
                "step": global_step,
                "elapsed_s": round(time.monotonic() - t0, 3),
                "reason": StepReason.PROTOCOL_ERROR.value,
            })
            raise AgentProtocolError(reason)
        return item_final, item_success, item_failed, item_error, item_had_failure, steps_used

    if plan_engaged:
        planner_backend = planner_llm or decompose_llm or llm
        try:
            plan_items = run_plan(task, planner_backend, max_items=planner_max_items)
        except LLMError as e:
            failed = True
            error_reason = f"planner failed: {e}"
            plan_items = []
            log_event(session_id, {"type": EventType.ERROR.value, "reason": error_reason, "outcome": Outcome.FAILED.value})

        if plan_items:
            order = topological_order(plan_items)
            log_event(session_id, {
                "type": EventType.PLAN_STARTED.value,
                "items": [it.to_dict() for it in plan_items],
                "order": order,
            })
            steps_remaining = max_steps
            per_item_outputs: list[str] = []
            completed: list[str] = []
            for plan_idx in order:
                if steps_remaining <= 0 or failed:
                    break
                item = plan_items[plan_idx]
                log_event(session_id, {
                    "type": EventType.PLAN_ITEM_STARTED.value,
                    "index": plan_idx,
                    "goal": item.goal,
                    "artifacts": list(item.artifacts),
                })
                # Per-item context deliberately omits the prior item's full
                # tool_result history — that's the token-cost cut vs. one big
                # loop. Just goal + artefacts + a one-liner per completed item.
                summary = "; ".join(completed) if completed else "(none yet)"
                item_user = (
                    f"Current goal: {item.goal}\n"
                    f"Target artefacts: {', '.join(item.artifacts) or '(none)'}\n"
                    f"Previously completed in this plan: {summary}\n"
                    f"Do this single goal, then reply with a brief confirmation."
                )
                item_messages = [{"role": "user", "content": item_user}]
                item_budget = max(1, steps_remaining // max(1, len(order) - len(per_item_outputs)))
                try:
                    item_final, item_success, item_failed, item_error, item_had_failure, used = _run_executor_loop(
                        item_messages, item.goal, item_budget, step_offset=step_count,
                    )
                except AgentProtocolError:
                    log_event(session_id, {
                        "type": EventType.PLAN_ITEM_FINISHED.value,
                        "index": plan_idx,
                        "ok": False,
                        "reason": "protocol_error",
                    })
                    raise
                step_count += used
                steps_remaining -= used
                had_tool_failure |= item_had_failure
                if item_failed:
                    failed = True
                    error_reason = item_error
                    log_event(session_id, {
                        "type": EventType.PLAN_ITEM_FINISHED.value,
                        "index": plan_idx,
                        "ok": False,
                        "reason": "llm_error",
                    })
                    break
                if item_success and item_final:
                    per_item_outputs.append(item_final)
                    completed.append(f"#{plan_idx} {item.goal}")
                    log_event(session_id, {
                        "type": EventType.PLAN_ITEM_FINISHED.value,
                        "index": plan_idx,
                        "ok": True,
                    })
                else:
                    log_event(session_id, {
                        "type": EventType.PLAN_ITEM_FINISHED.value,
                        "index": plan_idx,
                        "ok": False,
                        "reason": "incomplete",
                    })
            log_event(session_id, {
                "type": EventType.PLAN_FINISHED.value,
                "items_completed": len(per_item_outputs),
                "items_total": len(plan_items),
            })
            if per_item_outputs and not failed:
                final_text = "\n\n".join(per_item_outputs)
                success = len(per_item_outputs) == len(plan_items)
        # Skip the historical single-loop body below by jumping to outcome.
        plan_engaged = True
    else:
        plan_engaged = False

    messages = [{"role": "user", "content": task}]
    # When the planner engaged, the executor has consumed the step budget
    # item-by-item; skip the single-loop body entirely.
    legacy_steps: range | list[int] = [] if plan_engaged else range(max_steps)
    budget_breached_legacy = False
    for step_num in legacy_steps:
        step_count = step_num + 1
        logger.debug("step %d: calling llm", step_num)
        t0 = time.monotonic()
        if _budget_breached():
            budget_breached_legacy = True
            log_event(session_id, {
                "type": "budget_exceeded",
                "step": step_num,
                "max_cost_usd": max_cost_usd,
            })
            break
        log_event(session_id, {"type": EventType.STEP_STARTED.value, "step": step_num})
        if max_steps > 1 and step_num == max_steps - 1:
            _inject_final_step_nudge(messages)
        try:
            response = llm.step(system, messages)
            _accumulate(response.usage)
            _track_last_text(response)
            # Priced from this step's response.usage alone so reports can
            # attribute LLM-call cost to the tools this step dispatched.
            # cost_for returns None on an unknown model — surface that as
            # None too rather than fabricating a 0.
            step_cost = cost_for(response.usage) if response.usage else None
            if response.tool_calls:
                # Emit reasoning BEFORE tool_call events so trace readers see
                # "Let me check X first..." preceding the tool that checks it.
                _emit_thought(response.thinking_text, step_num)
                _emit_narration(response.tool_calls, step_num, "tools")
                step_had_failure = _dispatch_tool_calls(
                    response.tool_calls, tools, decompose_llm or llm, task, max_subquestions,
                    session_id, messages, step_num, result_format, output_format,
                    on_subagent_cost=_accumulate_subagent_cost,
                )
                had_tool_failure |= step_had_failure
                log_event(session_id, {
                    "type": EventType.STEP_FINISHED.value,
                    "step": step_num,
                    "elapsed_s": round(time.monotonic() - t0, 3),
                    "reason": StepReason.TOOLS.value,
                    "tool_failure": step_had_failure,
                    "tool_count": len(response.tool_calls),
                    "cost_usd": step_cost,
                })
                continue
        except LLMError as e:
            # Unrecoverable provider problem (API error, rate limit,
            # truncated response). End as FAILED rather than crashing the
            # caller with a raw traceback.
            failed = True
            error_reason = str(e)
            logger.error("step %d: unrecoverable llm error: %s", step_num, e)
            log_event(session_id, {"type": EventType.ERROR.value, "reason": error_reason, "outcome": Outcome.FAILED.value})
            log_event(session_id, {
                "type": EventType.STEP_FINISHED.value,
                "step": step_num,
                "elapsed_s": round(time.monotonic() - t0, 3),
                "reason": StepReason.LLM_ERROR.value,
            })
            break
        logger.debug("step %d: llm responded in %.2fs", step_num, time.monotonic() - t0)

        if response.final_text:
            _emit_thought(response.thinking_text, step_num)
            final_text = response.final_text
            success = True
            logger.info("step %d: final answer produced", step_num)
            log_event(session_id, {
                "type": EventType.STEP_FINISHED.value,
                "step": step_num,
                "elapsed_s": round(time.monotonic() - t0, 3),
                "reason": StepReason.FINAL.value,
                "cost_usd": step_cost,
            })
            break

        # No tool calls and no usable final text (empty or structurally
        # malformed): raising beats silently burning every remaining step and
        # then reporting an empty answer as success.
        reason = "llm response had no tool calls and no final text"
        logger.error(
            "step %d: %s (prior_tool_failure_in_run=%s)",
            step_num, reason, had_tool_failure,
        )
        log_event(session_id, {
            "type": EventType.ERROR.value,
            "reason": reason,
            "outcome": Outcome.FAILED.value,
            "had_tool_failure": had_tool_failure,
        })
        log_event(session_id, {
            "type": EventType.STEP_FINISHED.value,
            "step": step_num,
            "elapsed_s": round(time.monotonic() - t0, 3),
            "reason": StepReason.PROTOCOL_ERROR.value,
        })
        raise AgentProtocolError(reason)

    # A run that produced final_text before hitting the budget is still
    # SUCCESS; the budget is a backstop, not a retroactive invalidator.
    if not success and (budget_breached_legacy or any(
        e == "cost budget exceeded" for e in [error_reason]
    )):
        outcome = Outcome.BUDGET_EXCEEDED
        failed = True  # treat budget breach as a failure for exit-code purposes
        error_reason = error_reason or "cost budget exceeded"
    else:
        outcome = _classify_outcome(success, had_tool_failure, failed)
    logger.info("agent run finished: outcome=%s session_id=%s", outcome.value, session_id)

    if success:
        log_event(session_id, {"type": EventType.FINAL.value, "text": final_text, "outcome": outcome.value})
    elif failed:
        log_event(session_id, {
            "type": EventType.FAILED.value,
            "reason": error_reason,
            "outcome": outcome.value,
            "text": last_assistant_text[0],
        })
    else:
        log_event(session_id, {
            "type": EventType.INCOMPLETE.value,
            "reason": "max steps reached",
            "outcome": outcome.value,
            "text": last_assistant_text[0],
        })

    # cost_by_model covers own tokens only — a child's cost arrives as one
    # USD figure that can't be re-split by model. cost_usd = own; total_cost_usd
    # = own + sub-agent subtree, the number a host persists as run COGS.
    cost_by_model: dict[str, float] = {}
    own_cost = 0.0
    for model_id, bucket in usage_by_model.items():
        c = cost_for(bucket)
        if c is not None:
            cost_by_model[model_id or "unknown"] = c
            own_cost += c
    own_cost = round(own_cost, 6)
    subagent_cost = round(subagent_cost_total[0], 6)
    # Fraction of retrieved guidelines already confirmed by a prior session
    # (hit_count>=2 or len(session_ids)>=2) — the cross-session reuse signal.
    # None when nothing was retrieved.
    g_retrieved = retrieval_meta.get("retrieved", 0)
    g_from_prior = retrieval_meta.get("from_prior_sessions", 0)
    reuse_rate = round(g_from_prior / g_retrieved, 4) if g_retrieved else None
    usage_dict = {
        "input_tokens": usage_totals.input_tokens,
        "output_tokens": usage_totals.output_tokens,
        "cache_creation_input_tokens": usage_totals.cache_creation_input_tokens,
        "cache_read_input_tokens": usage_totals.cache_read_input_tokens,
        "step_count": step_count,
        "wall_time_s": round(time.monotonic() - run_t0, 3),
        "cost_usd": own_cost,
        "cost_by_model": cost_by_model,
        "subagent_cost_usd": subagent_cost,
        "total_cost_usd": round(own_cost + subagent_cost, 6),
        "guideline_reuse_rate": reuse_rate,
        "guidelines_retrieved": g_retrieved,
        "guidelines_from_prior_sessions": g_from_prior,
        "subagent_count": subagent_stats["count"],
        "subagent_successful_count": subagent_stats["successful_count"],
        "subagent_failed_count": subagent_stats["failed_count"],
        "subagent_max_subtree_cost_usd": subagent_stats["max_subtree_cost_usd"],
        "subagent_regret_count": subagent_stats["regret_count"],
    }
    log_event(session_id, {"type": EventType.USAGE.value, **usage_dict})

    return {
        "session_id": session_id,
        "success": success,
        "final_text": final_text,
        "outcome": outcome.value,
        "usage": usage_dict,
    }


SPAWN_SUBAGENT_TOOL_NAME = "spawn_subagent"


def _index_parallel_groups(calls: list[ToolCall]) -> dict[str, list[int]]:
    """Group spawn_subagent call indices by their `parallel_group` arg.
    Only spawn_subagent participates; other tools stay serial. A group with
    one entry is still tagged 'parallel' so traces get the tag uniformly.
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
    on_subagent_cost: Callable[[float], None] | None = None,
    on_subagent_finished: Callable[..., None] | None = None,
) -> bool:
    """Run every tool call the model emitted this turn (a model may emit
    several in parallel), then append exactly one assistant turn echoing all the
    tool_use blocks and one user turn with all the matching tool_result blocks --
    the Anthropic API rejects a tool_use that isn't paired with a tool_result.
    Returns whether any call failed.

    `on_subagent_cost`, when given, receives each spawned sub-agent's
    end-to-end `total_cost_usd` so the parent can roll the sub-agent subtree
    into its own COGS.

    `on_subagent_finished(call, ok, child_usage)`, when given, fires once
    per spawn_subagent completion (success or failure) with the child's
    usage dict (or None). The parent uses this for fan-out telemetry and
    delegation-regret detection."""
    had_failure = False
    real_ids = all(c.id is not None for c in calls)
    assistant_blocks, result_blocks = [], []
    simple_calls, simple_results = [], []

    # spawn_subagent calls sharing a `parallel_group` arg fan out via
    # threads; other tool kinds stay serial — only sub-agent spawns are
    # slow enough to justify the thread-pool overhead.
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

    def _emit_tool_started(i: int, call: ToolCall, group: str | None) -> None:
        # Lets the host UI flip to a "running …" state before slow tools
        # (spawn_subagent in particular) return. The paired completion
        # event is the existing `tool_call` emitted in section 3 with the
        # full result attached, so existing trace readers stay unaffected.
        event = {
            "type": EventType.TOOL_STARTED.value,
            "step": step_num,
            "call_index": i,
            "name": call.name,
            "args": call.args,
        }
        if group:
            event["parallel_group"] = group
        log_event(session_id, event)

    # 1) Serial calls in original order.
    for i, call in enumerate(calls):
        if i in parallel_index_set:
            continue
        logger.info("step %d: dispatching tool %s args=%s", step_num, call.name, call.args)
        _emit_tool_started(i, call, None)
        t0 = time.monotonic()
        results[i] = _dispatch_one(call)
        elapsed = time.monotonic() - t0
        logger.info("step %d: tool %s returned ok=%s in %.2fs", step_num, call.name, results[i].get("ok"), elapsed)

    # 2) Parallel-group calls. Events log in completion order so the trace
    # shows actual interleaving; assistant/result blocks below still emit
    # in original call order so the Anthropic API stays happy.
    for group_name, idx_list in parallel_indices.items():
        # One event per group naming the fan-out members so a UI can render
        # "running 3 subagents in parallel" without scanning ahead.
        log_event(session_id, {
            "type": EventType.PARALLEL_GROUP_STARTED.value,
            "step": step_num,
            "parallel_group": group_name,
            "calls": [{"call_index": j, "name": calls[j].name} for j in idx_list],
        })
        if len(idx_list) == 1:
            i = idx_list[0]
            call = calls[i]
            logger.info("step %d: dispatching tool %s (parallel_group=%s) args=%s",
                        step_num, call.name, group_name, call.args)
            _emit_tool_started(i, call, group_name)
            t0 = time.monotonic()
            results[i] = _dispatch_one(call)
            logger.info("step %d: tool %s returned ok=%s in %.2fs",
                        step_num, call.name, results[i].get("ok"), time.monotonic() - t0)
            continue
        # Emit tool_started for every fan-out member up-front; the actual
        # completion order is preserved by the as_completed loop below.
        for i in idx_list:
            _emit_tool_started(i, calls[i], group_name)
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

        # Roll the child's end-to-end cost into the parent. Prefer
        # total_cost_usd (includes grandchildren); fall back to cost_usd.
        # A child on an older fabri without cost fields contributes 0.
        if call.name == SPAWN_SUBAGENT_TOOL_NAME:
            ok = bool(result.get("ok"))
            child = result.get("result")
            child_usage = child.get("usage") if isinstance(child, dict) else None
            if on_subagent_cost is not None and ok and isinstance(child_usage, dict):
                child_cost = child_usage.get("total_cost_usd")
                if child_cost is None:
                    child_cost = child_usage.get("cost_usd")
                if isinstance(child_cost, (int, float)):
                    on_subagent_cost(float(child_cost))
            if on_subagent_finished is not None:
                on_subagent_finished(
                    call, ok,
                    child_usage if isinstance(child_usage, dict) else None,
                )

        event = {
            "type": EventType.TOOL_CALL.value,
            "step": step_num,
            "call_index": i,
            "name": call.name,
            "args": call.args,
            "result": result,
        }
        if i in parallel_index_set and call.args.get("parallel_group"):
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
        messages.append({"role": "assistant", "content": assistant_blocks})
        messages.append({"role": "user", "content": result_blocks})
    else:
        # ScriptedLLMBackend / id-less path: plain strings suffice.
        messages.append({"role": "assistant", "content": " ".join(simple_calls)})
        messages.append({"role": "user", "content": " ".join(simple_results)})
    return had_failure


def _classify_outcome(success: bool, had_tool_failure: bool, failed: bool) -> Outcome:
    if failed:
        return Outcome.FAILED
    if not success:
        # "Ran out of steps cleanly" vs "every tool failed trying": the
        # latter is usually the user's actual bug (bad sandbox path, missing
        # dep, wrong manifest). Collapsing both into INCOMPLETE hides that.
        return Outcome.INCOMPLETE_WITH_TOOL_FAILURE if had_tool_failure else Outcome.INCOMPLETE
    return Outcome.SUCCESS_WITH_RECOVERY if had_tool_failure else Outcome.SUCCESS
