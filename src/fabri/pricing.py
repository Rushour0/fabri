"""Model pricing -> USD cost for an LLMUsage.

Rates are USD per 1M tokens, from published Anthropic / OpenAI list pricing.
They are an APPROXIMATION the host reconciles against a real provider invoice:
sum the per-run `cost_usd` over a window, compare to the console invoice for the
same window, and tune the constants below until they agree.

Anthropic prompt-cache economics: a cache WRITE bills at 1.25x the input rate
(5-minute ephemeral), a cache READ at 0.10x. Those multipliers live here rather
than in the table so a new model only needs its (input, output) pair. The
provider's `input_tokens` already EXCLUDES cached tokens -- they arrive in the
`cache_creation_*` / `cache_read_*` buckets -- so the four buckets sum without
double-counting.
"""

from __future__ import annotations

from fabri.core.llm import LLMUsage
from fabri.core.logging_setup import get_logger

logger = get_logger()

_PER_MTOK = 1_000_000.0

# 5-minute ephemeral cache multipliers, relative to the input rate.
_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT = 0.10

# model id -> (input $/MTok, output $/MTok). Keyed by the bare model id the
# backend reports on LLMUsage.model. Lookup tolerates date-suffixed ids
# (e.g. "claude-haiku-4-5-20251001") via prefix match, so only the base id
# needs an entry. Extend as new models are adopted.
PRICING: dict[str, tuple[float, float]] = {
    # Anthropic -- the two models ludexel's fabri config uses today.
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    # Anthropic -- Opus tier, here so an Opus-configured agent prices too.
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    # OpenAI -- the OpenAILLMBackend default. Cache multipliers are an
    # Anthropic convention; for OpenAI they're approximate, reconciled to
    # invoice like everything else.
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.60),
    # OpenRouter -- model ids are namespaced (<vendor>/<model>). OpenRouter
    # passes through the underlying provider's rate with a small markup
    # (~5% on average); these entries match the underlying provider's list
    # price and are reconciled to the OpenRouter invoice like the others.
    # Extend as new models are adopted.
    "anthropic/claude-haiku-4-5":  (1.0, 5.0),
    "anthropic/claude-sonnet-4-6": (3.0, 15.0),
    "anthropic/claude-opus-4-8":   (5.0, 25.0),
    "openai/gpt-4o":               (2.5, 10.0),
    "openai/gpt-4o-mini":          (0.15, 0.60),
    # Google Gemini -- native google-genai backend. gemini-2.5-pro is the
    # Sonnet-class model for the main role. Base-tier list rates; 2.5-pro's
    # higher >200k-input-token tier is not modeled (reconciled to invoice like
    # the rest). Gemini implicit-cache reads land in cache_read_input_tokens and
    # price at _CACHE_READ_MULT (0.10x) -- an approximation for Gemini's caching.
    "gemini-2.5-pro":        (1.25, 10.0),
    "gemini-2.5-flash":      (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.0-flash":      (0.10, 0.40),
    # AWS Bedrock -- ids are vendor-namespaced (`anthropic.…`, `openai.…`,
    # `meta.…`). `_rates_for` prefix-matches from position 0, so a cross-region
    # inference profile (`us.` / `eu.` / `apac.` prefix) needs its OWN key --
    # it won't match the bare id. Rates mirror the underlying model's list price
    # and are reconciled to the Bedrock invoice like every other entry. Extend
    # as new Bedrock models are adopted.
    "anthropic.claude-3-5-sonnet":    (3.0, 15.0),
    "us.anthropic.claude-3-5-sonnet": (3.0, 15.0),
    "anthropic.claude-3-5-haiku":     (0.80, 4.0),
    "us.anthropic.claude-3-5-haiku":  (0.80, 4.0),
    "openai.gpt-oss-120b":            (0.15, 0.60),
    "openai.gpt-oss-20b":             (0.07, 0.30),
    # Moonshot AI Kimi on Bedrock -- region-agnostic foundation-model ids (no
    # us./apac. prefix). NB the two ids use DIFFERENT vendor prefixes
    # (`moonshot.` vs `moonshotai.`); match the console id exactly. Reasoning
    # tokens (k2-thinking) bill as output. Rates per published Bedrock pricing,
    # reconciled to invoice like the rest; ap-south-1 may differ slightly.
    "moonshot.kimi-k2-thinking":      (0.60, 2.50),
    "moonshotai.kimi-k2.5":           (0.60, 3.00),
}


def _rates_for(model: str | None) -> tuple[float, float] | None:
    if not model:
        return None
    if model in PRICING:
        return PRICING[model]
    # Tolerate date-suffixed / variant ids: longest matching base id wins so
    # "claude-haiku-4-5-20251001" resolves to "claude-haiku-4-5".
    best: tuple[int, tuple[float, float]] | None = None
    for key, rates in PRICING.items():
        if model.startswith(key) and (best is None or len(key) > best[0]):
            best = (len(key), rates)
    return best[1] if best else None


def cost_for(usage: LLMUsage) -> float | None:
    """USD cost of one LLMUsage, priced by its `model`.

    Returns None (and logs a warning) for an unknown/absent model so an
    unpriced run records a null cost rather than a misleading 0 -- the caller
    decides how to surface "we don't know the cost" vs "the cost was zero".
    """
    rates = _rates_for(usage.model)
    if rates is None:
        logger.warning(
            "no pricing entry for model=%r; cost recorded as None (add it to fabri.pricing.PRICING)",
            usage.model,
        )
        return None
    in_rate, out_rate = rates
    cost = (
        usage.input_tokens * in_rate
        + usage.output_tokens * out_rate
        + usage.cache_creation_input_tokens * in_rate * _CACHE_WRITE_MULT
        + usage.cache_read_input_tokens * in_rate * _CACHE_READ_MULT
    ) / _PER_MTOK
    return round(cost, 6)
