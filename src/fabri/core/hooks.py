"""Host hooks for one `run_agent` call.

A host passes a `RunHooks` object as the `hooks=` kwarg of `run_agent`. Every
field is optional; a field left as None costs nothing and changes nothing.
Hooks are code, so they are never read from YAML or `AgentRunConfig`.

Signatures:

- ``before_step(step, messages) -> Stop | None``. Runs before each main-loop
  model call, after the budget check and before the ``step_started`` event.
  Returning ``Stop(final_text)`` ends the run with that text as the final
  answer and no model call for this step.
- ``after_step(step, response) -> Stop | None``. Runs after a step that called
  the model and finished normally (tools dispatched, or a final answer). A
  ``Stop`` returned after a tools step ends the run with its text.
- ``on_llm_call(kind, response, elapsed_ms)``. Runs after every model call the
  loop makes on the main backend. ``kind`` is ``"step"`` for a loop step and
  ``"structured_retry"`` for a response-schema corrective call. Planner,
  decompose and narrator calls are not reported.
- ``before_tool(call) -> Reject | None``. Runs inside ``ToolRegistry.invoke``
  just before the handler, with the ``CallContext`` of the call. Batch inner
  calls are seen one by one; the batch wrapper is not. Returning
  ``Reject(reason, result=None)`` skips the handler; the call returns
  ``result`` or a tool error naming the reason.
- ``after_tool(call, result) -> dict | None``. Runs after the handler (or a
  rejection). Returning a dict replaces the result.
- ``on_unknown_tool(name, args) -> dict | None``. Runs when a tool name does
  not resolve to a registered tool or alias. A returned dict is the call's
  result; None keeps the default ``unknown tool`` error.

Exceptions raised by the step hooks propagate and end the run. Exceptions
raised by the tool hooks become a tool error for that call, so a failing hook
never leaves a tool call without a result.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Stop:
    """End the run now with `final_text` as the answer."""

    final_text: str


@dataclass(frozen=True)
class Reject:
    """Skip a tool handler. `result`, when given, is returned in its place."""

    reason: str
    result: dict | None = None


@dataclass(frozen=True)
class StopSignal:
    """Why and how a run was stopped early. Only kind ``"hook"`` exists today:
    it maps to the normal final outcome, skips response-schema validation, ends
    the planner item loop and is never repaired."""

    kind: str
    final_text: str


@dataclass
class RunHooks:
    before_step: Callable[[int, list], Any] | None = None
    after_step: Callable[[int, Any], Any] | None = None
    on_llm_call: Callable[[str, Any, float], None] | None = None
    before_tool: Callable[[Any], Any] | None = None
    after_tool: Callable[[Any, dict], Any] | None = None
    on_unknown_tool: Callable[[str, dict], Any] | None = None
