"""Typed questions and answers shared by every decider.

A decider takes a `state` (free text, or any JSON-serializable value) plus a
list of `Question`s and returns one `Answer` per question key. Three question
kinds exist:

- ``choice``: pick one of ``options`` (a map of option name -> description or
  None). 1 to 255 options.
- ``score``: pick one of ``levels``, an ordered list of 2 to 10 level names.
- ``yesno``: a true/false decision. ``options`` may optionally describe the two
  sides as ``{"true": "...", "false": "..."}``.

Option names and level names must be strings. In YAML, quote any option name
that YAML would read as a boolean (``yes``, ``no``, ``on``, ``off``, ``true``,
``false``); only yesno questions rename boolean keys to "true"/"false".

Confidence is on one 0..1 scale for every kind. For a question with n allowed
answers whose most likely answer has probability p_max, Jev reports
``confidence = (p_max - 1/n) / (1 - 1/n)``: 0 means no better than a uniform
guess, 1 means certain. A yesno answer has n = 2 and p_max = max(P, 1 - P), so
the same formula gives ``confidence = |2P - 1|``. A yesno threshold of 0.8
therefore means P >= 0.9 for the chosen side.

Accounting on an `Answer`: one decide() call usually answers several questions
with one provider request. `cost_usd`, `input_tokens` and `output_tokens` hold
this answer's even share of that request, so summing them over the answers of
one call gives the call total. `latency_ms` is the wall time of the request(s)
that produced the answer and is NOT a share: answers from the same call report
the same latency.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Iterable, Literal, Protocol

QuestionKind = Literal["choice", "score", "yesno"]
AnswerSource = Literal["jev", "llm"]

QUESTION_KINDS: tuple[str, ...] = ("choice", "score", "yesno")

_MAX_OPTIONS = 255
_MIN_LEVELS, _MAX_LEVELS = 2, 10
_YESNO_SIDES = ("true", "false")
_BEARER_TOKEN = re.compile(r"\bBearer\s+[^\s,;]+", re.IGNORECASE)
_SK_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]+")


class DecideError(RuntimeError):
    """A decider could not produce answers: HTTP, auth, timeout or parse
    failure. Messages never carry the API key."""


def sanitize(text: object, secrets: Iterable[str] | str = ()) -> str:
    """Redact configured secrets and common API-token forms from error text.

    Provider payloads and SDK exceptions are untrusted: this helper is the
    single final formatter for text that can reach a decision error, answer
    error, or log message.
    """
    result = str(text)
    values = (secrets,) if isinstance(secrets, str) else secrets
    for secret in values:
        if secret:
            result = result.replace(secret, "***")
    result = _BEARER_TOKEN.sub("Bearer ***", result)
    return _SK_TOKEN.sub("***", result)


@dataclass(frozen=True)
class Question:
    """One typed question. Validated on construction so a bad question fails
    before any provider call is made."""

    key: str
    kind: QuestionKind
    instructions: str
    options: dict[str, str | None] | None = None
    levels: list[str] | None = None

    def __post_init__(self) -> None:
        if not self.key or not isinstance(self.key, str):
            raise ValueError("question key must be a non-empty string")
        if self.kind not in QUESTION_KINDS:
            raise ValueError(
                f"question {self.key!r}: unknown kind {self.kind!r} "
                f"(expected one of {', '.join(QUESTION_KINDS)})"
            )
        if not isinstance(self.instructions, str) or not self.instructions.strip():
            raise ValueError(f"question {self.key!r}: instructions must be a non-empty string")
        if self.kind == "choice":
            if not isinstance(self.options, dict) or not self.options:
                raise ValueError(f"question {self.key!r}: a choice question needs options")
            for name in self.options:
                if isinstance(name, bool):
                    raise ValueError(
                        f"question {self.key!r}: option name {name!r} is a boolean, not a "
                        "string; YAML reads bare yes/no/on/off/true/false as booleans, so "
                        "quote the option name (for example 'yes': ...)"
                    )
                if not isinstance(name, str) or not name:
                    raise ValueError(
                        f"question {self.key!r}: option names must be non-empty strings, got {name!r}"
                    )
            if len(self.options) > _MAX_OPTIONS:
                raise ValueError(
                    f"question {self.key!r}: at most {_MAX_OPTIONS} options, got {len(self.options)}"
                )
            if self.levels is not None:
                raise ValueError(f"question {self.key!r}: levels apply only to score questions")
        elif self.kind == "score":
            if not isinstance(self.levels, list) or not (
                _MIN_LEVELS <= len(self.levels) <= _MAX_LEVELS
            ):
                raise ValueError(
                    f"question {self.key!r}: a score question needs "
                    f"{_MIN_LEVELS} to {_MAX_LEVELS} levels"
                )
            for level in self.levels:
                if isinstance(level, bool):
                    raise ValueError(
                        f"question {self.key!r}: score level {level!r} is a boolean, not a "
                        "string; YAML reads bare yes/no/on/off/true/false as booleans, so "
                        "quote the level name"
                    )
                if not isinstance(level, str) or not level:
                    raise ValueError(
                        f"question {self.key!r}: score levels must be non-empty strings, got {level!r}"
                    )
            if len(set(self.levels)) != len(self.levels):
                raise ValueError(f"question {self.key!r}: score levels must be unique")
            if self.options is not None:
                raise ValueError(f"question {self.key!r}: options apply only to choice and yesno questions")
        else:  # yesno
            if self.options is not None:
                if not isinstance(self.options, dict) or set(self.options) - set(_YESNO_SIDES):
                    raise ValueError(
                        f"question {self.key!r}: yesno options may only describe 'true' and 'false'"
                    )
            if self.levels is not None:
                raise ValueError(f"question {self.key!r}: levels apply only to score questions")

    def allowed_values(self) -> list[str] | list[bool]:
        """The answer values this question accepts."""
        if self.kind == "choice":
            return list(self.options or {})
        if self.kind == "score":
            return list(self.levels or [])
        return [True, False]

    @classmethod
    def from_dict(cls, key: str, data: dict) -> Question:
        """Build from a config mapping such as a decision-tree node's
        ``question`` block. YAML parses bare ``true``/``false`` keys as
        booleans, so yesno option keys are normalized to strings. Choice
        option keys are passed through unchanged, so a boolean key fails
        validation with a hint to quote it."""
        if not isinstance(data, dict):
            raise ValueError(f"question {key!r}: expected a mapping")
        options = data.get("options")
        if isinstance(options, dict) and data.get("kind") == "yesno":
            options = {_side_key(k): v for k, v in options.items()}
        levels = data.get("levels")
        return cls(
            key=key,
            kind=data.get("kind"),  # type: ignore[arg-type]
            instructions=data.get("instructions", ""),
            options=options,
            levels=list(levels) if isinstance(levels, (list, tuple)) else levels,
        )


def _side_key(k: object) -> str:
    """Normalize a YAML key to a string; booleans become 'true'/'false'."""
    if isinstance(k, bool):
        return "true" if k else "false"
    return str(k)


@dataclass
class Answer:
    """One question's answer plus its provenance and accounting.

    `value` is the chosen option (choice), level (score) or a bool (yesno). It
    is None when the decider could not produce a valid answer, in which case
    `error` says why. `confidence` is None for LLM answers, which carry no
    calibrated probability. `cost_known` is False when the model could not
    be priced; `cost_usd` is then 0.0 but means "unknown", not "free"."""

    key: str
    kind: QuestionKind
    value: str | bool | None
    probabilities: dict = field(default_factory=dict)
    confidence: float | None = None
    source: AnswerSource = "jev"
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""
    escalated: bool = False
    error: str | None = None
    cost_known: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


class Decider(Protocol):
    """Anything that answers typed questions about a state."""

    def decide(self, state: object, questions: list[Question]) -> dict[str, Answer]: ...


def split_evenly(total: int, n: int) -> list[int]:
    """Split an integer token count into `n` shares that sum to `total`; the
    remainder goes to the first shares."""
    if n <= 0:
        return []
    base, rem = divmod(total, n)
    return [base + (1 if i < rem else 0) for i in range(n)]
