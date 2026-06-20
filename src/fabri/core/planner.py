"""A2: planner/executor split.

A single LLM call ahead of the agent loop emits a structured plan (list of
goals with artefacts + dependencies); the agent loop then executes one item
at a time with a *minimal* per-item context block instead of the full
orchestrator prompt + accumulated tool_result history. Cheaper than
interleaved think-act for multi-domain tasks (typical Ludexel flow: "add a
forest map with a shopkeeper" touches map + character + items).

Generalises the existing `decompose` seam -- `decompose` is a flat list of
strings; `plan` is a typed list of dependency-ordered work items.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from fabri.core.llm import LLMBackend

DEFAULT_MAX_PLAN_ITEMS = 8

DEFAULT_PLANNER_PROMPT = (
    "You produce a JSON execution plan for an autonomous agent. "
    "Return ONLY a JSON object of shape "
    '{"items": [{"goal": str, "artifacts": [str], "depends_on": [int], "tool_hints": [str]}]}. '
    "Each item is one concrete sub-task. `depends_on` is a list of zero-indexed "
    "item indices that must complete first. Keep the list short and concrete; "
    "no prose outside the JSON."
)


@dataclass
class PlanItem:
    goal: str
    artifacts: list[str] = field(default_factory=list)
    depends_on: list[int] = field(default_factory=list)
    tool_hints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "artifacts": list(self.artifacts),
            "depends_on": list(self.depends_on),
            "tool_hints": list(self.tool_hints),
        }


def plan(
    task: str,
    llm: LLMBackend,
    *,
    max_items: int = DEFAULT_MAX_PLAN_ITEMS,
    prompt: str | None = None,
) -> list[PlanItem]:
    """Ask `llm` to break `task` into at most `max_items` plan items.

    Robust to: trailing prose, leading code fences, malformed depends_on
    indices, items missing optional fields. A response that is completely
    unparseable falls back to a one-item plan whose goal is the original
    task -- the executor still does useful work; we just don't get the
    cross-step token savings on this run.
    """
    system_prompt = prompt or DEFAULT_PLANNER_PROMPT
    user_prompt = (
        f"Plan this task in at most {max_items} items.\n\nTask: {task}"
    )
    response = llm.step(system_prompt, [{"role": "user", "content": user_prompt}])
    text = (response.final_text or "").strip()
    items = _parse_plan(text, fallback_goal=task)
    if not items:
        items = [PlanItem(goal=task)]
    return items[:max_items]


def _parse_plan(text: str, *, fallback_goal: str) -> list[PlanItem]:
    obj = _extract_json_object(text)
    if obj is None or not isinstance(obj, dict):
        return []
    raw_items = obj.get("items")
    if not isinstance(raw_items, list):
        return []
    items: list[PlanItem] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        goal = entry.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            continue
        items.append(
            PlanItem(
                goal=goal.strip(),
                artifacts=_as_str_list(entry.get("artifacts")),
                depends_on=_as_int_list(entry.get("depends_on")),
                tool_hints=_as_str_list(entry.get("tool_hints")),
            )
        )
    return items


def _extract_json_object(text: str) -> dict | None:
    # Strip Markdown code fences a model often wraps JSON in.
    fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    for candidate in (fenced, text):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    # As a last resort: pick the first {...} block in the text.
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def _as_str_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


def _as_int_list(value) -> list[int]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, int) and v >= 0]


def topological_order(items: list[PlanItem]) -> list[int]:
    """Return plan-item indices in dependency-resolved order. A cycle or a
    dangling dependency falls back to the original order rather than
    raising -- a planner mistake should not stop the executor."""
    n = len(items)
    indeg = [0] * n
    children: list[list[int]] = [[] for _ in range(n)]
    for i, item in enumerate(items):
        for dep in item.depends_on:
            if 0 <= dep < n and dep != i:
                indeg[i] += 1
                children[dep].append(i)
    ready = [i for i in range(n) if indeg[i] == 0]
    order: list[int] = []
    while ready:
        i = ready.pop(0)
        order.append(i)
        for c in children[i]:
            indeg[c] -= 1
            if indeg[c] == 0:
                ready.append(c)
    if len(order) != n:
        # Cycle or stray dep: append the unvisited tail in declared order.
        remaining = [i for i in range(n) if i not in order]
        order.extend(remaining)
    return order
