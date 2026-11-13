"""Compare three ways a parent agent can reach a large tool set.

Arms:

- ``direct``: the parent sees every tool and calls them itself.
- ``retrieval``: the parent sees the top-k tools by word overlap with the task.
- ``specialists``: the parent sees one ``delegate`` tool. Each call builds a
  specialist profile (``fabri.builder.specialists``), runs a child with the
  narrowed tools, and hands back a capped envelope
  (``fabri.tools.subagent_envelope``).

For each arm the report gives task success, cost, and the parent's peak
context size in tokens (system prompt + tool definitions + messages, at the
largest step). Child and designer calls count toward cost, not toward the
parent's context.

The tasks are synthetic: 30 lookup tools across six areas, each returning a
code buried in a verbose record. A task asks for two codes joined by ``+``.

By default the harness runs offline with a scripted model that follows each
task's plan. That checks the plumbing and shows the context-size mechanics; it
says nothing about how well a real model picks tools. Mock costs use a flat
illustrative rate. ``--live`` runs a real model from an agent config and costs
money; see docs/specialists.md for the command and a rough price.

This harness is for measurement. It does not make specialists a default.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Callable

from fabri.builder.prompt_kit import render_task_brief
from fabri.builder.specialists import DESIGNER_SYSTEM, ProfileCache, make_specialist
from fabri.core.llm import LLMResponse, LLMUsage, ToolCall
from fabri.tools.subagent_envelope import DEFAULT_SUMMARY_MAX_TOKENS, _count_tokens, cap_envelope

ARMS = ("direct", "retrieval", "specialists")
MOCK_RATE_IN = 3.0 / 1_000_000   # USD per input token, mock runs only
MOCK_RATE_OUT = 15.0 / 1_000_000
PARENT_IDENTITY = "You are a careful operations agent. Use tools to look things up, then answer exactly as asked."
DELEGATE_TOOL = "delegate"

AREAS = ("inventory", "billing", "shipping", "crm", "payroll", "support")
OPS = {
    "lookup": "Look up one {area} record by id and return its fields, including its code.",
    "history": "Return the change history of one {area} record by id.",
    "notes": "Return free-text notes attached to one {area} record by id.",
    "stats": "Return aggregate {area} statistics for a date range.",
    "audit": "Return the audit trail for one {area} record by id.",
}


# --- fixture ---------------------------------------------------------------

@dataclass(frozen=True)
class EvalTask:
    id: str
    text: str
    plan: tuple[tuple[str, dict], ...]
    answer: str


def _code(tool: str, rid: str) -> str:
    return f"{tool.split('_')[0][:3].upper()}{sum(map(ord, tool + rid)) % 9000 + 1000}"


def tool_catalog() -> dict[str, dict]:
    """name -> tool definition (Anthropic-shaped)."""
    out = {}
    for area in AREAS:
        for op, desc in OPS.items():
            name = f"{area}_{op}"
            out[name] = {
                "name": name,
                "description": desc.format(area=area) + " Read only.",
                "input_schema": {"type": "object", "properties": {"id": {"type": "string"}},
                                 "required": ["id"]},
            }
    return out


def run_fixture_tool(name: str, args: dict) -> dict:
    rid = str(args.get("id", ""))
    filler = " ".join(f"field_{i}=value_{(i * 7 + len(rid)) % 97}" for i in range(120))
    return {"ok": True, "result": {"id": rid, "tool": name, "code": _code(name, rid), "details": filler}}


def eval_tasks() -> list[EvalTask]:
    pairs = [("inventory_lookup", "billing_lookup"), ("shipping_lookup", "crm_lookup"),
             ("payroll_lookup", "support_lookup"), ("billing_lookup", "shipping_lookup"),
             ("crm_lookup", "payroll_lookup"), ("support_lookup", "inventory_lookup")]
    tasks = []
    for i, (a, b) in enumerate(pairs):
        ra, rb = f"R{i}a", f"R{i}b"
        text = (f"[task t{i}] Look up {a.split('_')[0]} record {ra} and {b.split('_')[0]} record {rb}. "
                "Reply with only their two codes joined by '+', in that order.")
        tasks.append(EvalTask(f"t{i}", text, ((a, {"id": ra}), (b, {"id": rb})),
                              f"{_code(a, ra)}+{_code(b, rb)}"))
    return tasks


# --- loop ------------------------------------------------------------------

@dataclass
class LoopResult:
    final_text: str
    usages: list[LLMUsage] = field(default_factory=list)
    peak_context_tokens: int = 0
    steps: int = 0


def _system(identity: str, defs: list[dict]) -> str:
    lines = "\n".join(f"- {d['name']}: {d['description']}" for d in defs)
    return f"{identity}\n\nAvailable tools:\n{lines}" if defs else identity


def run_loop(llm, identity: str, defs: list[dict], task: str,
             execute: Callable[[str, dict], dict], max_steps: int = 8) -> LoopResult:
    """A minimal tool loop with the same message shapes fabri's agent uses."""
    llm.set_tools(defs)
    system = _system(identity, defs)
    messages: list[dict] = [{"role": "user", "content": task}]
    res = LoopResult(final_text="")
    for step in range(max_steps):
        ctx = _count_tokens(system + json.dumps(defs) + json.dumps(messages))
        res.peak_context_tokens = max(res.peak_context_tokens, ctx)
        resp = llm.step(system, messages)
        res.steps = step + 1
        if resp.usage is not None:
            res.usages.append(resp.usage)
        if not resp.tool_calls:
            res.final_text = resp.final_text or ""
            return res
        uses, results = [], []
        for n, call in enumerate(resp.tool_calls):
            cid = call.id or f"call_{step}_{n}"
            out = execute(call.name, call.args) if call.name in {d["name"] for d in defs} else {
                "ok": False, "error": f"unknown tool: {call.name}"}
            uses.append({"type": "tool_use", "id": cid, "name": call.name, "input": call.args})
            results.append({"type": "tool_result", "tool_use_id": cid, "content": json.dumps(out)})
        messages.append({"role": "assistant", "content": uses})
        messages.append({"role": "user", "content": results})
    return res


def retrieve_tools(task: str, catalog: dict[str, dict], top_k: int) -> list[dict]:
    """Top-k tools by word overlap between the task and name + description.
    A stand-in for fabri's embedding retrieval so the harness runs offline."""
    words = set(re.findall(r"[a-z]+", task.lower()))
    scored = []
    for name, d in catalog.items():
        tool_words = set(re.findall(r"[a-z]+", (name.replace("_", " ") + " " + d["description"]).lower()))
        scored.append((-len(words & tool_words), name))
    return [catalog[n] for _, n in sorted(scored)[:top_k]]


# --- arms ------------------------------------------------------------------

@dataclass
class ArmRun:
    success: bool
    usages: list[LLMUsage]
    parent_peak_tokens: int
    steps: int


def run_arm(arm: str, task: EvalTask, make_llm: Callable[[str], object], *,
            top_k: int = 6, cache: ProfileCache | None = None,
            summary_max_tokens: int = DEFAULT_SUMMARY_MAX_TOKENS) -> ArmRun:
    """``make_llm(role)`` returns a backend for role "parent", "child" or
    "designer"."""
    catalog = tool_catalog()
    usages: list[LLMUsage] = []
    if arm == "direct":
        r = run_loop(make_llm("parent"), PARENT_IDENTITY, list(catalog.values()), task.text, run_fixture_tool)
    elif arm == "retrieval":
        r = run_loop(make_llm("parent"), PARENT_IDENTITY, retrieve_tools(task.text, catalog, top_k),
                     task.text, run_fixture_tool)
    elif arm == "specialists":
        def delegate(_name: str, args: dict) -> dict:
            sub_task = str(args.get("task") or task.text)
            designer = make_llm("designer")
            designer.set_tools([])
            profile = make_specialist(sub_task, list(catalog), designer, cache=cache)
            usages.extend(getattr(designer, "usages", []))
            child_defs = [catalog[t] for t in profile.tools]
            brief = render_task_brief(sub_task, tools=profile.tools, tool_notes=profile.tool_notes)
            child = run_loop(make_llm("child"), profile.system_prompt, child_defs, brief, run_fixture_tool)
            usages.extend(child.usages)
            env = cap_envelope({"final_text": child.final_text, "outcome": "success",
                                "session_id": f"eval-{task.id}", "trace_path": "(none)"}, summary_max_tokens)
            return {"ok": True, "result": env}
        defs = [{"name": DELEGATE_TOOL,
                 "description": "Hand a self-contained task to a specialist with the right tools. "
                                "Returns a short summary.",
                 "input_schema": {"type": "object", "properties": {"task": {"type": "string"}},
                                  "required": ["task"]}}]
        r = run_loop(make_llm("parent"), PARENT_IDENTITY, defs, task.text, delegate)
    else:
        raise ValueError(f"unknown arm {arm!r}")
    return ArmRun(task.answer in (r.final_text or ""), r.usages + usages, r.peak_context_tokens, r.steps)


# --- mock model -------------------------------------------------------------

class PlanFollowingLLM:
    """Scripted stand-in for a model. It reads the task id from the first
    user message and follows that task's plan with whatever tools it was
    given; it answers with codes found in tool results. Usage is estimated
    from the text it saw, so mock costs scale with context size."""

    def __init__(self, tasks: list[EvalTask]):
        self.plans = {t.id: t for t in tasks}
        self.tools: set[str] = set()
        self.usages: list[LLMUsage] = []

    def set_tools(self, defs: list[dict]) -> None:
        self.tools = {d["name"] for d in defs}

    def _reply(self, system: str, messages: list[dict], resp: LLMResponse) -> LLMResponse:
        out = resp.final_text or json.dumps([c.args for c in resp.tool_calls])
        resp.usage = LLMUsage(input_tokens=_count_tokens(system + json.dumps(messages)),
                              output_tokens=_count_tokens(out), model="mock")
        self.usages.append(resp.usage)
        return resp

    def step(self, system: str, messages: list[dict]) -> LLMResponse:
        first = messages[0]["content"] if isinstance(messages[0]["content"], str) else ""
        m = re.search(r"\[task (\w+)\]", first)
        task = self.plans.get(m.group(1)) if m else None
        if task is None:
            return self._reply(system, messages, LLMResponse(final_text="unknown"))
        if system == DESIGNER_SYSTEM:
            return self._reply(system, messages, LLMResponse(final_text=json.dumps(
                {"role": "record lookup clerk", "owns": ["the requested codes"],
                 "tools": [name for name, _ in task.plan]})))
        seen = json.dumps(messages[1:])
        called = set(re.findall(r'"name": "(\w+)"', seen))
        if DELEGATE_TOOL in self.tools and DELEGATE_TOOL not in called:
            return self._reply(system, messages, LLMResponse(
                tool_calls=[ToolCall(DELEGATE_TOOL, {"task": task.text}, id="d1")]))
        pending = [(n, a) for n, a in task.plan if n in self.tools and n not in called]
        if pending:
            return self._reply(system, messages, LLMResponse(
                tool_calls=[ToolCall(n, a, id=f"c{i}") for i, (n, a) in enumerate(pending)]))
        codes = re.findall(r'\\"code\\": \\"(\w+)\\"', seen) or re.findall(r"\b[A-Z]{3}\d{4}\b", seen)
        return self._reply(system, messages, LLMResponse(final_text="+".join(dict.fromkeys(codes)) or "unknown"))


class _Recording:
    """Wraps any backend and keeps the usage of every reply, so designer and
    child calls are counted the same way for mock and live backends."""

    def __init__(self, inner):
        self.inner = inner
        self.usages: list[LLMUsage] = []

    def set_tools(self, defs: list[dict]) -> None:
        self.inner.set_tools(defs)

    def step(self, system: str, messages: list[dict]) -> LLMResponse:
        resp = self.inner.step(system, messages)
        if resp.usage is not None:
            self.usages.append(resp.usage)
        return resp


def _mock_cost(u: LLMUsage) -> float:
    return u.input_tokens * MOCK_RATE_IN + u.output_tokens * MOCK_RATE_OUT


# --- report -----------------------------------------------------------------

def evaluate(make_llm: Callable[[str], object], tasks: list[EvalTask], *, arms=ARMS,
             cost_fn: Callable[[LLMUsage], float | None] = _mock_cost, top_k: int = 6) -> dict:
    cache = ProfileCache()
    report: dict = {"tasks": len(tasks), "arms": {}}
    for arm in arms:
        runs = [run_arm(arm, t, lambda role: _Recording(make_llm(role)), top_k=top_k, cache=cache)
                for t in tasks]
        costs = [cost_fn(u) for r in runs for u in r.usages]
        report["arms"][arm] = {
            "success_rate": round(sum(r.success for r in runs) / len(runs), 3) if runs else 0.0,
            "cost_usd": None if any(c is None for c in costs) else round(sum(costs), 6),
            "mean_parent_peak_tokens": round(sum(r.parent_peak_tokens for r in runs) / len(runs)) if runs else 0,
            "llm_calls": sum(len(r.usages) for r in runs),
        }
    return report


def _format(report: dict) -> str:
    rows = [f"{'arm':<12} {'success':>8} {'cost_usd':>10} {'parent_peak_tok':>16} {'llm_calls':>10}"]
    for arm, r in report["arms"].items():
        cost = "n/a" if r["cost_usd"] is None else f"{r['cost_usd']:.4f}"
        rows.append(f"{arm:<12} {r['success_rate']:>8.2f} {cost:>10} {r['mean_parent_peak_tokens']:>16} "
                    f"{r['llm_calls']:>10}")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m fabri.benchmarks.specialist_eval", description=__doc__.split("\n")[0])
    p.add_argument("--live", action="store_true", help="Use a real model from --config. Costs money.")
    p.add_argument("--config", help="agent.yaml whose llm block is used for every role in --live mode")
    p.add_argument("--arms", default=",".join(ARMS))
    p.add_argument("--tasks", type=int, default=None, help="Run only the first N tasks")
    p.add_argument("--top-k", type=int, default=6)
    p.add_argument("--json", action="store_true", help="Print the JSON report only")
    a = p.parse_args(argv)
    tasks = eval_tasks()[: a.tasks] if a.tasks else eval_tasks()
    arms = tuple(x for x in a.arms.split(",") if x)
    if a.live:
        if not a.config:
            p.error("--live needs --config")
        from fabri.config import load_config
        from fabri.pricing import cost_for
        from fabri.runtime import build_llm

        config = load_config(a.config)
        report = evaluate(lambda role: build_llm(config, []), tasks, arms=arms, cost_fn=cost_for, top_k=a.top_k)
        report["mode"] = "live"
    else:
        report = evaluate(lambda role: PlanFollowingLLM(tasks), tasks, arms=arms, top_k=a.top_k)
        report["mode"] = "mock (scripted model, flat illustrative rate)"
    print(json.dumps(report, indent=2) if a.json else _format(report) + f"\nmode: {report['mode']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
