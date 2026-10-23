"""Deterministic de-identification for lessons headed to the GLOBAL scope.

``scrub`` removes emails, phones, URLs, IPs, ids, secrets, home paths, handles,
known names (org, person, project), amounts, dates, entity-tied numbers and
unfamiliar capitalized names. ``leak_check`` verifies the result. An optional
LLM rewrite (``rewrite_generic``) only ever sees text that was already scrubbed,
and its output is scrubbed and checked again.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_URL = re.compile(r"\b(?:https?://|www\.)\S+", re.I)
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_LONG_HEX = re.compile(r"\b[0-9a-f]{16,}\b", re.I)
_SECRET = re.compile(r"\b(?:sk|pk|ghp|gho|xox[abpr]|AKIA)[-_A-Za-z0-9]{12,}\b")
_TOKEN = re.compile(r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{28,}\b")
_PHONE = re.compile(
    r"(?<![\w.])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?|\d{2,4}[\s.-])\d{3,4}[\s.-]\d{3,4}(?![\w])"
    r"|(?<![\w.])\+\d{8,15}(?![\w])"
)
_HOME = re.compile(r"(?:/Users/|/home/|[A-Za-z]:\\Users\\)\S*?(?=[.,;:)]*(?:\s|$))")
_ABS_PATH = re.compile(r"(?<![\w<])/(?:[\w.@-]+/)+[\w.@-]+")
_HANDLE = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{2,}")
_AMOUNT = re.compile(r"[$€£₹]\s?\d[\d,]*(?:\.\d+)?(?:\s?[kKmMbB]\b)?|\b\d[\d,]*(?:\.\d+)?\s?(?:USD|EUR|GBP|INR|dollars|euros)\b", re.I)
_DATE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b|"
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.? \d{1,2}(?:st|nd|rd|th)?(?:,? \d{4})?\b",
    re.I,
)
_CODE_ID = re.compile(r"\b[A-Za-z]{2,}[-_#]\d+\b|#\d+\b")
_LONG_NUMBER = re.compile(r"\b\d{3,}(?:[.,]\d+)*\b")
_CAP_WORD = r"[A-Z][a-zA-Z'&-]+"
# A small integer glued to a removed name is a count of that entity.
_NUM_ENTITY_BEFORE = re.compile(r"\b\d+\s+(?=<name>)")
_NUM_ENTITY_AFTER = re.compile(r"(<name>\s)\d+\b")
_PLACEHOLDER = re.compile(r"<[a-z_]+>")

# Words that are capitalized but carry no entity: tools, formats, days, months.
_COMMON_CAPS = frozenset(
    """I A An The This That These Those It In On At For And Or But If When After Before
    Python JSON YAML XML HTML CSS SQL HTTP HTTPS API URL CLI SDK LLM AI CSV PDF UI UX
    Git GitHub Linux Windows macOS Docker Kubernetes Qdrant SQLite Postgres Redis
    Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February March April
    May June July August September October November December Note Task Plan Outcome Tool
    Always Never Do Don't Use Avoid Check Verify Confirm Retry Re-read Ensure""".split()
)


def _ci_word(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", re.I)


def _name_patterns(names: Iterable[str]) -> list[re.Pattern[str]]:
    """Full names first (longest first), then single tokens of 4+ letters."""
    full: set[str] = set()
    parts: set[str] = set()
    for raw in names:
        name = (raw or "").strip()
        if len(name) < 2:
            continue
        full.add(name)
        for token in re.split(r"[\s@._-]+", name):
            if len(token) >= 4 and token.isalpha():
                parts.add(token)
    ordered = sorted(full, key=len, reverse=True) + sorted(parts - full, key=len, reverse=True)
    return [_ci_word(n) for n in ordered]


def scrub(text: str, names: Iterable[str] = ()) -> str:
    """Return ``text`` with identifying details replaced by ``<placeholder>``."""
    out = text or ""
    # Order matters: structured tokens before the number and name passes.
    out = _EMAIL.sub("<email>", out)
    out = _URL.sub("<url>", out)
    out = _UUID.sub("<id>", out)
    out = _SECRET.sub("<secret>", out)
    out = _LONG_HEX.sub("<id>", out)
    out = _TOKEN.sub("<secret>", out)
    out = _IPV4.sub("<ip>", out)
    out = _HOME.sub("<path>", out)
    out = _ABS_PATH.sub("<path>", out)
    out = _AMOUNT.sub("<amount>", out)
    out = _DATE.sub("<date>", out)
    out = _PHONE.sub("<phone>", out)
    out = _HANDLE.sub("<handle>", out)
    for pattern in _name_patterns(names):
        out = pattern.sub("<name>", out)
    out = _CODE_ID.sub("<id>", out)
    out = _LONG_NUMBER.sub("<num>", out)
    out = _scrub_proper_nouns(out)
    out = _NUM_ENTITY_BEFORE.sub("<num> ", out)
    out = _NUM_ENTITY_AFTER.sub(r"\1<num>", out)
    out = re.sub(r"(<[a-z_]+>)(?:[\s,/-]+\1)+", r"\1", out)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


_WORD = re.compile(rf"(?P<pre>(?:^|[.!?:]\s+|\n)\s*)?(?P<w>{_CAP_WORD})")


def _scrub_proper_nouns(text: str) -> str:
    """Replace capitalized words that are not at a sentence start and not on the
    common list. Sentence-start words are kept unless followed by 's or by
    another capitalized word (a likely name)."""

    def repl(match: re.Match[str]) -> str:
        word = match.group("w")
        if word in _COMMON_CAPS or word.isupper():
            return match.group(0)
        pre = match.group("pre") or ""
        if pre:
            rest = text[match.end(): match.end() + 3]
            if rest.startswith("'s") or re.match(r"\s[A-Z][a-z]", rest):
                return f"{pre}<name>"
            return match.group(0)
        return "<name>"

    return _WORD.sub(repl, text)


def leak_check(text: str, names: Iterable[str] = ()) -> list[str]:
    """Return what is still identifying in ``text``. Empty means clean."""
    problems: list[str] = []
    if _EMAIL.search(text):
        problems.append("email")
    if _URL.search(text):
        problems.append("url")
    if _PHONE.search(text):
        problems.append("phone")
    if _IPV4.search(text):
        problems.append("ip")
    for pattern in _name_patterns(names):
        if pattern.search(text):
            problems.append(f"name:{pattern.pattern[:30]}")
    return problems


def placeholder_ratio(text: str) -> float:
    words = text.split()
    if not words:
        return 1.0
    return len(_PLACEHOLDER.findall(text)) / len(words)


_REWRITE_SYSTEM = (
    "Rewrite the note as one short, generic, reusable rule. Remove anything specific to a "
    "person, company, product, project or customer. Keep the technical lesson. "
    "Output only the rule."
)


def rewrite_generic(text: str, llm, on_usage=None) -> str | None:
    """Optional LLM pass. Input must already be scrubbed. Returns None on failure."""
    try:
        response = llm.step(_REWRITE_SYSTEM, [{"role": "user", "content": text}])
        if on_usage is not None and getattr(response, "usage", None) is not None:
            on_usage(response.usage)
        out = (response.final_text or "").strip()
    except Exception:  # noqa: BLE001
        return None
    return out or None
