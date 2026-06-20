"""Centralized string-enum types for fabri's trace event vocabulary.

Every `log_event(session_id, {"type": "..."})` site in the agent loop reads from
`EventType` here; every `step_finished.reason` reads from `StepReason`. The
classes derive from `(str, Enum)` so `EventType.THOUGHT.value == "thought"` and
the on-wire JSON stays bytes-identical to the pre-enum strings -- existing
trace readers (the ludexel service, `read_trace`, tests) keep working unchanged.

Why centralize: before this module the string "thought" / "tool_call" /
"step_finished" was duplicated across emission sites and consumer sites in
two repos. A typo on either side silently dropped the event. The enum makes
the vocabulary single-source-of-truth and grep-able.
"""
from enum import Enum


class EventType(str, Enum):
    START = "start"
    STEP_STARTED = "step_started"
    STEP_FINISHED = "step_finished"
    THOUGHT = "thought"
    TOOL_STARTED = "tool_started"
    TOOL_CALL = "tool_call"  # tool completion -- name kept for back-compat
    PARALLEL_GROUP_STARTED = "parallel_group_started"
    FINAL = "final"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    ERROR = "error"
    ASK_USER = "ask_user"
    USAGE = "usage"
    PLAN_STARTED = "plan_started"
    PLAN_ITEM_STARTED = "plan_item_started"
    PLAN_ITEM_FINISHED = "plan_item_finished"
    PLAN_FINISHED = "plan_finished"


class StepReason(str, Enum):
    """The exit condition for a single agent-loop step. Stamped on every
    `step_finished` event so a UI can colour-code or filter step rows."""

    TOOLS = "tools"  # the step dispatched tools and the loop will continue
    FINAL = "final"  # the step produced final_text and the loop will break
    LLM_ERROR = "llm_error"  # provider raised LLMError; loop will break FAILED
    PROTOCOL_ERROR = "protocol_error"  # no tool_calls and no final_text; raise
