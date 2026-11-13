"""Offline run of the specialist eval harness (D5) with the scripted model."""
import json

from fabri.benchmarks import specialist_eval as ev


def _mock(tasks):
    return lambda role: ev.PlanFollowingLLM(tasks)


def test_all_arms_solve_the_fixture_offline():
    tasks = ev.eval_tasks()
    report = ev.evaluate(_mock(tasks), tasks)
    assert set(report["arms"]) == {"direct", "retrieval", "specialists"}
    for arm in report["arms"].values():
        assert arm["success_rate"] == 1.0
        assert arm["cost_usd"] > 0


def test_parent_context_shrinks_from_direct_to_retrieval_to_specialists():
    tasks = ev.eval_tasks()[:2]
    arms = ev.evaluate(_mock(tasks), tasks)["arms"]
    direct, retrieval, spec = (arms[a]["mean_parent_peak_tokens"] for a in ("direct", "retrieval", "specialists"))
    assert direct > retrieval > spec


def test_specialist_arm_counts_designer_and_child_calls():
    tasks = ev.eval_tasks()[:1]
    arms = ev.evaluate(_mock(tasks), tasks)["arms"]
    # parent: delegate + answer; designer: 1; child: lookups + answer.
    assert arms["specialists"]["llm_calls"] == 5
    assert arms["direct"]["llm_calls"] == 2


def test_retrieval_miss_fails_the_task():
    # With one retrieved tool the plan's second lookup is invisible.
    tasks = ev.eval_tasks()[:1]
    report = ev.evaluate(_mock(tasks), tasks, arms=("retrieval",), top_k=1)
    assert report["arms"]["retrieval"]["success_rate"] == 0.0


def test_unknown_cost_is_reported_as_none():
    tasks = ev.eval_tasks()[:1]
    report = ev.evaluate(_mock(tasks), tasks, arms=("direct",), cost_fn=lambda u: None)
    assert report["arms"]["direct"]["cost_usd"] is None


def test_cli_defaults_to_offline(capsys):
    assert ev.main(["--tasks", "1", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"].startswith("mock")


def test_live_requires_a_config(capsys):
    import pytest

    with pytest.raises(SystemExit):
        ev.main(["--live"])
