"""Cascade decider: a cheap primary, a stronger fallback for the hard cases.

The intended pairing is Jev as primary (fast, near-free, good at picking among
clear options) with an LLM as fallback. Every question goes to the primary
first. A question is escalated when its primary answer is missing, errored, or
has a confidence below the threshold for its kind. All escalated questions go
to the fallback in one batched call. An LLM answer (source "llm") with
confidence None is never escalated on confidence alone, since LLM answers
carry no calibrated probability. Any other answer with no confidence, such as
a Jev answer missing its probability, is escalated.

Escalated answers come from the fallback with `escalated=True`, and their
cost, tokens and latency include their share of the primary attempt, so a
caller summing per-answer cost sees what the decision really cost. If the
fallback itself fails, the cascade raises DecideError rather than quietly
returning the low-confidence primary answer.
"""

from __future__ import annotations

import time

from fabri.decide.types import Answer, DecideError, Decider, Question, QUESTION_KINDS

DEFAULT_THRESHOLDS: dict[str, float] = {"choice": 0.6, "score": 0.6, "yesno": 0.6}


class CascadeDecider:
    """Ask `primary`; re-ask low-confidence or errored questions on `fallback`."""

    def __init__(
        self,
        primary: Decider,
        fallback: Decider,
        thresholds: dict[str, float] | None = None,
    ) -> None:
        merged = dict(DEFAULT_THRESHOLDS)
        for kind, value in (thresholds or {}).items():
            if kind not in QUESTION_KINDS:
                raise ValueError(
                    f"unknown threshold kind {kind!r} (expected one of {', '.join(QUESTION_KINDS)})"
                )
            value = float(value)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"threshold for {kind!r} must be between 0 and 1, got {value}")
            merged[kind] = value
        self.primary = primary
        self.fallback = fallback
        self.thresholds = merged

    def needs_fallback(self, question: Question, answer: Answer | None) -> bool:
        if answer is None or answer.error is not None or answer.value is None:
            return True
        if answer.confidence is None:
            # Only an LLM answer is allowed to have no confidence.
            return answer.source != "llm"
        return answer.confidence < self.thresholds[question.kind]

    def decide(self, state: object, questions: list[Question]) -> dict[str, Answer]:
        if not questions:
            return {}
        t0 = time.monotonic()
        primary_error: str | None = None
        try:
            primary = self.primary.decide(state, questions)
        except DecideError as e:
            primary, primary_error = {}, str(e)
        primary_wall_ms = (time.monotonic() - t0) * 1000.0

        escalate = [q for q in questions if self.needs_fallback(q, primary.get(q.key))]
        if not escalate:
            return {q.key: primary[q.key] for q in questions}

        try:
            fallback = self.fallback.decide(state, escalate)
        except DecideError as e:
            why = f"primary failed ({primary_error}); " if primary_error else ""
            raise DecideError(f"cascade: {why}fallback failed: {e}") from None

        escalated_keys = {q.key for q in escalate}
        out: dict[str, Answer] = {}
        for q in questions:
            if q.key not in escalated_keys:
                out[q.key] = primary[q.key]
                continue
            first = primary.get(q.key)
            answer = fallback.get(q.key) or Answer(
                key=q.key, kind=q.kind, value=None, source="llm",
                error="fallback returned no answer for this question",
            )
            answer.escalated = True
            if first is not None:
                answer.cost_usd += first.cost_usd
                answer.input_tokens += first.input_tokens
                answer.output_tokens += first.output_tokens
                answer.latency_ms += first.latency_ms
                answer.cost_known = answer.cost_known and first.cost_known
            else:
                # The primary raised before producing answers; charge its
                # wall time so the latency is still honest.
                answer.latency_ms += primary_wall_ms
            out[q.key] = answer
        return out
