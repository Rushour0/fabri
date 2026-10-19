"""GPT-5 reasoning_effort defaults when function tools are attached."""

from __future__ import annotations

import pytest

from fabri.core.llm import OpenAILLMBackend, _default_tool_reasoning_effort
from fabri.runtime import build_llm


@pytest.mark.parametrize(
    ("model", "effort"),
    [
        ("gpt-5", "minimal"),
        ("gpt-5-mini", "minimal"),
        ("gpt-5-nano", "minimal"),
        ("gpt-5-mini-2025-08-07", "minimal"),
        ("gpt-5.1", "none"),
        ("gpt-5.6-terra", "none"),
        ("gpt-5.4-mini", "none"),
    ],
)
def test_default_effort_per_model(model: str, effort: str) -> None:
    # gpt-5-mini answers 400 to 'none' ("supported values are minimal, low,
    # medium, high"); the dotted 5.x models need 'none' to take tools.
    assert _default_tool_reasoning_effort(model) == effort


class _Completions:
    def __init__(self) -> None:
        self.kwargs: dict = {}

    def create(self, **kwargs):
        self.kwargs = kwargs
        raise RuntimeError("stop after capturing the request")


def _capture(monkeypatch: pytest.MonkeyPatch, backend: OpenAILLMBackend) -> dict:
    completions = _Completions()
    monkeypatch.setattr(backend._client.chat, "completions", completions)
    with pytest.raises(Exception):
        backend.step("sys", [{"role": "user", "content": "hi"}])
    return completions.kwargs


TOOL = [{"name": "t", "description": "d", "input_schema": {"type": "object"}}]


def test_gpt5_mini_with_tools_sends_minimal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    kwargs = _capture(monkeypatch, OpenAILLMBackend(model="gpt-5-mini", tools=TOOL))
    assert kwargs["reasoning_effort"] == "minimal"
    assert "max_completion_tokens" in kwargs


def test_explicit_effort_wins_and_flows_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    backend = build_llm({"llm": {"provider": "openai", "model": "gpt-5-mini", "reasoning_effort": "low"}}, TOOL)
    assert _capture(monkeypatch, backend)["reasoning_effort"] == "low"


def test_no_tools_sends_no_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    assert "reasoning_effort" not in _capture(monkeypatch, OpenAILLMBackend(model="gpt-5-mini"))
