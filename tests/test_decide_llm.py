"""LLMDecider: one call, answers constrained to the option set, cost from
usage. The backend is a ScriptedLLMBackend (via a monkeypatched
runtime._instantiate) so nothing touches the network."""

from __future__ import annotations

import json

import pytest

from fabri.core.llm import LLMError, LLMResponse, LLMUsage, ScriptedLLMBackend
from fabri.decide import DecideError, LLMDecider, Question

CFG = {"provider": "openai", "model": "gpt-5-mini", "api_key_env": "OPENAI_API_KEY", "max_tokens": 200}

QUESTIONS = [
    Question("intent", "choice", "Which agent?", options={"canvas": "edit", "tasks": None}),
    Question("urgency", "score", "How urgent?", levels=["low", "high"]),
    Question("risky", "yesno", "Is it risky?"),
]


class _Recording(ScriptedLLMBackend):
    def __init__(self, script):
        super().__init__(script)
        self.calls: list[tuple[str, list[dict]]] = []

    def step(self, system, messages):
        self.calls.append((system, messages))
        return super().step(system, messages)


def _decider(monkeypatch, text: str, usage: LLMUsage | None = None):
    backend = _Recording([LLMResponse(final_text=text, usage=usage)])
    seen: dict = {}

    def fake_instantiate(rcfg, tool_defs):
        seen["rcfg"], seen["tools"] = rcfg, tool_defs
        return backend

    monkeypatch.setattr("fabri.runtime._instantiate", fake_instantiate)
    return LLMDecider(CFG), backend, seen


def test_valid_answers_and_single_call(monkeypatch):
    text = '```json\n{"intent": "tasks", "urgency": "high", "risky": true}\n```'
    usage = LLMUsage(input_tokens=1000, output_tokens=100, model="gpt-5-mini")
    decider, backend, seen = _decider(monkeypatch, text, usage)
    out = decider.decide("make a task", QUESTIONS)

    assert seen["rcfg"]["model"] == "gpt-5-mini" and seen["tools"] == []
    assert len(backend.calls) == 1
    system, messages = backend.calls[0]
    assert "JSON object" in system
    assert len(messages) == 1 and messages[0]["role"] == "user"
    content = messages[0]["content"]
    assert "make a task" in content and "canvas" in content and "low, high" in content

    assert out["intent"].value == "tasks"
    assert out["urgency"].value == "high"
    assert out["risky"].value is True
    for a in out.values():
        assert a.source == "llm"
        assert a.confidence is None
        assert a.error is None
        assert a.model == "gpt-5-mini"
    # gpt-5-mini: $0.25/MTok in, $2.0/MTok out
    expected = (1000 * 0.25 + 100 * 2.0) / 1_000_000
    assert sum(a.cost_usd for a in out.values()) == pytest.approx(expected, rel=0.01)


def test_out_of_set_answer_rejected(monkeypatch):
    decider, _, _ = _decider(monkeypatch, json.dumps({"intent": "billing", "urgency": "medium", "risky": "maybe"}))
    out = decider.decide("s", QUESTIONS)
    for key in ("intent", "urgency", "risky"):
        assert out[key].value is None
        assert out[key].error
    assert "billing" in out["intent"].error


def test_yesno_string_booleans_accepted(monkeypatch):
    decider, _, _ = _decider(monkeypatch, json.dumps({"intent": "canvas", "urgency": "low", "risky": "False"}))
    assert decider.decide("s", QUESTIONS)["risky"].value is False


def test_missing_key_and_non_json(monkeypatch):
    decider, _, _ = _decider(monkeypatch, json.dumps({"intent": "canvas"}))
    out = decider.decide("s", QUESTIONS)
    assert out["intent"].value == "canvas"
    assert out["urgency"].value is None and "no answer" in out["urgency"].error

    decider, _, _ = _decider(monkeypatch, "I think canvas")
    out = decider.decide("s", QUESTIONS)
    assert all(a.value is None and a.error for a in out.values())


def test_backend_error_becomes_decide_error(monkeypatch):
    class _Boom:
        def step(self, system, messages):
            raise LLMError("rate limited")

    decider = LLMDecider(CFG, backend=_Boom())
    with pytest.raises(DecideError, match="rate limited"):
        decider.decide("s", QUESTIONS)


def test_provider_exception_and_log_redact_api_key(monkeypatch, caplog):
    import logging

    secret = "sk-llm-SECRET-123"
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    class _Boom:
        def step(self, system, messages):
            raise LLMError(f"Authorization: Bearer {secret}")

    with caplog.at_level(logging.WARNING, logger="fabri.decide.llm"):
        with pytest.raises(DecideError) as exc:
            LLMDecider(CFG, backend=_Boom()).decide("s", QUESTIONS)
        text = json.dumps({"intent": "canvas", "urgency": "low", "risky": False})
        backend = _Recording([LLMResponse(
            final_text=text, usage=LLMUsage(input_tokens=1, output_tokens=1, model=secret),
        )])
        LLMDecider({**CFG, "model": secret}, backend=backend).decide("s", QUESTIONS)

    assert secret not in str(exc.value)
    assert all(secret not in record.getMessage() for record in caplog.records)


def test_backend_built_lazily(monkeypatch):
    calls = []
    monkeypatch.setattr("fabri.runtime._instantiate", lambda rcfg, tools: calls.append(1))
    LLMDecider(CFG)
    assert calls == []


def test_requires_model():
    with pytest.raises(ValueError):
        LLMDecider({"provider": "openai"})


def test_unpriced_model_warns_once_and_marks_cost_unknown(monkeypatch, caplog):
    import logging

    import fabri.decide.llm as llm_mod

    monkeypatch.setattr(llm_mod, "_warned_unpriced", set())
    text = json.dumps({"intent": "canvas", "urgency": "low", "risky": False})
    usage = LLMUsage(input_tokens=500, output_tokens=50, model="mystery-llm-1")
    backend = _Recording([LLMResponse(final_text=text, usage=usage), LLMResponse(final_text=text, usage=usage)])
    decider = LLMDecider(CFG, backend=backend)
    with caplog.at_level(logging.WARNING, logger="fabri.decide.llm"):
        first = decider.decide("s", QUESTIONS)
        second = decider.decide("s", QUESTIONS)
    for out in (first, second):
        for a in out.values():
            assert a.cost_usd == 0.0
            assert a.cost_known is False
            assert a.model == "mystery-llm-1"
    warnings = [r for r in caplog.records if r.name == "fabri.decide.llm"]
    assert len(warnings) == 1
    assert "mystery-llm-1" in warnings[0].getMessage()


def test_priced_model_cost_known(monkeypatch):
    usage = LLMUsage(input_tokens=1000, output_tokens=100, model="gpt-5-mini")
    decider, _, _ = _decider(monkeypatch, json.dumps({"intent": "canvas", "urgency": "low", "risky": True}), usage)
    assert all(a.cost_known for a in decider.decide("s", QUESTIONS).values())
