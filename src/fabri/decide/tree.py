"""Decision trees: typed questions chained into a router.

A tree is a set of named nodes. Each node asks one question and maps every
possible answer to either another node (by name) or a leaf (``{leaf: {...}}``).
Running a tree walks from `start`, asking one question per node through the
tree's decider, until it reaches a leaf. The leaf mapping is returned as
written: fabri never adds, drops or clears keys on it, so a leaf that sets
``requires_approval: true`` reaches the caller with it set.

Validation happens at load so a broken tree fails before any provider call:
every branch key must be a declared option, level, or true/false; every answer
must have a branch; every referenced node must exist; no cycles; depth at most
`MAX_DEPTH` nodes.

YAML shape::

    name: playbook-router
    decider: {kind: cascade, primary: {kind: jev, ...}, fallback: {kind: llm, llm: {...}}}
    start: intent
    nodes:
      intent:
        question: {kind: choice, instructions: "...", options: {a: "...", b: "..."}}
        branches: {a: {leaf: {agent: x}}, b: next_node}
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from fabri.decide.types import Answer, DecideError, Decider, Question, _side_key

MAX_DEPTH = 16


class TreeError(ValueError):
    """The decision tree config is invalid."""


def build_decider(cfg: dict) -> Decider:
    """Build a decider from a config block. Kinds:

    - ``{kind: jev, model, provider, api_key_env, base_url, timeout_s}``
    - ``{kind: llm, llm: {provider, model, api_key_env, max_tokens, base_url}}``
    - ``{kind: cascade, primary: <decider>, fallback: <decider>, thresholds: {...}}``
    """
    if not isinstance(cfg, dict):
        raise TreeError("decider must be a mapping with a 'kind'")
    kind = cfg.get("kind")
    try:
        if kind == "jev":
            from fabri.decide.jev import JevDecider

            kwargs = {
                k: cfg[k]
                for k in ("model", "provider", "api_key_env", "base_url", "timeout_s")
                if cfg.get(k) is not None
            }
            return JevDecider(**kwargs)
        if kind == "llm":
            from fabri.decide.llm import LLMDecider

            return LLMDecider(cfg.get("llm") or {})
        if kind == "cascade":
            from fabri.decide.cascade import CascadeDecider

            if "primary" not in cfg or "fallback" not in cfg:
                raise TreeError("a cascade decider needs 'primary' and 'fallback'")
            return CascadeDecider(
                build_decider(cfg["primary"]),
                build_decider(cfg["fallback"]),
                thresholds=cfg.get("thresholds"),
            )
    except TreeError:
        raise
    except (TypeError, ValueError) as e:
        raise TreeError(f"invalid {kind} decider: {e}") from None
    raise TreeError(f"unknown decider kind {kind!r} (expected jev, llm or cascade)")


@dataclass
class TreeNode:
    name: str
    question: Question
    # answer key ("true"/"false" for yesno) -> node name (str) or leaf (dict)
    branches: dict[str, str | dict]


@dataclass
class TreeResult:
    path: list[Answer]
    leaf: dict
    cost_usd: float
    latency_ms: float
    nodes: list[str] = field(default_factory=list)

    @property
    def cost_known(self) -> bool:
        """False when any answer on the path came from a model fabri could
        not price; `cost_usd` then undercounts."""
        return all(a.cost_known for a in self.path)

    @property
    def unpriced_models(self) -> list[str]:
        """Model ids on the path whose cost is unknown, in path order."""
        out: list[str] = []
        for a in self.path:
            if not a.cost_known and a.model not in out:
                out.append(a.model)
        return out

    def cost_text(self) -> str:
        """Human-readable cost: ``$0.001234``, or ``cost unknown for <model>``
        when any step could not be priced."""
        if self.cost_known:
            return f"${self.cost_usd:.6f}"
        return "cost unknown for " + ", ".join(m or "unknown model" for m in self.unpriced_models)

    def to_dict(self) -> dict:
        return {
            "nodes": list(self.nodes),
            "path": [a.to_dict() for a in self.path],
            "leaf": copy.deepcopy(self.leaf),
            "cost_usd": self.cost_usd,
            "cost_known": self.cost_known,
            "unpriced_models": self.unpriced_models,
            "latency_ms": self.latency_ms,
        }


class DecisionTree:
    def __init__(self, name: str, start: str, nodes: dict[str, TreeNode], decider: Decider) -> None:
        self.name = name
        self.start = start
        self.nodes = nodes
        self.decider = decider
        _validate_graph(start, nodes)

    @classmethod
    def from_yaml(cls, source: str | Path | dict, decider: Decider | None = None) -> DecisionTree:
        """Load from a YAML path or an already-parsed mapping. `decider`
        overrides the config's ``decider`` block (handy in tests)."""
        if isinstance(source, dict):
            data = copy.deepcopy(source)
        else:
            try:
                data = yaml.safe_load(Path(source).read_text())
            except OSError as e:
                raise TreeError(f"cannot read tree {source}: {e}") from None
            except yaml.YAMLError as e:
                raise TreeError(f"tree {source} is not valid YAML: {e}") from None
        if not isinstance(data, dict):
            raise TreeError("tree config must be a mapping")

        raw_nodes = data.get("nodes")
        if not isinstance(raw_nodes, dict) or not raw_nodes:
            raise TreeError("tree needs a non-empty 'nodes' mapping")
        start = data.get("start")
        if not isinstance(start, str) or not start:
            raise TreeError("tree needs a 'start' node name")

        nodes: dict[str, TreeNode] = {}
        for name, raw in raw_nodes.items():
            name = str(name)
            if not isinstance(raw, dict):
                raise TreeError(f"node {name!r} must be a mapping")
            try:
                question = Question.from_dict(name, raw.get("question") or {})
            except ValueError as e:
                raise TreeError(str(e)) from None
            nodes[name] = TreeNode(name, question, _parse_branches(name, question, raw.get("branches")))

        if decider is None:
            if "decider" not in data:
                raise TreeError("tree needs a 'decider' block")
            decider = build_decider(data["decider"])
        return cls(str(data.get("name") or "tree"), start, nodes, decider)

    def run(self, state: object) -> TreeResult:
        """Walk the tree from `start` to a leaf. Raises DecideError when a
        node's answer is missing or invalid, since there is no branch to take."""
        path: list[Answer] = []
        visited: list[str] = []
        current = self.start
        for _ in range(MAX_DEPTH):
            node = self.nodes[current]
            visited.append(current)
            answers = self.decider.decide(state, [node.question])
            answer = answers.get(node.question.key)
            if answer is None:
                raise DecideError(f"tree {self.name!r}: node {current!r} got no answer")
            path.append(answer)
            if answer.error is not None or answer.value is None:
                raise DecideError(
                    f"tree {self.name!r}: node {current!r} has no usable answer: {answer.error}"
                )
            target = node.branches.get(_side_key(answer.value))
            if target is None:
                raise DecideError(
                    f"tree {self.name!r}: node {current!r} has no branch for {answer.value!r}"
                )
            if isinstance(target, dict):
                return TreeResult(
                    path=path,
                    leaf=copy.deepcopy(target),
                    cost_usd=sum(a.cost_usd for a in path),
                    latency_ms=sum(a.latency_ms for a in path),
                    nodes=visited,
                )
            current = target
        raise DecideError(f"tree {self.name!r}: exceeded max depth {MAX_DEPTH}")  # pragma: no cover


def _parse_branches(name: str, question: Question, raw: object) -> dict[str, str | dict]:
    if not isinstance(raw, dict) or not raw:
        raise TreeError(f"node {name!r} needs a non-empty 'branches' mapping")
    allowed = [_side_key(v) for v in question.allowed_values()]
    branches: dict[str, str | dict] = {}
    for key, target in raw.items():
        if question.kind != "yesno" and isinstance(key, bool):
            raise TreeError(
                f"node {name!r}: branch key {key!r} is a boolean, not a string; YAML reads "
                "bare yes/no/on/off/true/false as booleans, so quote the branch key"
            )
        k = _side_key(key)
        if k not in allowed:
            raise TreeError(
                f"node {name!r}: branch {k!r} is not a declared answer (expected one of {allowed!r})"
            )
        if isinstance(target, str):
            branches[k] = target
        elif isinstance(target, dict) and set(target) == {"leaf"} and isinstance(target["leaf"], dict):
            branches[k] = copy.deepcopy(target["leaf"])
        else:
            raise TreeError(
                f"node {name!r}: branch {k!r} must be a node name or {{leaf: {{...}}}}"
            )
    missing = [a for a in allowed if a not in branches]
    if missing:
        raise TreeError(f"node {name!r}: no branch for answer(s) {missing!r}")
    return branches


def _validate_graph(start: str, nodes: dict[str, TreeNode]) -> None:
    if start not in nodes:
        raise TreeError(f"start node {start!r} does not exist")
    for node in nodes.values():
        for key, target in node.branches.items():
            if isinstance(target, str) and target not in nodes:
                raise TreeError(f"node {node.name!r}: branch {key!r} points to missing node {target!r}")

    # Longest path in nodes from each node; a node seen again on the current
    # path is a cycle.
    depth: dict[str, int] = {}
    on_path: list[str] = []

    def walk(name: str) -> int:
        if name in on_path:
            cycle = on_path[on_path.index(name):] + [name]
            raise TreeError(f"tree has a cycle: {' -> '.join(cycle)}")
        if name in depth:
            return depth[name]
        on_path.append(name)
        below = [walk(t) for t in nodes[name].branches.values() if isinstance(t, str)]
        on_path.pop()
        depth[name] = 1 + max(below, default=0)
        return depth[name]

    for name in nodes:
        walk(name)
    if depth[start] > MAX_DEPTH:
        raise TreeError(f"tree depth {depth[start]} exceeds the maximum of {MAX_DEPTH}")
