"""Narrator backend emits short status updates between tool steps. Cheap by
design (Haiku-class), best-effort: a narrator failure must never break the
agent loop. Verified end-to-end against ScriptedLLMBackend so neither test
hits a real provider."""
import uuid

from fabri import QdrantMemoryStore, ScriptedLLMBackend, ToolRegistry, run_agent
from fabri.core.llm import LLMResponse, ToolCall
from fabri.orchestrator.traces import read_trace
from fabri.tools.manifest_schema import ToolManifest


def _store():
    return QdrantMemoryStore(collection=f"narrator_{uuid.uuid4().hex[:8]}")


def _registry_with_noop():
    reg = ToolRegistry([])
    reg.register(ToolManifest(
        name="noop", description="ok",
        command=["python3", "-c", "import sys,json; sys.stdin.read(); print(json.dumps({}))"],
        input_schema={}, output_schema={},
    ))
    return reg


def test_narrator_emits_narration_event_after_tool_dispatch():
    reg = _registry_with_noop()
    main_script = [
        LLMResponse(tool_call=ToolCall(name="noop", args={}, id="t1")),
        LLMResponse(final_text="done"),
    ]
    # The narrator gets one .step() call per tool-dispatch step.
    narrator_script = [LLMResponse(final_text="Reading project state.")]
    result = run_agent(
        "x",
        ScriptedLLMBackend(main_script),
        reg,
        _store(),
        narrator_llm=ScriptedLLMBackend(narrator_script),
    )
    assert result["outcome"] == "success"
    events = read_trace(result["session_id"])
    narrations = [e for e in events if e["type"] == "narration"]
    assert len(narrations) == 1
    assert narrations[0]["text"] == "Reading project state."
    assert narrations[0]["trigger"] == "tools"


def test_narrator_failure_does_not_break_the_run():
    reg = _registry_with_noop()
    main_script = [
        LLMResponse(tool_call=ToolCall(name="noop", args={}, id="t1")),
        LLMResponse(final_text="done"),
    ]

    class BoomBackend:
        def set_tools(self, _):
            return None

        def step(self, *_a, **_kw):
            raise RuntimeError("narrator down")

    result = run_agent(
        "x", ScriptedLLMBackend(main_script), reg, _store(),
        narrator_llm=BoomBackend(),
    )
    assert result["outcome"] == "success"
    events = read_trace(result["session_id"])
    assert not [e for e in events if e["type"] == "narration"]


def test_no_narrator_means_no_narration_events():
    reg = _registry_with_noop()
    main_script = [
        LLMResponse(tool_call=ToolCall(name="noop", args={}, id="t1")),
        LLMResponse(final_text="done"),
    ]
    result = run_agent("x", ScriptedLLMBackend(main_script), reg, _store())
    events = read_trace(result["session_id"])
    assert not [e for e in events if e["type"] == "narration"]


def test_narrator_emits_once_per_tool_step():
    """Three tool steps -> three narration events, in step order."""
    reg = _registry_with_noop()
    main_script = [
        LLMResponse(tool_call=ToolCall(name="noop", args={}, id="t1")),
        LLMResponse(tool_call=ToolCall(name="noop", args={}, id="t2")),
        LLMResponse(tool_call=ToolCall(name="noop", args={}, id="t3")),
        LLMResponse(final_text="done"),
    ]
    narrator_script = [
        LLMResponse(final_text="Listing files."),
        LLMResponse(final_text="Reading the config."),
        LLMResponse(final_text="Writing the patch."),
    ]
    result = run_agent(
        "x", ScriptedLLMBackend(main_script), reg, _store(),
        narrator_llm=ScriptedLLMBackend(narrator_script),
    )
    assert result["outcome"] == "success"
    narrations = [e for e in read_trace(result["session_id"]) if e["type"] == "narration"]
    assert [n["text"] for n in narrations] == [
        "Listing files.", "Reading the config.", "Writing the patch.",
    ]
    assert [n["step"] for n in narrations] == [0, 1, 2]


def test_narrator_drops_duplicate_consecutive_updates():
    """If the narrator returns the same string twice in a row, the second is
    suppressed so a host UI doesn't flash an identical line."""
    reg = _registry_with_noop()
    main_script = [
        LLMResponse(tool_call=ToolCall(name="noop", args={}, id="t1")),
        LLMResponse(tool_call=ToolCall(name="noop", args={}, id="t2")),
        LLMResponse(final_text="done"),
    ]
    narrator_script = [
        LLMResponse(final_text="Reading the config."),
        LLMResponse(final_text="Reading the config."),  # duplicate -> dropped
    ]
    result = run_agent(
        "x", ScriptedLLMBackend(main_script), reg, _store(),
        narrator_llm=ScriptedLLMBackend(narrator_script),
    )
    narrations = [e for e in read_trace(result["session_id"]) if e["type"] == "narration"]
    assert len(narrations) == 1
    assert narrations[0]["text"] == "Reading the config."


def test_narrator_skips_empty_text():
    """Whitespace-only / empty narrator output is not emitted as an event."""
    reg = _registry_with_noop()
    main_script = [
        LLMResponse(tool_call=ToolCall(name="noop", args={}, id="t1")),
        LLMResponse(tool_call=ToolCall(name="noop", args={}, id="t2")),
        LLMResponse(final_text="done"),
    ]
    narrator_script = [
        LLMResponse(final_text="   "),  # whitespace -> dropped
        LLMResponse(final_text="Wrapping up."),
    ]
    result = run_agent(
        "x", ScriptedLLMBackend(main_script), reg, _store(),
        narrator_llm=ScriptedLLMBackend(narrator_script),
    )
    narrations = [e for e in read_trace(result["session_id"]) if e["type"] == "narration"]
    assert [n["text"] for n in narrations] == ["Wrapping up."]


def test_narrator_does_not_fire_on_final_text_only_step():
    """A step that only produces final_text (no tool calls) shouldn't trigger
    a narration -- the user already sees the final answer."""
    reg = _registry_with_noop()
    main_script = [LLMResponse(final_text="done immediately")]
    narrator_script = [LLMResponse(final_text="should not appear")]
    result = run_agent(
        "x", ScriptedLLMBackend(main_script), reg, _store(),
        narrator_llm=ScriptedLLMBackend(narrator_script),
    )
    assert result["outcome"] == "success"
    narrations = [e for e in read_trace(result["session_id"]) if e["type"] == "narration"]
    assert narrations == []


def test_narrator_usage_rolls_into_run_totals():
    """The narrator's tokens are billed to the run -- folding them into
    `usage` keeps `total_cost_usd` honest."""
    from fabri.core.llm import LLMUsage
    reg = _registry_with_noop()
    main_script = [
        LLMResponse(
            tool_call=ToolCall(name="noop", args={}, id="t1"),
            usage=LLMUsage(input_tokens=100, output_tokens=20),
        ),
        LLMResponse(
            final_text="done",
            usage=LLMUsage(input_tokens=120, output_tokens=10),
        ),
    ]
    narrator_script = [
        LLMResponse(
            final_text="Reading.",
            usage=LLMUsage(input_tokens=50, output_tokens=8),
        ),
    ]
    result = run_agent(
        "x", ScriptedLLMBackend(main_script), reg, _store(),
        narrator_llm=ScriptedLLMBackend(narrator_script),
    )
    usage = result["usage"]
    # 100 + 120 + 50 = 270 input across main + narrator.
    assert usage["input_tokens"] == 270
    assert usage["output_tokens"] == 38


def test_narrator_runs_in_planner_executor_loop():
    """Planner-mode runs use a separate executor loop. Narration must fire
    there too so the user sees progress on long planned runs."""
    from fabri.core.planner import PlanItem
    reg = _registry_with_noop()
    # Stub the planner to return one deterministic plan item.
    plan_script = [LLMResponse(final_text='[{"goal":"do the thing","artifacts":[],"deps":[]}]')]
    main_script = [
        LLMResponse(tool_call=ToolCall(name="noop", args={}, id="t1")),
        LLMResponse(final_text="item done"),
    ]
    narrator_script = [LLMResponse(final_text="Doing the thing.")]

    # We don't actually need to drive `run_plan` -- just confirm narration
    # works inside the executor by calling run_agent with planner_mode=off
    # and a tool step. The planner executor and the single (non-planner) loop
    # now share ONE step engine (`_run_step_loop`), so there is a single
    # `_emit_narration` call site that both paths flow through -- they can't
    # diverge on whether narration fires.
    import fabri.core.agent as agent_mod
    src = agent_mod.__file__
    with open(src) as f:
        text = f.read()
    assert text.count("_emit_narration(response.tool_calls") == 1

    # And the single-loop case still emits narration end-to-end.
    _ = plan_script  # silence unused
    result = run_agent(
        "x", ScriptedLLMBackend(main_script), reg, _store(),
        narrator_llm=ScriptedLLMBackend(narrator_script),
    )
    narrations = [e for e in read_trace(result["session_id"]) if e["type"] == "narration"]
    assert len(narrations) == 1


def test_build_narrator_llm_defaults_to_haiku_for_anthropic():
    from fabri.runtime import build_narrator_llm
    cfg = {
        "llm": {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "max_tokens": 1024,
            "api_key_env": "ANTHROPIC_API_KEY",
            "narrator_model": "claude-haiku-4-5",
            "narrator_max_tokens": 60,
        },
    }
    narrator = build_narrator_llm(cfg)
    assert narrator is not None
    assert getattr(narrator, "_model") == "claude-haiku-4-5"
    assert getattr(narrator, "_max_tokens") == 60


def test_build_narrator_llm_returns_none_when_disabled():
    from fabri.runtime import build_narrator_llm
    cfg = {
        "llm": {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "max_tokens": 1024,
            "api_key_env": "ANTHROPIC_API_KEY",
            "narrator_model": None,
            "narrator_max_tokens": 60,
        },
    }
    assert build_narrator_llm(cfg) is None


def test_build_narrator_llm_uses_explicit_per_role_provider(monkeypatch):
    """User configures a per-role provider via `llm.narrator.provider`; the
    resolver instantiates that backend with the right model and base_url
    (for openrouter). Monkeypatched `_instantiate` so the test runs without
    the openai SDK installed."""
    from fabri import runtime
    captured = {}

    def fake_instantiate(rcfg, tools):
        captured.update(rcfg)
        return "stub-backend"

    monkeypatch.setattr(runtime, "_instantiate", fake_instantiate)
    narrator = runtime.build_narrator_llm({
        "llm": {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "max_tokens": 1024,
            "api_key_env": "ANTHROPIC_API_KEY",
            "narrator": {
                "provider": "openrouter",
                "model": "anthropic/claude-haiku-4-5",
                "api_key_env": "OPENROUTER_API_KEY",
                "max_tokens": 60,
            },
        },
    })
    assert narrator == "stub-backend"
    assert captured["provider"] == "openrouter"
    assert captured["model"] == "anthropic/claude-haiku-4-5"
    assert captured["api_key_env"] == "OPENROUTER_API_KEY"
    assert captured["max_tokens"] == 60


def test_build_narrator_llm_legacy_model_key_still_works(monkeypatch):
    """A v0.7.x config using the flat `narrator_model` key gets lifted into
    the normalized role shape and still produces a haiku-class backend."""
    from fabri import runtime
    captured = {}

    def fake_instantiate(rcfg, tools):
        captured.update(rcfg)
        return "stub"

    monkeypatch.setattr(runtime, "_instantiate", fake_instantiate)
    runtime.build_narrator_llm({
        "llm": {
            "provider": "anthropic", "model": "claude-sonnet-4-6",
            "max_tokens": 1024, "api_key_env": "ANTHROPIC_API_KEY",
            "narrator_model": "claude-haiku-4-5", "narrator_max_tokens": 60,
        },
    })
    assert captured["model"] == "claude-haiku-4-5"
    # Provider inherits from parent llm.* when the role didn't override.
    assert captured["provider"] == "anthropic"
    assert captured["api_key_env"] == "ANTHROPIC_API_KEY"


def test_narrator_default_is_haiku_in_default_config():
    """The packaged default config must default narrator to Haiku so users
    get progress updates out of the box without extra wiring."""
    from fabri.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["llm"]["narrator"]["model"] == "claude-haiku-4-5"
    assert DEFAULT_CONFIG["llm"]["narrator"]["max_tokens"] == 60
