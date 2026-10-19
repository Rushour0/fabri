"""Decide whether a new memory item is USER-specific or an ORG lesson.

Deterministic rules run first. An optional cheap LLM call runs only when the
rules find nothing and the caller turned the flag on. A personal marker always
wins over an org marker, because the user scope is the narrower one: leaking a
personal habit to colleagues costs more than keeping a process lesson private.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from fabri.memory.scopes import SCOPE_ORG, SCOPE_USER, MemoryScope

# Kinds that are personal by definition.
_USER_KINDS = frozenset({"preference", "user_preference", "phrasing", "shortcut"})

_USER_PATTERNS = [
    r"\bI (?:prefer|like|love|want|need|always|never|usually|tend|hate|dislike)\b",
    r"\bI(?:'d| would) (?:rather|prefer)\b",
    r"\bmy (?:preferred|favou?rite|own|style|shortcut|shortcuts|habit|habits|tone|phrasing|"
    r"signature|nickname|name|wording|template)\b",
    r"\b(?:call|address|refer to|greet) me\b",
    r"\bthe user (?:prefers|likes|wants|always|never|asked to be|likes to)\b",
    r"\bthis user(?:'s)? (?:prefers|likes|wants)\b",
    r"\buser(?:'s)? (?:preferred|favou?rite|personal|own) \w+",
    r"\bpersonal (?:preference|shortcut|style|habit)\b",
    r"\bin (?:my|their own) (?:words|style|voice)\b",
]
_ORG_PATTERNS = [
    r"\b(?:our|the) (?:company|org|organi[sz]ation|team|department|policy|policies|process|"
    r"workflow|pipeline|runbook|standard|customers?|clients?)\b",
    r"\b(?:company|org|team|organi[sz]ation)[- ]wide\b",
    r"\bapproval (?:is )?required\b",
    r"\b(?:tool|api|endpoint|service|command)\b.*\b(?:fail|fails|failed|error|timeout|returns?)\b",
    r"\b(?:fail|fails|failed|error|timeout)\b.*\b(?:tool|api|endpoint|service|command)\b",
    r"\b(?:retry|re-read|verify|confirm|check)\b.*\b(?:before|after)\b",
]
_USER_RE = [re.compile(p, re.I) for p in _USER_PATTERNS]
# Org patterns are case-insensitive; the user patterns keep "I" case-insensitive too,
# so "i prefer" in lowercased text still matches.
_ORG_RE = [re.compile(p, re.I | re.S) for p in _ORG_PATTERNS]


@dataclass(frozen=True)
class ScopeDecision:
    scope: str          # "user" | "org"
    method: str         # "kind" | "identity" | "rule" | "llm" | "default"
    reason: str


def _mentions_person(text: str, scope: MemoryScope | None) -> bool:
    """True when the text names the person (user id or a personal alias)."""
    if scope is None:
        return False
    lowered = text.lower()
    return bool(scope.user_id.strip()) and scope.user_id.strip().lower() in lowered


def classify_scope(
    text: str,
    *,
    kind: str | None = None,
    scope: MemoryScope | None = None,
    llm=None,
    use_llm: bool = False,
    default: str = SCOPE_ORG,
) -> ScopeDecision:
    if kind in _USER_KINDS:
        return ScopeDecision(SCOPE_USER, "kind", f"kind {kind!r} is personal")
    if _mentions_person(text, scope):
        return ScopeDecision(SCOPE_USER, "identity", "text names the person")
    for pat in _USER_RE:
        if pat.search(text):
            return ScopeDecision(SCOPE_USER, "rule", f"personal marker: {pat.pattern[:40]}")
    for pat in _ORG_RE:
        if pat.search(text):
            return ScopeDecision(SCOPE_ORG, "rule", f"org marker: {pat.pattern[:40]}")
    if use_llm and llm is not None:
        decided = _llm_classify(text, llm)
        if decided is not None:
            return ScopeDecision(decided, "llm", "llm classifier")
    return ScopeDecision(default, "default", "no rule matched")


_LLM_SYSTEM = (
    "You label one memory note. Answer with exactly one word. "
    "USER if the note is a personal preference, phrasing habit or shortcut of one person. "
    "ORG if it is a fact or process lesson that anyone in the same organization could reuse."
)


def _llm_classify(text: str, llm) -> str | None:
    try:
        response = llm.step(_LLM_SYSTEM, [{"role": "user", "content": text[:1000]}])
        answer = (response.final_text or "").strip().upper()
    except Exception:  # noqa: BLE001 -- classifier is best effort; fall back to the default
        return None
    if answer.startswith("USER"):
        return SCOPE_USER
    if answer.startswith("ORG"):
        return SCOPE_ORG
    return None
