"""Offline integration contracts for fabri's two self-improvement paths."""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from fabri import cli
from fabri.core import agent
from fabri.core.llm import LLMResponse, LLMUsage, ToolCall
from fabri.tools.registry import ToolRegistry


pytestmark = pytest.mark.integration

_TOOLS_DIR = Path(__file__).parents[1] / "src" / "fabri" / "tools" / "examples"
_LESSON = "For this acknowledgement task, answer directly; do not inspect the workspace."


class _LessonAwareBackend:
    """A deterministic stand-in whose policy changes only when the lesson arrives."""

    def __init__(self) -> None:
        self.systems: list[str] = []

    def step(self, system: str, _messages: list[dict]) -> LLMResponse:
        self.systems.append(system)
        usage = LLMUsage(input_tokens=1_000, output_tokens=100, model="gpt-4o-mini")
        if _LESSON in system:
            return LLMResponse(final_text="Acknowledged.", usage=usage)
        if len(self.systems) == 1:
            return LLMResponse(
                tool_call=ToolCall(name="list_dir", args={"path": "."}, id="t1"),
                usage=usage,
            )
        return LLMResponse(final_text="Acknowledged.", usage=usage)


def test_retrieved_lesson_removes_a_priced_turn(tmp_path, monkeypatch):
    """A learned runtime hint reaches the agent and reduces measured COGS."""
    monkeypatch.setenv("FABRI_HOME", str(tmp_path / "home"))
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("FABRI_SANDBOX_ROOT", str(sandbox))
    tools = ToolRegistry(_TOOLS_DIR)

    def run_with(context: str, meta: dict[str, int]):
        monkeypatch.setattr(agent, "retrieve_context_with_meta", lambda *_args, **_kwargs: (context, meta))
        backend = _LessonAwareBackend()
        result = agent.run_agent("acknowledge the deployment", backend, tools, store=object())
        return result, backend

    baseline, baseline_backend = run_with("", {"retrieved": 0, "from_prior_sessions": 0, "strategic": 0})
    learned, learned_backend = run_with(
        f"<retrieved_guidelines>\n- [strategic] {_LESSON}\n</retrieved_guidelines>",
        {"retrieved": 1, "from_prior_sessions": 1, "strategic": 1},
    )

    assert _LESSON not in baseline_backend.systems[0]
    assert _LESSON in learned_backend.systems[0]
    assert baseline["outcome"] == learned["outcome"] == "success"
    assert baseline["usage"]["step_count"] == 2
    assert learned["usage"]["step_count"] == 1
    assert baseline["usage"]["total_cost_usd"] == pytest.approx(0.00042)
    assert learned["usage"]["total_cost_usd"] == pytest.approx(0.00021)
    assert learned["usage"]["total_cost_usd"] == pytest.approx(
        baseline["usage"]["total_cost_usd"] / 2
    )


def test_open_pr_projects_promoted_guidelines_without_editing_workspace(tmp_path, monkeypatch, capsys):
    """Promoted lessons become a reviewable draft, never an in-place prompt edit."""
    config_path = tmp_path / "agent.yaml"
    original = "agent:\n  name: scout\n  system_prompt: |\n    Be useful.\nmemory: {}\n"
    config_path.write_text(original)
    calls: dict[str, tuple] = {}

    class Store:
        def iterate(self, kind, limit):
            assert (kind, limit) == ("strategic", 10)
            return [type("Guideline", (), {"text": "Check assumptions before acting."})()]

    class Provider:
        def push_branch(self, *args):
            calls["push"] = args

        def open_or_update_pr(self, *args):
            calls["pr"] = args
            return "https://example.test/acme/widget/pull/1"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda _path: {"agent": {"name": "scout"}, "memory": {}})
    monkeypatch.setattr(cli, "_open_store", lambda _memory: Store())
    monkeypatch.setattr(cli, "get_provider", lambda _name, _token: Provider())

    cli.cmd_repo_open_pr(
        Namespace(config="agent.yaml", repo="acme/widget", token="token", provider="github", kind="strategic", limit=10, base="main", branch="fabri/self-improve")
    )

    pushed = calls["push"]
    assert pushed[3] == "agent.yaml"
    assert "# --- fabri: learned from prior runs (auto-proposed) ---" in pushed[4]
    assert "# - Check assumptions before acting." in pushed[4]
    assert config_path.read_text() == original
    assert calls["pr"][3] == "open-pr:scout"
    assert capsys.readouterr().out == "https://example.test/acme/widget/pull/1\n"
