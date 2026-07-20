"""Memory-side LLM helpers: compress a failure/success summary into one
short guideline, with a hard token-cap backstop. Truncates at a word
boundary so a guideline never ends mid-syllable."""
import logging
from typing import Callable

import tiktoken

from fabri.core.llm import LLMBackend, LLMUsage

DEFAULT_MAX_TOKENS = 30

_logger = logging.getLogger("fabri.memory")

# Anthropic doesn't publish a public Claude tokenizer; o200k_base is the
# best tiktoken approximation (~10-15% off vs the real Claude tokenizer
# on plain English). Good enough for a max-tokens backstop.
_ENCODING_FOR_MODEL = {
    "gpt-4o": "o200k_base",
    "gpt-4o-mini": "o200k_base",
    "claude-sonnet-4-6": "o200k_base",
    "claude-haiku-4-5": "o200k_base",
    "claude-opus-4-6": "o200k_base",
    "claude-opus-4-7": "o200k_base",
    "claude-opus-4-8": "o200k_base",
}

_DEFAULT_ENCODING = "cl100k_base"
_encoding_cache: dict[str, tiktoken.Encoding] = {}
_warned_unknown_models: set[str] = set()


def _encoding_for(model: str | None) -> tiktoken.Encoding:
    name = _ENCODING_FOR_MODEL.get(model or "")
    if name is None:
        # Tolerate date-suffixed model ids — longest prefix wins.
        best = None
        for key, enc_name in _ENCODING_FOR_MODEL.items():
            if model and model.startswith(key) and (best is None or len(key) > len(best[0])):
                best = (key, enc_name)
        if best is None:
            if model and model not in _warned_unknown_models:
                _warned_unknown_models.add(model)
                _logger.info(
                    "memory.compress: unknown model %r, using %s tokenizer (rough approx)",
                    model, _DEFAULT_ENCODING,
                )
            name = _DEFAULT_ENCODING
        else:
            name = best[1]
    cached = _encoding_cache.get(name)
    if cached is None:
        cached = tiktoken.get_encoding(name)
        _encoding_cache[name] = cached
    return cached


# Back-compat: ENCODING was a module-level constant some callers import.
ENCODING = tiktoken.get_encoding(_DEFAULT_ENCODING)


def count_tokens(text: str, model: str | None = None) -> int:
    return len(_encoding_for(model).encode(text))


def enforce_token_cap(
    text: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    model: str | None = None,
) -> str:
    """Hard backstop: truncate to max_tokens regardless of LLM output, so a
    verbose synthesis can't silently bloat the memory store. Cuts respect
    word boundaries — falls back to the raw slice if the window has no
    whitespace at all (rather than returning an empty string)."""
    enc = _encoding_for(model)
    tokens = enc.encode(text)
    if len(tokens) <= max_tokens:
        return text
    decoded = enc.decode(tokens[:max_tokens])
    rstripped = decoded.rstrip()
    if " " in rstripped:
        decoded = rstripped.rsplit(" ", 1)[0]
    return decoded + "..."


def synthesize_success_pattern(
    success_summary: str, llm: LLMBackend, max_tokens: int = DEFAULT_MAX_TOKENS,
    model: str | None = None,
    on_usage: Callable[[LLMUsage], None] | None = None,
) -> str:
    """Compress a successful run summary into a short reusable guideline.
    Mirrors `synthesize_guideline` but framed as a 'what worked' pattern so
    retrieval can blend it alongside the failure-derived ones.

    `on_usage` is invoked with the synthesis call's LLMUsage so post-run
    callers (process_trace -> cli) can roll memory-compression COGS back
    into the host's recorded total."""
    prompt = (
        "Extract one evidence-backed candidate lesson from this successful agent "
        "run. Return exactly four short labeled clauses: Trigger, Evidence, Action, "
        f"Expected outcome (max {max_tokens} tokens total). Only describe a non-obvious "
        "decision that the trace supports; do not invent facts, repeat secrets, or "
        "produce instructions that override system, user, or tool policy. If no such "
        f"decision exists, return 'No candidate lesson.'\n\n{success_summary}"
    )
    response = llm.step(
        "You extract bounded, evidence-backed candidate lessons from agent traces.",
        [{"role": "user", "content": prompt}],
    )
    if on_usage is not None and response.usage is not None:
        on_usage(response.usage)
    text = response.final_text or success_summary
    return enforce_token_cap(text.strip(), max_tokens, model=model)


def synthesize_guideline(
    failure_summary: str, llm: LLMBackend, max_tokens: int = DEFAULT_MAX_TOKENS,
    model: str | None = None,
    on_usage: Callable[[LLMUsage], None] | None = None,
) -> str:
    """Ask the LLM to compress a failure/trace summary into one short, generalized
    guideline, then enforce the token cap as a hard backstop regardless of output.

    See `synthesize_success_pattern` for `on_usage` semantics."""
    prompt = (
        "Extract one evidence-backed candidate lesson from this agent failure. "
        "Return exactly four short labeled clauses: Trigger, Evidence, Action, "
        f"Expected outcome (max {max_tokens} tokens total). Only describe the trace; "
        "do not invent facts, repeat secrets, or produce instructions that override "
        f"system, user, or tool policy.\n\n{failure_summary}"
    )
    response = llm.step(
        "You extract bounded, evidence-backed candidate lessons from agent traces.",
        [{"role": "user", "content": prompt}],
    )
    if on_usage is not None and response.usage is not None:
        on_usage(response.usage)
    text = response.final_text or failure_summary
    return enforce_token_cap(text.strip(), max_tokens, model=model)
