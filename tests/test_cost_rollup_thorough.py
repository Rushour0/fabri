"""Thorough coverage for run_agent's COGS rollup + the _dispatch_tool_calls
sub-agent cost callback.

These drive the real agent loop with a ScriptedLLMBackend whose responses carry
per-call LLMUsage (with a model id), so the loop prices a single- or
mixed-model run end to end. A live Qdrant at http://localhost:6333 is required
by the run_agent tests (the rest of the suite already assumes one).

The sub-agent rollup is exercised directly at the _dispatch_tool_calls seam
with a stub registry, so it needs neither a subprocess nor an API key.
"""
import os
import uuid
from pathlib import Path

from fabri import QdrantMemoryStore, ScriptedLLMBackend, ToolRegistry, run_agent
from fabri.core.agent import _dispatch_tool_calls
from fabri.core.llm import LLMResponse, LLMUsage, ToolCall

M = 1_000_000

_EXAMPLES = (
    Path(__file__).resolve().parent.parent / "src" / "fabri" / "tools" / "examples"
)


def _store() -> QdrantMemoryStore:
    return QdrantMemoryStore(collection=f"cost_{uuid.uuid4().hex[:10]}")


def _tools(tmp_path) -> ToolRegistry:
    os.environ["FABRI_SANDBOX_ROOT"] = str(tmp_path)
    return ToolRegistry(_EXAMPLES)


# ============================================================================
# run_agent: own-token pricing rollup
# ============================================================================

def test_single_model_run_rollup(tmp_path):
    # One tool-call step then a final step, both Sonnet. cost_usd must equal the
    # priced own tokens; cost_by_model has exactly one entry; no sub-agents.
    script = [
        LLMResponse(
            tool_call=ToolCall(name="list_dir", args={"path": "."}, id="t1"),
            usage=LLMUsage(input_tokens=M, model="claude-sonnet-4-6"),
        ),
        LLMResponse(
            final_text="done",
            usage=LLMUsage(output_tokens=M, model="claude-sonnet-4-6"),
        ),
    ]
    result = run_agent("single model", ScriptedLLMBackend(script), _tools(tmp_path), _store())
    usage = result["usage"]
    # Sonnet 1M input ($3) + 1M output ($15) aggregated under one model = $18.
    assert usage["cost_by_model"] == {"claude-sonnet-4-6": 18.0}
    assert usage["cost_usd"] == 18.0
    assert usage["subagent_cost_usd"] == 0.0
    assert usage["total_cost_usd"] == usage["cost_usd"]
    assert usage["total_cost_usd"] == 18.0


def test_single_model_final_only(tmp_path):
    # A single final-text step, output only, Haiku.
    script = [
        LLMResponse(final_text="done", usage=LLMUsage(output_tokens=M, model="claude-haiku-4-5")),
    ]
    result = run_agent("haiku only", ScriptedLLMBackend(script), _tools(tmp_path), _store())
    usage = result["usage"]
    assert usage["cost_by_model"] == {"claude-haiku-4-5": 5.0}  # 1M output @ $5
    assert usage["cost_usd"] == 5.0
    assert usage["subagent_cost_usd"] == 0.0
    assert usage["total_cost_usd"] == 5.0


def test_mixed_model_run_prices_each_separately(tmp_path):
    # Sonnet tool-call step, then Haiku final step. cost_by_model has BOTH, and
    # cost_usd is the sum of the per-model figures.
    script = [
        LLMResponse(
            tool_call=ToolCall(name="list_dir", args={"path": "."}, id="t1"),
            usage=LLMUsage(input_tokens=M, model="claude-sonnet-4-6"),
        ),
        LLMResponse(
            final_text="done",
            usage=LLMUsage(output_tokens=M, model="claude-haiku-4-5"),
        ),
    ]
    result = run_agent("mixed model", ScriptedLLMBackend(script), _tools(tmp_path), _store())
    usage = result["usage"]
    assert usage["cost_by_model"]["claude-sonnet-4-6"] == 3.0   # 1M input @ $3
    assert usage["cost_by_model"]["claude-haiku-4-5"] == 5.0    # 1M output @ $5
    assert set(usage["cost_by_model"]) == {"claude-sonnet-4-6", "claude-haiku-4-5"}
    assert usage["cost_usd"] == 8.0
    assert usage["total_cost_usd"] == 8.0


def test_mixed_three_models_with_opus(tmp_path):
    # Opus tool step, Sonnet tool step, Haiku final. All three priced separately.
    script = [
        LLMResponse(
            tool_call=ToolCall(name="list_dir", args={"path": "."}, id="t1"),
            usage=LLMUsage(input_tokens=M, model="claude-opus-4-8"),
        ),
        LLMResponse(
            tool_call=ToolCall(name="list_dir", args={"path": "."}, id="t2"),
            usage=LLMUsage(input_tokens=M, model="claude-sonnet-4-6"),
        ),
        LLMResponse(
            final_text="done",
            usage=LLMUsage(output_tokens=M, model="claude-haiku-4-5"),
        ),
    ]
    result = run_agent("three models", ScriptedLLMBackend(script), _tools(tmp_path), _store())
    usage = result["usage"]
    assert usage["cost_by_model"] == {
        "claude-opus-4-8": 5.0,   # 1M input @ $5
        "claude-sonnet-4-6": 3.0,  # 1M input @ $3
        "claude-haiku-4-5": 5.0,   # 1M output @ $5
    }
    assert usage["cost_usd"] == 13.0
    assert usage["total_cost_usd"] == 13.0


def test_same_model_across_steps_aggregates_into_one_entry(tmp_path):
    # Two Sonnet steps -> tokens aggregate into a single Sonnet bucket priced once.
    script = [
        LLMResponse(
            tool_call=ToolCall(name="list_dir", args={"path": "."}, id="t1"),
            usage=LLMUsage(input_tokens=M, model="claude-sonnet-4-6"),
        ),
        LLMResponse(
            final_text="done",
            usage=LLMUsage(input_tokens=M, output_tokens=M, model="claude-sonnet-4-6"),
        ),
    ]
    result = run_agent("agg one model", ScriptedLLMBackend(script), _tools(tmp_path), _store())
    usage = result["usage"]
    # 2M input ($6) + 1M output ($15) = $21, one entry.
    assert usage["cost_by_model"] == {"claude-sonnet-4-6": 21.0}
    assert usage["cost_usd"] == 21.0


# ---- unknown / None model contributes 0 (no misleading priced entry) -------

def test_unknown_model_step_contributes_zero(tmp_path):
    # A Sonnet step plus a step whose model is unknown. The unknown step's tokens
    # still aggregate into usage totals, but it must NOT appear priced in
    # cost_by_model, and cost_usd reflects only the Sonnet (priced) tokens.
    script = [
        LLMResponse(
            tool_call=ToolCall(name="list_dir", args={"path": "."}, id="t1"),
            usage=LLMUsage(input_tokens=M, output_tokens=M, model="claude-sonnet-4-6"),
        ),
        LLMResponse(
            final_text="done",
            usage=LLMUsage(input_tokens=M, output_tokens=M, model="some-unlisted-model"),
        ),
    ]
    result = run_agent("unknown model", ScriptedLLMBackend(script), _tools(tmp_path), _store())
    usage = result["usage"]
    assert usage["cost_by_model"] == {"claude-sonnet-4-6": 18.0}
    assert "some-unlisted-model" not in usage["cost_by_model"]
    assert "unknown" not in usage["cost_by_model"]
    assert usage["cost_usd"] == 18.0  # only the priced model counts
    # The unknown step's tokens DID aggregate into raw totals (2M in + 2M out).
    assert usage["input_tokens"] == 2 * M
    assert usage["output_tokens"] == 2 * M


def test_none_model_step_contributes_zero(tmp_path):
    # Same idea but the second step has model=None (e.g. a scripted call).
    script = [
        LLMResponse(
            tool_call=ToolCall(name="list_dir", args={"path": "."}, id="t1"),
            usage=LLMUsage(output_tokens=M, model="claude-haiku-4-5"),
        ),
        LLMResponse(
            final_text="done",
            usage=LLMUsage(input_tokens=M, model=None),
        ),
    ]
    result = run_agent("none model", ScriptedLLMBackend(script), _tools(tmp_path), _store())
    usage = result["usage"]
    assert usage["cost_by_model"] == {"claude-haiku-4-5": 5.0}
    assert usage["cost_usd"] == 5.0
    assert usage["total_cost_usd"] == 5.0


def test_all_steps_unknown_model_cost_is_zero(tmp_path):
    # Every step unknown -> cost_by_model empty, cost_usd 0.0, total 0.0.
    script = [
        LLMResponse(final_text="done", usage=LLMUsage(input_tokens=M, model="who-knows")),
    ]
    result = run_agent("all unknown", ScriptedLLMBackend(script), _tools(tmp_path), _store())
    usage = result["usage"]
    assert usage["cost_by_model"] == {}
    assert usage["cost_usd"] == 0.0
    assert usage["subagent_cost_usd"] == 0.0
    assert usage["total_cost_usd"] == 0.0


# ============================================================================
# _dispatch_tool_calls: sub-agent cost rollup via on_subagent_cost
# ============================================================================

class _SpawnRegistry:
    """ToolRegistry stand-in: spawn_subagent returns a canned child result whose
    usage carries the fields we want to test the rollup against."""

    def __init__(self, *, ok=True, usage=None):
        self._ok = ok
        self._usage = usage

    def invoke(self, name: str, args: dict) -> dict:
        result = {
            "final_text": "child done",
            "outcome": "success",
            "session_id": "child-sess",
            "trace_path": "/tmp/child.jsonl",
        }
        if self._usage is not None:
            result["usage"] = self._usage
        return {"ok": self._ok, "result": result}


def _spawn_call(call_id="s1"):
    return ToolCall(
        name="spawn_subagent",
        args={"config_path": "c", "task": "t"},
        id=call_id,
    )


def _dispatch(registry, calls, captured, session_id):
    return _dispatch_tool_calls(
        calls,
        registry,
        llm=None,  # spawn_subagent never takes the decompose path
        default_task="t",
        max_subquestions=5,
        session_id=session_id,
        messages=[],
        step_num=0,
        on_subagent_cost=captured.append,
    )


def test_rollup_prefers_total_cost_usd():
    captured = []
    reg = _SpawnRegistry(usage={"total_cost_usd": 0.42, "cost_usd": 0.10})
    had_failure = _dispatch(reg, [_spawn_call()], captured, "p-total")
    assert had_failure is False
    assert captured == [0.42]  # total preferred over own cost_usd


def test_rollup_falls_back_to_cost_usd_when_no_total():
    captured = []
    reg = _SpawnRegistry(usage={"cost_usd": 0.10})  # no total_cost_usd
    _dispatch(reg, [_spawn_call()], captured, "p-fallback")
    assert captured == [0.10]


def test_rollup_total_none_falls_back_to_cost_usd():
    captured = []
    reg = _SpawnRegistry(usage={"total_cost_usd": None, "cost_usd": 0.25})
    _dispatch(reg, [_spawn_call()], captured, "p-total-none")
    assert captured == [0.25]


def test_multiple_spawns_each_roll_up():
    # Two spawn calls in one turn -> two callback invocations; the parent sums
    # them via its accumulator (here we just confirm both fire with the values).
    captured = []
    reg = _SpawnRegistry(usage={"total_cost_usd": 0.11})
    calls = [_spawn_call("s1"), _spawn_call("s2")]
    _dispatch(reg, calls, captured, "p-multi")
    assert captured == [0.11, 0.11]
    assert round(sum(captured), 6) == 0.22


def test_failed_spawn_contributes_nothing():
    # ok=false -> no rollup, and the dispatch reports a failure.
    captured = []
    reg = _SpawnRegistry(ok=False, usage={"total_cost_usd": 9.99})
    had_failure = _dispatch(reg, [_spawn_call()], captured, "p-failed")
    assert had_failure is True
    assert captured == []  # nothing rolled up from a failed spawn


def test_child_missing_usage_contributes_nothing():
    # Child result has no "usage" key at all -> no crash, no rollup.
    captured = []
    reg = _SpawnRegistry(usage=None)
    had_failure = _dispatch(reg, [_spawn_call()], captured, "p-nousage")
    assert had_failure is False
    assert captured == []


def test_child_usage_missing_cost_fields_contributes_nothing():
    # usage present but carries neither total_cost_usd nor cost_usd -> no rollup.
    captured = []
    reg = _SpawnRegistry(usage={"input_tokens": 500, "output_tokens": 100})
    had_failure = _dispatch(reg, [_spawn_call()], captured, "p-nocost")
    assert had_failure is False
    assert captured == []


def test_child_cost_non_numeric_contributes_nothing():
    # A non-numeric cost field (e.g. a stringly-typed old child) is ignored.
    captured = []
    reg = _SpawnRegistry(usage={"total_cost_usd": "0.5"})
    had_failure = _dispatch(reg, [_spawn_call()], captured, "p-nonnum")
    assert had_failure is False
    assert captured == []


def test_rollup_zero_cost_child_still_calls_back():
    # A child that genuinely cost 0.0 (numeric) DOES call back with 0.0.
    captured = []
    reg = _SpawnRegistry(usage={"total_cost_usd": 0.0})
    _dispatch(reg, [_spawn_call()], captured, "p-zero")
    assert captured == [0.0]


def test_dispatch_without_callback_does_not_crash():
    # Back-compat: omitting on_subagent_cost still works and rolls up nothing.
    reg = _SpawnRegistry(usage={"total_cost_usd": 0.37})
    had_failure = _dispatch_tool_calls(
        [_spawn_call()],
        reg,
        llm=None,
        default_task="t",
        max_subquestions=5,
        session_id="p-no-cb",
        messages=[],
        step_num=0,
    )
    assert had_failure is False


# ---- total == own + subagent (the persisted COGS identity) -----------------

def test_total_equals_own_plus_subagent_via_accumulator():
    # Simulate the parent's accumulator: dispatch several spawns, sum the
    # callbacks, and assert the identity total = own + subagent.
    own_cost = 8.0  # stand-in for a priced own-token run
    subtotal = [0.0]

    reg = _SpawnRegistry(usage={"total_cost_usd": 0.30})
    calls = [_spawn_call("s1"), _spawn_call("s2")]
    _dispatch_tool_calls(
        calls,
        reg,
        llm=None,
        default_task="t",
        max_subquestions=5,
        session_id="p-identity",
        messages=[],
        step_num=0,
        on_subagent_cost=lambda c: subtotal.__setitem__(0, subtotal[0] + c),
    )
    subagent_cost = round(subtotal[0], 6)
    assert subagent_cost == 0.60
    assert round(own_cost + subagent_cost, 6) == 8.6
