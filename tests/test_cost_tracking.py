"""COGS instrumentation: model-priced per-run cost + sub-agent rollup.

`fabri.pricing.cost_for` is pure and needs nothing external. The run_agent
aggregation tests drive a ScriptedLLMBackend whose responses carry per-call
LLMUsage (with a model id) so the loop prices a mixed-model run correctly. The
sub-agent rollup is exercised at the `_dispatch_tool_calls` seam with a stub
registry, so it needs neither a subprocess nor an API key. A live Qdrant is
required only by the run_agent tests (the rest of the suite already assumes it).
"""
import uuid

from fabri import QdrantMemoryStore, ScriptedLLMBackend, ToolRegistry, run_agent
from fabri.core.agent import _dispatch_tool_calls
from fabri.core.llm import LLMResponse, LLMUsage, ToolCall
from fabri.pricing import cost_for


def _store() -> QdrantMemoryStore:
    return QdrantMemoryStore(collection=f"cost_{uuid.uuid4().hex[:10]}")


# ---- fabri.pricing.cost_for ------------------------------------------------

def test_pricing_sonnet_input_and_output():
    # 1M input @ $3 + 1M output @ $15 = $18.
    u = LLMUsage(input_tokens=1_000_000, output_tokens=1_000_000, model="claude-sonnet-4-6")
    assert cost_for(u) == 18.0


def test_pricing_cache_multipliers():
    # cache write = 1.25x input, cache read = 0.10x input (Sonnet input $3/MTok).
    u = LLMUsage(
        cache_creation_input_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
        model="claude-sonnet-4-6",
    )
    assert cost_for(u) == round(3.0 * 1.25 + 3.0 * 0.10, 6)


def test_pricing_tolerates_date_suffixed_model_id():
    # The backend may report "claude-haiku-4-5-20251001"; prefix match resolves
    # it to the base "claude-haiku-4-5" ($1/MTok input).
    u = LLMUsage(input_tokens=1_000_000, model="claude-haiku-4-5-20251001")
    assert cost_for(u) == 1.0


def test_pricing_unknown_model_is_none_not_zero():
    # Unknown/absent model -> None (we don't know), never a misleading 0.
    assert cost_for(LLMUsage(input_tokens=100, model="some-unlisted-model")) is None
    assert cost_for(LLMUsage(input_tokens=100, model=None)) is None


# ---- run_agent per-model aggregation ---------------------------------------

def test_run_agent_prices_each_model_separately(tmp_path):
    import os
    os.environ["FABRI_SANDBOX_ROOT"] = str(tmp_path)
    tools = ToolRegistry(
        __import__("pathlib").Path(__file__).resolve().parent.parent
        / "src" / "fabri" / "tools" / "examples"
    )
    # Two steps on two different models: a Sonnet tool-call step, then a Haiku
    # final step. Cost must be priced per model, not at one blended rate.
    script = [
        LLMResponse(
            tool_call=ToolCall(name="list_dir", args={"path": "."}, id="t1"),
            usage=LLMUsage(input_tokens=1_000_000, model="claude-sonnet-4-6"),
        ),
        LLMResponse(
            final_text="done",
            usage=LLMUsage(output_tokens=1_000_000, model="claude-haiku-4-5"),
        ),
    ]
    result = run_agent("price two models", ScriptedLLMBackend(script), tools, _store())
    usage = result["usage"]
    # Sonnet 1M input = $3; Haiku 1M output = $5.
    assert usage["cost_by_model"]["claude-sonnet-4-6"] == 3.0
    assert usage["cost_by_model"]["claude-haiku-4-5"] == 5.0
    assert usage["cost_usd"] == 8.0
    assert usage["subagent_cost_usd"] == 0.0
    assert usage["total_cost_usd"] == 8.0


# ---- sub-agent rollup ------------------------------------------------------

class _StubRegistry:
    """Minimal ToolRegistry stand-in: invoke returns a canned spawn_subagent
    result carrying the child's usage.total_cost_usd."""

    def __init__(self, child_total_cost):
        self._child_total_cost = child_total_cost

    def invoke(self, name: str, args: dict) -> dict:
        return {
            "ok": True,
            "result": {
                "final_text": "child done",
                "outcome": "success",
                "session_id": "child-sess",
                "trace_path": "/tmp/child.jsonl",
                "usage": {
                    "input_tokens": 500,
                    "output_tokens": 100,
                    "total_cost_usd": self._child_total_cost,
                    "cost_usd": self._child_total_cost,
                },
            },
        }


def test_dispatch_rolls_up_subagent_total_cost():
    captured = []
    calls = [ToolCall(name="spawn_subagent", args={"config_path": "c", "task": "t"}, id="s1")]
    messages: list[dict] = []
    _dispatch_tool_calls(
        calls,
        _StubRegistry(child_total_cost=0.37),
        llm=None,  # no decompose path taken for spawn_subagent
        default_task="t",
        max_subquestions=5,
        session_id="parent-sess-rollup",
        messages=messages,
        step_num=0,
        on_subagent_cost=captured.append,
    )
    assert captured == [0.37]


def test_scripted_prewarm_is_a_noop():
    # prewarm exists on every backend so callers don't special-case; the
    # scripted backend has no provider cache, so it returns zero usage.
    u = ScriptedLLMBackend([]).prewarm("some system prompt")
    assert u.input_tokens == 0
    assert u.cache_creation_input_tokens == 0
    assert cost_for(u) is None  # no model -> unpriced, never a misleading 0


def test_dispatch_without_callback_is_a_noop_for_cost():
    # Back-compat: a caller that doesn't pass on_subagent_cost (e.g. the F2
    # timing tests) still works and simply doesn't roll up cost.
    calls = [ToolCall(name="spawn_subagent", args={"config_path": "c", "task": "t"}, id="s1")]
    had_failure = _dispatch_tool_calls(
        calls,
        _StubRegistry(child_total_cost=0.37),
        llm=None,
        default_task="t",
        max_subquestions=5,
        session_id="parent-sess-noncb",
        messages=[],
        step_num=0,
    )
    assert had_failure is False
