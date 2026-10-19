"""DecisionTree: load-time validation, walking to a leaf, requires_approval
passthrough, build_decider, and the `fabri decide` CLI with a fake decider."""

from __future__ import annotations

import copy
import json
import sys

import pytest
import yaml

from fabri.decide import (
    Answer,
    CascadeDecider,
    DecideError,
    DecisionTree,
    JevDecider,
    LLMDecider,
    TreeError,
    build_decider,
)

TREE_YAML = """
name: playbook-router
decider:
  kind: cascade
  primary: {kind: jev, model: typesafe/jev-1.13, provider: openrouter}
  fallback: {kind: llm, llm: {provider: openai, model: gpt-5-mini, api_key_env: OPENAI_API_KEY}}
  thresholds: {choice: 0.6}
start: intent
nodes:
  intent:
    question:
      kind: choice
      instructions: "Which agent?"
      options: {canvas_edit: edit canvas, task_create: make a task, other: anything else}
    branches:
      canvas_edit: {leaf: {agent: canvas}}
      task_create: risky_check
      other: {leaf: {agent: chat}}
  risky_check:
    question: {kind: yesno, instructions: "Is it risky?"}
    branches:
      true: {leaf: {agent: tasks, requires_approval: true}}
      false: {leaf: {agent: tasks}}
"""


def _cfg() -> dict:
    return yaml.safe_load(TREE_YAML)


class _Scripted:
    """Answers each node from a {node: (value, confidence)} map."""

    def __init__(self, script: dict):
        self.script = script
        self.calls: list[str] = []

    def decide(self, state, questions):
        out = {}
        for q in questions:
            self.calls.append(q.key)
            value, conf = self.script[q.key]
            out[q.key] = Answer(key=q.key, kind=q.kind, value=value, confidence=conf,
                                source="jev", cost_usd=0.001, latency_ms=5.0, model="jev")
        return out


def test_loads_and_builds_cascade_decider():
    tree = DecisionTree.from_yaml(_cfg())
    assert tree.name == "playbook-router"
    assert isinstance(tree.decider, CascadeDecider)
    assert isinstance(tree.decider.primary, JevDecider)
    assert tree.decider.primary.provider == "openrouter"
    assert isinstance(tree.decider.fallback, LLMDecider)
    assert tree.decider.thresholds["choice"] == 0.6
    # YAML booleans become "true"/"false" branch keys
    assert set(tree.nodes["risky_check"].branches) == {"true", "false"}


def test_loads_from_path(tmp_path):
    p = tmp_path / "tree.yaml"
    p.write_text(TREE_YAML)
    assert DecisionTree.from_yaml(str(p)).start == "intent"


def test_run_walks_to_leaf_and_passes_requires_approval_through():
    fake = _Scripted({"intent": ("task_create", 0.9), "risky_check": (True, 0.8)})
    tree = DecisionTree.from_yaml(_cfg(), decider=fake)
    result = tree.run("delete everyone's tasks")

    assert fake.calls == ["intent", "risky_check"]
    assert result.nodes == ["intent", "risky_check"]
    assert [a.value for a in result.path] == ["task_create", True]
    assert result.leaf == {"agent": "tasks", "requires_approval": True}
    assert result.cost_usd == pytest.approx(0.002)
    assert result.latency_ms == pytest.approx(10.0)


def test_leaf_is_a_copy_and_flag_survives_repeat_runs():
    fake = _Scripted({"intent": ("task_create", 0.9), "risky_check": (True, 0.8)})
    tree = DecisionTree.from_yaml(_cfg(), decider=fake)
    first = tree.run("s")
    first.leaf["requires_approval"] = False  # caller mutates its copy
    assert tree.run("s").leaf["requires_approval"] is True


def test_false_branch_and_single_node_path():
    tree = DecisionTree.from_yaml(_cfg(), decider=_Scripted({"intent": ("task_create", 0.9),
                                                             "risky_check": (False, 0.9)}))
    assert tree.run("s").leaf == {"agent": "tasks"}
    tree = DecisionTree.from_yaml(_cfg(), decider=_Scripted({"intent": ("other", 0.9)}))
    result = tree.run("s")
    assert result.leaf == {"agent": "chat"} and len(result.path) == 1


def test_unusable_answer_raises():
    class _Err:
        def decide(self, state, questions):
            q = questions[0]
            return {q.key: Answer(key=q.key, kind=q.kind, value=None, error="nope")}

    tree = DecisionTree.from_yaml(_cfg(), decider=_Err())
    with pytest.raises(DecideError, match="no usable answer"):
        tree.run("s")


def test_bad_branch_key_rejected():
    cfg = _cfg()
    cfg["nodes"]["intent"]["branches"]["billing"] = {"leaf": {"agent": "x"}}
    with pytest.raises(TreeError, match="'billing' is not a declared answer"):
        DecisionTree.from_yaml(cfg)


def test_bad_yesno_branch_key_rejected():
    cfg = _cfg()
    cfg["nodes"]["risky_check"]["branches"]["maybe"] = {"leaf": {"agent": "x"}}
    with pytest.raises(TreeError, match="not a declared answer"):
        DecisionTree.from_yaml(cfg)


def test_uncovered_answer_rejected():
    cfg = _cfg()
    del cfg["nodes"]["intent"]["branches"]["other"]
    with pytest.raises(TreeError, match="no branch for answer"):
        DecisionTree.from_yaml(cfg)


def test_missing_node_rejected():
    cfg = _cfg()
    cfg["nodes"]["intent"]["branches"]["task_create"] = "ghost"
    with pytest.raises(TreeError, match="missing node 'ghost'"):
        DecisionTree.from_yaml(cfg)


def test_missing_start_rejected():
    cfg = _cfg()
    cfg["start"] = "nowhere"
    with pytest.raises(TreeError, match="start node"):
        DecisionTree.from_yaml(cfg)


def test_cycle_rejected():
    cfg = _cfg()
    cfg["nodes"]["risky_check"]["branches"][False] = "intent"
    with pytest.raises(TreeError, match="cycle"):
        DecisionTree.from_yaml(cfg)


def test_depth_limit():
    def chain(n: int) -> dict:
        nodes = {}
        for i in range(n):
            nxt = f"n{i + 1}" if i + 1 < n else {"leaf": {"agent": "end"}}
            nodes[f"n{i}"] = {"question": {"kind": "yesno", "instructions": "go?"},
                              "branches": {"true": nxt, "false": {"leaf": {"agent": "stop"}}}}
        return {"name": "deep", "start": "n0", "nodes": nodes,
                "decider": {"kind": "jev"}}

    DecisionTree.from_yaml(chain(16))
    with pytest.raises(TreeError, match="exceeds the maximum of 16"):
        DecisionTree.from_yaml(chain(17))


def test_bad_question_rejected():
    cfg = _cfg()
    cfg["nodes"]["intent"]["question"]["kind"] = "freeform"
    with pytest.raises(TreeError, match="unknown kind"):
        DecisionTree.from_yaml(cfg)


def test_build_decider_errors():
    with pytest.raises(TreeError, match="unknown decider kind"):
        build_decider({"kind": "oracle"})
    with pytest.raises(TreeError, match="primary"):
        build_decider({"kind": "cascade", "primary": {"kind": "jev"}})
    with pytest.raises(TreeError, match="invalid jev decider"):
        build_decider({"kind": "jev", "provider": "nope"})
    with pytest.raises(TreeError, match="invalid llm decider"):
        build_decider({"kind": "llm"})


def _run_cli(monkeypatch, capsys, argv):
    from fabri import cli

    monkeypatch.setattr(sys, "argv", ["fabri", *argv])
    cli.main()
    return capsys.readouterr()


@pytest.fixture
def fake_decider(monkeypatch):
    fake = _Scripted({"intent": ("task_create", 0.95), "risky_check": (True, 0.7)})
    monkeypatch.setattr("fabri.decide.tree.build_decider", lambda cfg: fake)
    return fake


def test_cli_decide_json(tmp_path, monkeypatch, capsys, fake_decider):
    tree_path = tmp_path / "tree.yaml"
    tree_path.write_text(TREE_YAML)
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"message": "close all tasks"}))

    captured = _run_cli(monkeypatch, capsys,
                        ["decide", "--tree", str(tree_path), "--state", f"@{state_path}", "--json"])
    out = json.loads(captured.out)
    assert out["tree"] == "playbook-router"
    assert out["leaf"] == {"agent": "tasks", "requires_approval": True}
    assert [s["key"] for s in out["path"]] == ["intent", "risky_check"]
    assert out["path"][0]["value"] == "task_create"
    assert out["path"][0]["question"] == "Which agent?"
    assert out["path"][1]["confidence"] == 0.7
    assert out["path"][1]["source"] == "jev"
    assert out["path"][1]["escalated"] is False
    assert out["cost_usd"] == pytest.approx(0.002)
    assert out["latency_ms"] == pytest.approx(10.0)


def test_cli_decide_text(tmp_path, monkeypatch, capsys, fake_decider):
    tree_path = tmp_path / "tree.yaml"
    tree_path.write_text(TREE_YAML)
    captured = _run_cli(monkeypatch, capsys, ["decide", "--tree", str(tree_path), "--state", "close tasks"])
    assert "intent: Which agent?" in captured.out
    assert "'task_create' (confidence 0.95, source jev)" in captured.out
    assert '"requires_approval": true' in captured.out
    assert "cost: $0.002000" in captured.out


def test_cli_decide_invalid_tree_exits_1(tmp_path, monkeypatch, capsys):
    cfg = copy.deepcopy(_cfg())
    cfg["nodes"]["intent"]["branches"]["task_create"] = "ghost"
    tree_path = tmp_path / "tree.yaml"
    tree_path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(SystemExit) as exc:
        _run_cli(monkeypatch, capsys, ["decide", "--tree", str(tree_path), "--state", "x"])
    assert exc.value.code == 1
    assert "missing node 'ghost'" in capsys.readouterr().err


# ---------------------------------------------------------------- review fixes

from fabri.decide import Question  # noqa: E402

_YESNO_CHOICE_YAML = """
name: t
start: n
nodes:
  n:
    question:
      kind: choice
      instructions: "Pick"
      options: {OPTIONS}
    branches: {BRANCHES}
"""


def _choice_tree(options: str, branches: str):
    text = _YESNO_CHOICE_YAML.replace("{OPTIONS}", options).replace("{BRANCHES}", branches)
    return DecisionTree.from_yaml(yaml.safe_load(text), decider=_Scripted({}))


@pytest.mark.parametrize("options", ["{yes: approve, no: reject}", "{on: a, off: b}", "{true: a, false: b}"])
def test_choice_bool_option_key_tells_author_to_quote(options):
    with pytest.raises(TreeError, match="quote the option name"):
        _choice_tree(options, "{a: {leaf: {}}}")


def test_quoted_choice_keys_are_not_renamed():
    tree = _choice_tree("{'yes': approve, 'no': reject}", "{'yes': {leaf: {a: 1}}, 'no': {leaf: {a: 2}}}")
    q = tree.nodes["n"].question
    assert q.allowed_values() == ["yes", "no"]
    assert set(tree.nodes["n"].branches) == {"yes", "no"}
    # A quoted 'true' stays the string 'true' for a choice; there is no bool renaming.
    q2 = Question.from_dict("x", {"kind": "choice", "instructions": "?", "options": {"true": None, "maybe": None}})
    assert q2.allowed_values() == ["true", "maybe"]


def test_choice_bool_branch_key_tells_author_to_quote():
    with pytest.raises(TreeError, match="quote the branch key"):
        _choice_tree("{'yes': approve, 'no': reject}", "{yes: {leaf: {}}, no: {leaf: {}}}")


def test_choice_non_string_option_key_rejected():
    with pytest.raises(TreeError, match="option names must be non-empty strings"):
        _choice_tree("{1: one, 2: two}", "{1: {leaf: {}}, 2: {leaf: {}}}")
    with pytest.raises(ValueError, match="option names must be non-empty strings"):
        Question("x", "choice", "?", options={3: None})


def test_score_levels_must_be_strings():
    with pytest.raises(ValueError, match="score levels must be non-empty strings"):
        Question("x", "score", "?", levels=["low", 3])
    with pytest.raises(ValueError, match="quote the level name"):
        Question.from_dict("x", yaml.safe_load("{kind: score, instructions: '?', levels: [no, yes]}"))


def test_yesno_bool_keys_still_normalized():
    q = Question.from_dict("x", yaml.safe_load("{kind: yesno, instructions: '?', options: {true: a, false: b}}"))
    assert q.options == {"true": "a", "false": "b"}


class _Unpriced(_Scripted):
    def decide(self, state, questions):
        out = super().decide(state, questions)
        for a in out.values():
            if a.key == "risky_check":
                a.cost_known, a.cost_usd, a.model = False, 0.0, "mystery-llm"
        return out


def test_tree_result_reports_unknown_cost():
    tree = DecisionTree.from_yaml(_cfg(), decider=_Unpriced({"intent": ("task_create", 0.9),
                                                             "risky_check": (True, 0.9)}))
    result = tree.run("s")
    assert result.cost_known is False
    assert result.unpriced_models == ["mystery-llm"]
    assert result.cost_text() == "cost unknown for mystery-llm"
    d = result.to_dict()
    assert d["cost_known"] is False and d["unpriced_models"] == ["mystery-llm"]

    known = DecisionTree.from_yaml(_cfg(), decider=_Scripted({"intent": ("other", 0.9)})).run("s")
    assert known.cost_known is True and known.cost_text() == "$0.001000"


def test_cli_decide_text_prints_cost_unknown(tmp_path, monkeypatch, capsys):
    fake = _Unpriced({"intent": ("task_create", 0.95), "risky_check": (True, 0.7)})
    monkeypatch.setattr("fabri.decide.tree.build_decider", lambda cfg: fake)
    tree_path = tmp_path / "tree.yaml"
    tree_path.write_text(TREE_YAML)
    captured = _run_cli(monkeypatch, capsys, ["decide", "--tree", str(tree_path), "--state", "x"])
    assert "cost: cost unknown for mystery-llm" in captured.out
    assert "$0.0" not in captured.out


class _StateRecorder(_Scripted):
    def __init__(self, script):
        super().__init__(script)
        self.states: list = []

    def decide(self, state, questions):
        self.states.append(state)
        return super().decide(state, questions)


@pytest.fixture
def state_recorder(monkeypatch):
    fake = _StateRecorder({"intent": ("other", 0.95)})
    monkeypatch.setattr("fabri.decide.tree.build_decider", lambda cfg: fake)
    return fake


def test_cli_state_at_missing_file_is_literal_text(tmp_path, monkeypatch, capsys, state_recorder):
    tree_path = tmp_path / "tree.yaml"
    tree_path.write_text(TREE_YAML)
    monkeypatch.chdir(tmp_path)
    _run_cli(monkeypatch, capsys, ["decide", "--tree", str(tree_path), "--state", "@alice can you help"])
    assert state_recorder.states == ["@alice can you help"]


def test_cli_state_at_existing_file_is_read(tmp_path, monkeypatch, capsys, state_recorder):
    tree_path = tmp_path / "tree.yaml"
    tree_path.write_text(TREE_YAML)
    state_path = tmp_path / "s.json"
    state_path.write_text(json.dumps({"m": 1}))
    _run_cli(monkeypatch, capsys, ["decide", "--tree", str(tree_path), "--state", f"@{state_path}"])
    assert state_recorder.states == [{"m": 1}]


def test_cli_state_file_flag(tmp_path, monkeypatch, capsys, state_recorder):
    tree_path = tmp_path / "tree.yaml"
    tree_path.write_text(TREE_YAML)
    text_path = tmp_path / "msg.txt"
    text_path.write_text("@team please close it")
    _run_cli(monkeypatch, capsys, ["decide", "--tree", str(tree_path), "--state-file", str(text_path)])
    assert state_recorder.states == ["@team please close it"]

    missing = tmp_path / "nope.txt"
    with pytest.raises(SystemExit) as exc:
        _run_cli(monkeypatch, capsys, ["decide", "--tree", str(tree_path), "--state-file", str(missing)])
    assert exc.value.code == 1
    assert "decide error" in capsys.readouterr().err


def test_cli_state_and_state_file_are_exclusive(tmp_path, monkeypatch, capsys):
    tree_path = tmp_path / "tree.yaml"
    tree_path.write_text(TREE_YAML)
    with pytest.raises(SystemExit) as exc:
        _run_cli(monkeypatch, capsys, ["decide", "--tree", str(tree_path), "--state", "x",
                                       "--state-file", str(tree_path)])
    assert exc.value.code == 2
