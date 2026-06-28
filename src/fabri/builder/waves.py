"""B6 -- decompose/wave planner as a builder primitive.

Hand-authoring ``parallel_group`` tags for fan-out is the friction this closes:
declare *dependency edges* between work items and let the layering decide the
serial/parallel spawn shape. From "A before B; C independent" you get the wave
plan ``[{A, C}, {B}]`` -- A and C run in parallel in the first wave, B follows
in the second -- and a ``parallel_group`` name per item ready to hand to
``spawn_subagent``.

Why not reuse :func:`core.planner.topological_order`? It returns a *flat*
dependency-resolved order (no wave boundaries) and deliberately *tolerates*
cycles by falling back to declared order so a planner mistake never stops the
executor. A builder primitive wants the opposite on both counts: explicit waves
for fan-out, and a hard error on a cycle so a bad edge declaration is caught at
build time rather than silently serialized. So this module does its own
indegree layering (Kahn's algorithm, one wave per level).

All pure functions -- no LLM, no store, no network.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping


class WaveError(ValueError):
    """Raised when a wave plan cannot be built: a dependency cycle, a self-loop,
    a dangling edge (depends_on an id that no item declares), or a duplicate id.
    """


@dataclass
class Wave:
    """One layer of the plan: ids with no dependency on each other, all of whose
    dependencies live in earlier waves. `group` is the ``parallel_group`` tag a
    caller hands to ``spawn_subagent`` so the whole wave fans out concurrently.
    """

    group: str
    ids: list[str]

    def to_dict(self) -> dict:
        return {"group": self.group, "ids": list(self.ids)}


@dataclass
class WavePlan:
    """An ordered list of :class:`Wave`s. Wave ``n`` may start only once every
    earlier wave has finished; within a wave the ids are independent."""

    waves: list[Wave] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"waves": [w.to_dict() for w in self.waves]}


@dataclass
class SpawnDescriptor:
    """The spawn shape for one item: its id and the ``parallel_group`` tag it
    carries. Pass `parallel_group` straight to ``spawn_subagent``; items sharing
    a group fan out concurrently, items in later groups run after earlier ones.
    """

    id: str
    parallel_group: str

    def to_dict(self) -> dict:
        return {"id": self.id, "parallel_group": self.parallel_group}


@dataclass
class _Node:
    id: str
    depends_on: list[str]


def _normalize(items: Iterable) -> list[_Node]:
    """Coerce input items into ``(id, depends_on)`` nodes, preserving declared
    order. Accepts mappings (``{"id": ..., "depends_on": [...]}``) or any object
    exposing ``.id`` / ``.depends_on`` attributes. Missing ``depends_on`` means
    no dependencies. Ids are coerced to ``str`` so callers may use ints or
    enums."""
    nodes: list[_Node] = []
    for item in items:
        if isinstance(item, Mapping):
            raw_id = item.get("id")
            raw_deps = item.get("depends_on")
        else:
            raw_id = getattr(item, "id", None)
            raw_deps = getattr(item, "depends_on", None)
        if raw_id is None or (isinstance(raw_id, str) and not raw_id.strip()):
            raise WaveError(f"item is missing an 'id': {item!r}")
        deps = [str(d) for d in raw_deps] if raw_deps else []
        nodes.append(_Node(id=str(raw_id), depends_on=deps))
    return nodes


def plan_waves(items: Iterable, *, group_prefix: str = "wave") -> WavePlan:
    """Layer `items` into dependency waves and tag each wave with a
    ``parallel_group`` name.

    Each item carries an ``id`` and a ``depends_on`` list of ids that must
    finish first. The result is an ordered :class:`WavePlan`: wave 1 holds every
    item with no dependencies, wave 2 holds items whose dependencies are all
    satisfied by wave 1, and so on. Items within a wave are independent and may
    run in parallel; the wave's `group` is ``f"{group_prefix}1"``,
    ``f"{group_prefix}2"``, ... Order within a wave follows declaration order so
    the shape is deterministic.

    Raises :class:`WaveError` on a duplicate id, a self-dependency, a dangling
    dependency (an id that no item declares), or a dependency cycle.
    """
    nodes = _normalize(items)
    if not nodes:
        return WavePlan(waves=[])

    order = [n.id for n in nodes]
    known = set(order)
    if len(known) != len(order):
        dupes = sorted({i for i in order if order.count(i) > 1})
        raise WaveError(f"duplicate item id(s): {', '.join(dupes)}")

    indeg: dict[str, int] = {i: 0 for i in order}
    children: dict[str, list[str]] = {i: [] for i in order}
    for node in nodes:
        for dep in node.depends_on:
            if dep == node.id:
                raise WaveError(f"item {node.id!r} depends on itself")
            if dep not in known:
                raise WaveError(
                    f"item {node.id!r} depends on unknown id {dep!r}"
                )
            indeg[node.id] += 1
            children[dep].append(node.id)

    waves: list[Wave] = []
    placed = 0
    ready = [i for i in order if indeg[i] == 0]
    while ready:
        waves.append(Wave(group=f"{group_prefix}{len(waves) + 1}", ids=list(ready)))
        placed += len(ready)
        freed: set[str] = set()
        for i in ready:
            for child in children[i]:
                indeg[child] -= 1
                if indeg[child] == 0:
                    freed.add(child)
        # Re-impose declared order on the next wave for a deterministic shape.
        ready = [i for i in order if i in freed]

    if placed != len(order):
        stuck = sorted(i for i in order if indeg[i] > 0)
        raise WaveError(f"dependency cycle among: {', '.join(stuck)}")

    return WavePlan(waves=waves)


def spawn_descriptors(plan: WavePlan) -> list[SpawnDescriptor]:
    """Flatten a :class:`WavePlan` into the wave-ordered sequence of
    :class:`SpawnDescriptor`s a caller would iterate to spawn each item with its
    ``parallel_group`` tag. Order is wave-by-wave, declaration order within a
    wave -- the same shape an executor walks to fan out then join."""
    return [
        SpawnDescriptor(id=item_id, parallel_group=wave.group)
        for wave in plan.waves
        for item_id in wave.ids
    ]
