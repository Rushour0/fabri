"""Plain-writing rules for agent answers, and a checker that counts AI tells.

`PLAIN_WRITING_RULES` is a short block meant for `agent.system_prompt_prefix`
so every user-facing answer follows it. `slop_report(text)` is deterministic:
it counts the patterns readers most often flag as machine-written (dashes,
stock vocabulary, chatbot openers and closers, false contrasts, emoji
markers). It never rewrites text; use it to measure prompts and to gate evals.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

PLAIN_WRITING_RULES = """\
Write answers the way a busy colleague would.
- Start with the answer. No greeting, no restating the question.
- Short sentences, common words. Keep names, numbers and paths exact.
- Do not use em dashes or en dashes. Use a period, comma or parentheses.
- Do not write "not just X, it's Y" or "it's not about X, it's about Y". Say Y.
- Avoid these words: delve, leverage, robust, seamless, pivotal, crucial,
  vibrant, testament, underscore, landscape, tapestry, showcase, foster,
  harness, comprehensive, elevate, empower, unlock, journey.
- No emoji, no bold labels at the start of every bullet, no closing offer
  such as "let me know if you need anything else".
- If something failed or you are unsure, say so plainly.
"""

BANNED_WORDS = (
    "delve", "leverage", "robust", "seamless", "pivotal", "crucial", "vibrant",
    "testament", "underscore", "landscape", "tapestry", "showcase", "foster",
    "harness", "comprehensive", "game-changing", "cutting-edge", "elevate",
    "empower", "unlock", "journey", "additionally", "furthermore", "moreover",
)

_PATTERNS: dict[str, re.Pattern[str]] = {
    "dash": re.compile(r"[—–]| -- "),
    "banned_word": re.compile(
        r"\b(" + "|".join(re.escape(w) for w in BANNED_WORDS) + r")\w*\b", re.IGNORECASE
    ),
    "opener": re.compile(
        r"^\s*(great question|certainly|of course|absolutely|sure[!,]|i'd be happy to|"
        r"i would be happy to|happy to help)",
        re.IGNORECASE,
    ),
    "closer": re.compile(
        r"(let me know if|hope (this|that) helps|feel free to|happy to (help|clarify)|"
        r"don't hesitate to)",
        re.IGNORECASE,
    ),
    "false_contrast": re.compile(
        r"\b(not (just|only|merely) [^.]{1,60}?, (it's|it is|but)|"
        r"isn't (just )?about [^.]{1,60}?, it's about)",
        re.IGNORECASE,
    ),
    "filler": re.compile(
        r"\b(it's worth noting|it is worth noting|it's important to note|in today's|"
        r"at the end of the day|in order to)\b",
        re.IGNORECASE,
    ),
    "emoji": re.compile("[\U0001F300-\U0001FAFF☀-➿]"),
    "curly_quote": re.compile("[“”]"),
}


@dataclass
class SlopReport:
    """Counts per pattern plus up to three example snippets for each."""

    counts: dict[str, int] = field(default_factory=dict)
    examples: dict[str, list[str]] = field(default_factory=dict)
    words: int = 0

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def per_100_words(self) -> float:
        return 100.0 * self.total / self.words if self.words else 0.0


def slop_report(text: str) -> SlopReport:
    """Count AI-writing tells in `text`. The opener check looks only at the
    first line; every other pattern is counted across the whole text."""
    report = SlopReport(words=len(text.split()))
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    for name, pattern in _PATTERNS.items():
        haystack = first_line if name == "opener" else text
        hits = list(pattern.finditer(haystack))
        if not hits:
            continue
        report.counts[name] = len(hits)
        report.examples[name] = [
            haystack[max(0, m.start() - 20): m.end() + 20].replace("\n", " ") for m in hits[:3]
        ]
    return report
