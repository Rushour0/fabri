"""B6 -- wave planner. Declared dependency edges layer into serial/parallel
waves with auto-assigned ``parallel_group`` tags. All pure functions -- no LLM,
no store, no network."""
from __future__ import annotations

import pytest

from fabri.builder import (
    WaveError,
    plan_waves,
    spawn_descriptors,
)


def _wave_id_sets(plan):
    """Wave ids as sets (intra-wave order is parallel, so order-insensitive)."""
    return [set(w.ids) for w in plan.waves]


def _group_of(plan, item_id):
    for wave in plan.waves:
        if item_id in wave.ids:
            return wave.group
    raise AssertionError(f"{item_id} not placed in any wave")


# ---------------------------------------------------------------------------
# acceptance: "A before B; C independent" -> {A, C} then {B}
# ---------------------------------------------------------------------------


def test_acceptance_a_before_b_c_independent():
    items = [
        {"id": "A", "depends_on": []},
        {"id": "B", "depends_on": ["A"]},
        {"id": "C", "depends_on": []},
    ]
    plan = plan_waves(items)
    assert _wave_id_sets(plan) == [{"A", "C"}, {"B"}]
    # A and C parallelize (same group); B is in a later group.
    assert _group_of(plan, "A") == _group_of(plan, "C")
    assert _group_of(plan, "B") != _group_of(plan, "A")
    assert [w.group for w in plan.waves] == ["wave1", "wave2"]


# ---------------------------------------------------------------------------
# linear chain
# ---------------------------------------------------------------------------


def test_linear_chain_is_one_item_per_wave():
    items = [
        {"id": "a", "depends_on": []},
        {"id": "b", "depends_on": ["a"]},
        {"id": "c", "depends_on": ["b"]},
    ]
    plan = plan_waves(items)
    assert [w.ids for w in plan.waves] == [["a"], ["b"], ["c"]]
    assert [w.group for w in plan.waves] == ["wave1", "wave2", "wave3"]


# ---------------------------------------------------------------------------
# independent set
# ---------------------------------------------------------------------------


def test_independent_set_is_a_single_wave():
    items = [{"id": x} for x in ("x", "y", "z")]
    plan = plan_waves(items)
    assert len(plan.waves) == 1
    assert plan.waves[0].ids == ["x", "y", "z"]  # declaration order preserved
    assert plan.waves[0].group == "wave1"


# ---------------------------------------------------------------------------
# diamond: A -> {B, C} -> D
# ---------------------------------------------------------------------------


def test_diamond_groups_middle_layer():
    items = [
        {"id": "A", "depends_on": []},
        {"id": "B", "depends_on": ["A"]},
        {"id": "C", "depends_on": ["A"]},
        {"id": "D", "depends_on": ["B", "C"]},
    ]
    plan = plan_waves(items)
    assert _wave_id_sets(plan) == [{"A"}, {"B", "C"}, {"D"}]
    # D lands after BOTH of its deps, not just the first.
    assert _group_of(plan, "B") == _group_of(plan, "C")
    assert _group_of(plan, "D") == "wave3"


# ---------------------------------------------------------------------------
# cycle detection
# ---------------------------------------------------------------------------


def test_cycle_raises():
    items = [
        {"id": "A", "depends_on": ["B"]},
        {"id": "B", "depends_on": ["A"]},
    ]
    with pytest.raises(WaveError, match="cycle"):
        plan_waves(items)


def test_self_dependency_raises():
    with pytest.raises(WaveError, match="itself"):
        plan_waves([{"id": "A", "depends_on": ["A"]}])


def test_dangling_dependency_raises():
    with pytest.raises(WaveError, match="unknown id"):
        plan_waves([{"id": "A", "depends_on": ["ghost"]}])


def test_duplicate_id_raises():
    with pytest.raises(WaveError, match="duplicate"):
        plan_waves([{"id": "A"}, {"id": "A"}])


def test_missing_id_raises():
    with pytest.raises(WaveError, match="missing an 'id'"):
        plan_waves([{"depends_on": []}])


# ---------------------------------------------------------------------------
# spawn_descriptors: group name per item, wave-ordered
# ---------------------------------------------------------------------------


def test_spawn_descriptors_tag_each_item_with_its_group():
    items = [
        {"id": "A", "depends_on": []},
        {"id": "B", "depends_on": ["A"]},
        {"id": "C", "depends_on": []},
    ]
    descriptors = spawn_descriptors(plan_waves(items))
    by_id = {d.id: d.parallel_group for d in descriptors}
    assert by_id == {"A": "wave1", "C": "wave1", "B": "wave2"}
    # Sequence is wave-ordered: every wave1 item precedes any wave2 item.
    groups = [d.parallel_group for d in descriptors]
    assert groups == ["wave1", "wave1", "wave2"]


def test_spawn_descriptors_round_trip_to_dict():
    descriptors = spawn_descriptors(plan_waves([{"id": "only"}]))
    assert [d.to_dict() for d in descriptors] == [
        {"id": "only", "parallel_group": "wave1"}
    ]


# ---------------------------------------------------------------------------
# input flexibility + edge cases
# ---------------------------------------------------------------------------


def test_empty_input_yields_empty_plan():
    plan = plan_waves([])
    assert plan.waves == []
    assert spawn_descriptors(plan) == []


def test_custom_group_prefix():
    plan = plan_waves([{"id": "a"}, {"id": "b", "depends_on": ["a"]}], group_prefix="layer")
    assert [w.group for w in plan.waves] == ["layer1", "layer2"]


def test_accepts_objects_with_attributes():
    class _Item:
        def __init__(self, id, depends_on=None):
            self.id = id
            self.depends_on = depends_on or []

    plan = plan_waves([_Item("a"), _Item("b", ["a"])])
    assert [w.ids for w in plan.waves] == [["a"], ["b"]]


def test_non_string_ids_are_coerced():
    plan = plan_waves([{"id": 1}, {"id": 2, "depends_on": [1]}])
    assert [w.ids for w in plan.waves] == [["1"], ["2"]]
