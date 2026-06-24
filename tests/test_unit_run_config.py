"""AgentRunConfig: the single value object that keeps run/replay/agent-runner
from drifting in which orchestration knobs they pass to run_agent."""
import copy

from fabri.config import load_config
from fabri.core.run_config import AgentRunConfig, planner_mode_from_cfg


def _cfg():
    # load_config(None) shares DEFAULT_CONFIG's nested dicts; deep-copy before
    # mutating so a test can't leak into the module-global default.
    return copy.deepcopy(load_config(None))


def test_planner_mode_enabled_flag_wins_over_stale_mode():
    assert planner_mode_from_cfg({"enabled": False, "mode": "force"}) == "off"
    assert planner_mode_from_cfg({"enabled": True, "mode": "force"}) == "force"
    assert planner_mode_from_cfg({"enabled": True, "mode": "auto"}) == "auto"
    assert planner_mode_from_cfg({}) == "off"
    assert planner_mode_from_cfg({"mode": "bogus"}) == "off"


def test_from_config_defaults_roundtrip():
    cfg = _cfg()
    rc = AgentRunConfig.from_config(cfg)
    kwargs = rc.as_kwargs()
    # The knobs replay/agent-runner historically dropped must be present and
    # sourced from config, not silently defaulted away.
    assert kwargs["max_steps"] == cfg["agent"]["max_steps"]
    assert kwargs["planner_mode"] == "off"
    assert kwargs["tool_retrieval_enabled"] is False
    assert kwargs["max_cost_usd"] is None
    assert kwargs["result_format"] == cfg["tools"]["result_format"]
    assert set(kwargs["tool_retrieval_always_include"]) == set(
        cfg["tools"]["retrieval"]["always_include"]
    )


def test_from_config_carries_planner_and_budget():
    cfg = _cfg()
    cfg["agent"]["planner"] = {"enabled": True, "mode": "force", "max_items": 3,
                               "auto_token_threshold": 5}
    cfg["agent"]["max_cost_usd"] = 0.25
    cfg["tools"]["retrieval"] = {"enabled": True, "top_k": 4, "always_include": ["x"]}
    rc = AgentRunConfig.from_config(cfg)
    assert rc.planner_mode == "force"
    assert rc.planner_max_items == 3
    assert rc.max_cost_usd == 0.25
    assert rc.tool_retrieval_enabled is True
    assert rc.tool_retrieval_top_k == 4


def test_for_subagent_swaps_only_budget():
    cfg = _cfg()
    cfg["agent"]["planner"] = {"enabled": True, "mode": "auto", "max_items": 6,
                               "auto_token_threshold": 9}
    rc = AgentRunConfig.from_config(cfg)
    child = rc.for_subagent(max_steps=3, max_cost_usd=0.1)
    assert child.max_steps == 3
    assert child.max_cost_usd == 0.1
    # Orchestration shape is inherited, not reset.
    assert child.planner_mode == rc.planner_mode == "auto"
    assert child.planner_max_items == 6


def test_as_kwargs_matches_run_agent_signature():
    import inspect
    from fabri.core.agent import run_agent

    sig = set(inspect.signature(run_agent).parameters)
    for key in AgentRunConfig().as_kwargs():
        assert key in sig, f"as_kwargs produced {key!r} which run_agent does not accept"
