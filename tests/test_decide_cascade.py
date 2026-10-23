"""CascadeDecider: escalate only low-confidence or errored questions, in one
batched fallback call, with the primary attempt counted in cost and latency."""

from __future__ import annotations

import pytest

from fabri.decide import Answer, CascadeDecider, DecideError, Question

QS = [
    Question("a", "choice", "A?", options={"x": None, "y": None}),
    Question("b", "score", "B?", levels=["lo", "hi"]),
    Question("c", "yesno", "C?"),
    Question("d", "choice", "D?", options={"x": None, "y": None}),
]


class _Fake:
    def __init__(self, answers: dict[str, Answer] | Exception, source: str = "jev"):
        self.answers = answers
        self.source = source
        self.calls: list[list[str]] = []

    def decide(self, state, questions):
        self.calls.append([q.key for q in questions])
        if isinstance(self.answers, Exception):
            raise self.answers
        return {q.key: self.answers[q.key] for q in questions if q.key in self.answers}


def _ans(key, kind, value, conf, source="jev", cost=0.001, lat=10.0, err=None):
    return Answer(key=key, kind=kind, value=value, confidence=conf, source=source,
                  cost_usd=cost, latency_ms=lat, input_tokens=100, output_tokens=0,
                  model=source, error=err)


def _fallback_answers():
    return {q.key: _ans(q.key, q.kind, {"choice": "y", "score": "hi", "yesno": False}[q.kind],
                        None, source="llm", cost=0.01, lat=500.0) for q in QS}


def test_escalates_only_low_confidence_and_errored():
    primary = _Fake({
        "a": _ans("a", "choice", "x", 0.9),           # confident -> keep
        "b": _ans("b", "score", "hi", 0.4),           # low -> escalate
        "c": _ans("c", "yesno", True, 0.2),           # low -> escalate
        "d": _ans("d", "choice", None, None, err="bad"),  # errored -> escalate
    })
    fallback = _Fake(_fallback_answers(), source="llm")
    out = CascadeDecider(primary, fallback).decide("s", QS)

    assert primary.calls == [["a", "b", "c", "d"]]
    assert fallback.calls == [["b", "c", "d"]]  # one batched call

    assert out["a"].escalated is False and out["a"].source == "jev" and out["a"].value == "x"
    for key in ("b", "c", "d"):
        a = out[key]
        assert a.escalated is True
        assert a.source == "llm"
        assert a.cost_usd == pytest.approx(0.011)  # fallback + primary attempt
        assert a.latency_ms == pytest.approx(510.0)
        assert a.input_tokens == 200
    assert out["c"].value is False


def test_nothing_escalated_means_no_fallback_call():
    primary = _Fake({q.key: _ans(q.key, q.kind, {"choice": "x", "score": "lo", "yesno": True}[q.kind], 0.95)
                     for q in QS})
    fallback = _Fake(_fallback_answers(), source="llm")
    out = CascadeDecider(primary, fallback).decide("s", QS)
    assert fallback.calls == []
    assert not any(a.escalated for a in out.values())


def test_per_kind_thresholds():
    primary = _Fake({
        "a": _ans("a", "choice", "x", 0.7),
        "b": _ans("b", "score", "hi", 0.7),
        "c": _ans("c", "yesno", True, 0.7),
        "d": _ans("d", "choice", "y", 0.7),
    })
    fallback = _Fake(_fallback_answers(), source="llm")
    CascadeDecider(primary, fallback, thresholds={"yesno": 0.8}).decide("s", QS)
    assert fallback.calls == [["c"]]


def test_missing_primary_answer_escalates():
    primary = _Fake({"a": _ans("a", "choice", "x", 0.9)})
    fallback = _Fake(_fallback_answers(), source="llm")
    CascadeDecider(primary, fallback).decide("s", QS[:2])
    assert fallback.calls == [["b"]]


def test_primary_failure_sends_everything_to_fallback():
    primary = _Fake(DecideError("jev: HTTP 503"))
    fallback = _Fake(_fallback_answers(), source="llm")
    out = CascadeDecider(primary, fallback).decide("s", QS)
    assert fallback.calls == [["a", "b", "c", "d"]]
    assert all(a.escalated and a.source == "llm" for a in out.values())
    assert all(a.cost_usd == pytest.approx(0.01) for a in out.values())


def test_fallback_failure_raises():
    primary = _Fake({q.key: _ans(q.key, q.kind, None, None, err="x") for q in QS})
    fallback = _Fake(DecideError("llm down"))
    with pytest.raises(DecideError, match="fallback failed: llm down"):
        CascadeDecider(primary, fallback).decide("s", QS)


def test_none_confidence_not_escalated():
    primary = _Fake({"a": _ans("a", "choice", "x", None, source="llm")})
    fallback = _Fake(_fallback_answers(), source="llm")
    out = CascadeDecider(primary, fallback).decide("s", QS[:1])
    assert fallback.calls == [] and out["a"].escalated is False


def test_bad_thresholds_rejected():
    with pytest.raises(ValueError):
        CascadeDecider(_Fake({}), _Fake({}), thresholds={"maybe": 0.5})
    with pytest.raises(ValueError):
        CascadeDecider(_Fake({}), _Fake({}), thresholds={"choice": 1.5})


def test_jev_answer_without_confidence_escalates():
    # Only an LLM answer may lack confidence. A Jev answer with a value but no
    # confidence (for example no probabilities came back) must be re-asked.
    primary = _Fake({"a": _ans("a", "choice", "x", None, source="jev")})
    fallback = _Fake(_fallback_answers(), source="llm")
    cascade = CascadeDecider(primary, fallback)
    assert cascade.needs_fallback(QS[0], primary.answers["a"]) is True
    out = cascade.decide("s", QS[:1])
    assert fallback.calls == [["a"]]
    assert out["a"].escalated is True and out["a"].value == "y"


def test_escalated_answer_cost_unknown_if_primary_cost_unknown():
    first = _ans("a", "choice", "x", 0.1)
    first.cost_known = False
    primary = _Fake({"a": first})
    fallback = _Fake(_fallback_answers(), source="llm")
    out = CascadeDecider(primary, fallback).decide("s", QS[:1])
    assert out["a"].escalated is True
    assert out["a"].cost_known is False
