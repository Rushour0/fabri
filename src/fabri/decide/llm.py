"""LLM decider: answer typed questions with a chat model.

Used as the fallback when Jev is unsure, or on its own. Takes a fabri `llm:`
block (provider, model, api_key_env, max_tokens, base_url), builds the backend
through `fabri.runtime._instantiate`, and sends one system + user message
asking for a JSON object ``{question_key: answer}``. Any answer outside the
question's options, levels or true/false is rejected: that Answer gets
value None and an `error`, never a guessed value. LLM answers carry no
calibrated probability, so `confidence` is None.
"""

from __future__ import annotations

import json
import logging
import os
import time

from fabri.core.llm import LLMError, LLMUsage
from fabri.core.structured import parse_response
from fabri.decide.types import Answer, DecideError, Question, sanitize, split_evenly

logger = logging.getLogger(__name__)
_warned_unpriced: set[str] = set()

_SYSTEM = (
    "You are a decision function. Read the state and answer every question. "
    "Reply with only a JSON object that maps each question key to its answer. "
    "For a choice question the answer is exactly one option name. For a score "
    "question it is exactly one level name. For a yesno question it is the JSON "
    "boolean true or false. Use no other values and add no commentary."
)


class LLMDecider:
    """Answer questions with one chat-model call per decide()."""

    def __init__(self, llm_cfg: dict, backend: object | None = None) -> None:
        if not isinstance(llm_cfg, dict) or not llm_cfg.get("model"):
            raise ValueError("LLMDecider needs an llm block with at least a model")
        self.llm_cfg = dict(llm_cfg)
        if not self.llm_cfg.get("api_key_env"):
            # Resolve the provider's default key env once and write it into the
            # config, so the backend that sends the key and _secrets() that
            # redacts it always read the same variable. runtime._instantiate on
            # its own would fall back to GEMINI_API_KEY for every provider.
            from fabri.config import _PROVIDER_DEFAULT_API_KEY_ENV
            from fabri.core.llm import Provider

            raw_provider = self.llm_cfg.get("provider")
            provider = Provider.coerce(raw_provider) if raw_provider else Provider.GEMINI
            default_env = _PROVIDER_DEFAULT_API_KEY_ENV.get(provider)
            if default_env:
                self.llm_cfg["api_key_env"] = default_env
        # Built lazily so loading a tree does not require the provider key.
        self._backend = backend

    @property
    def backend(self):
        if self._backend is None:
            from fabri import runtime

            self._backend = runtime._instantiate(self.llm_cfg, [])
        return self._backend

    def build_messages(self, state: object, questions: list[Question]) -> tuple[str, list[dict]]:
        """The system prompt and single user message for one call."""
        state_text = state if isinstance(state, str) else json.dumps(state, indent=2, default=str)
        lines = ["State:", state_text, "", "Questions:"]
        for q in questions:
            lines.append(f"- key: {q.key}")
            lines.append(f"  kind: {q.kind}")
            lines.append(f"  instructions: {q.instructions}")
            if q.kind == "choice":
                lines.append("  options:")
                for name, desc in (q.options or {}).items():
                    lines.append(f"    - {name}" + (f": {desc}" if desc else ""))
            elif q.kind == "score":
                lines.append("  levels (lowest to highest): " + ", ".join(q.levels or []))
            else:
                lines.append("  answer: true or false")
                for side, desc in (q.options or {}).items():
                    if desc:
                        lines.append(f"    {side} means: {desc}")
        keys = ", ".join(q.key for q in questions)
        lines += ["", f"Return a JSON object with exactly these keys: {keys}"]
        return _SYSTEM, [{"role": "user", "content": "\n".join(lines)}]

    def decide(self, state: object, questions: list[Question]) -> dict[str, Answer]:
        if not questions:
            return {}
        system, messages = self.build_messages(state, questions)
        secrets = self._secrets()
        t0 = time.monotonic()
        try:
            resp = self.backend.step(system, messages)
        except LLMError as e:
            raise DecideError(f"llm decider: {sanitize(e, secrets)}") from None
        except Exception as e:  # provider SDK errors (auth, bad request) are not LLMError
            detail = sanitize(f"{type(e).__name__}: {e}", secrets)
            raise DecideError(f"llm decider: {detail}") from None
        latency_ms = (time.monotonic() - t0) * 1000.0

        usage = resp.usage or LLMUsage()
        model = sanitize(usage.model or str(self.llm_cfg["model"]), secrets)
        if usage.model != model:
            usage = LLMUsage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_creation_input_tokens=usage.cache_creation_input_tokens,
                cache_read_input_tokens=usage.cache_read_input_tokens,
                model=model,
            )
        from fabri.pricing import cost_for

        priced = cost_for(usage)
        cost_known = priced is not None
        total_cost = priced if priced is not None else 0.0
        if not cost_known and model not in _warned_unpriced:
            _warned_unpriced.add(model)
            logger.warning(
                "llm decider: cannot price model %r; its cost is unknown and recorded as 0.0",
                sanitize(model, secrets),
            )
        n = len(questions)
        in_shares = split_evenly(usage.input_tokens, n)
        out_shares = split_evenly(usage.output_tokens, n)

        value, _schema_errors = parse_response(resp.final_text or "", _schema(questions))
        out: dict[str, Answer] = {}
        for i, q in enumerate(questions):
            answer = Answer(key=q.key, kind=q.kind, value=None, source="llm")
            if not isinstance(value, dict):
                answer.error = "llm response is not a JSON object"
            elif q.key not in value:
                answer.error = "llm response has no answer for this question"
            else:
                answer.value, answer.error = _coerce(q, value[q.key], secrets)
            answer.latency_ms = latency_ms
            answer.input_tokens = in_shares[i]
            answer.output_tokens = out_shares[i]
            answer.cost_usd = total_cost / n
            answer.cost_known = cost_known
            answer.model = model
            out[q.key] = answer
        return out

    def _secrets(self) -> tuple[str, ...]:
        api_key_env = self.llm_cfg.get("api_key_env")
        key = os.environ.get(api_key_env) if isinstance(api_key_env, str) else None
        return (key,) if key else ()


def _schema(questions: list[Question]) -> dict:
    props: dict[str, dict] = {}
    for q in questions:
        if q.kind == "yesno":
            props[q.key] = {"type": "boolean"}
        else:
            props[q.key] = {"type": "string", "enum": q.allowed_values()}
    return {"type": "object", "required": [q.key for q in questions], "properties": props}


def _coerce(
    q: Question, raw: object, secrets: tuple[str, ...] = ()
) -> tuple[str | bool | None, str | None]:
    """Accept only a value in the question's allowed set. A yesno answer may
    come back as the strings "true"/"false"; anything else is rejected."""
    if q.kind == "yesno":
        if isinstance(raw, bool):
            return raw, None
        if isinstance(raw, str) and raw.strip().lower() in ("true", "false"):
            return raw.strip().lower() == "true", None
        return None, sanitize(f"llm answered {raw!r}, expected true or false", secrets)
    allowed = q.allowed_values()
    if isinstance(raw, str) and raw in allowed:
        return raw, None
    return None, sanitize(f"llm answered {raw!r}, which is not one of {allowed!r}", secrets)
