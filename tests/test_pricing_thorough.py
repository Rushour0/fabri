"""Thorough unit coverage for fabri.pricing.cost_for.

cost_for is pure (no Qdrant / no API key), so these are fast deterministic
unit tests. Every expected USD figure below is computed by hand against the
PRICING table and the cache multipliers so the test pins the arithmetic, not
just "whatever the code currently returns".

Rates (USD / 1M tokens), from fabri.pricing.PRICING:
    claude-sonnet-4-6 -> input 3,  output 15
    claude-haiku-4-5  -> input 1,  output 5
    claude-opus-4-8   -> input 5,  output 25
    claude-opus-4-7   -> input 5,  output 25
    claude-opus-4-6   -> input 5,  output 25
    gpt-4o            -> input 2.5, output 10
Cache: write = 1.25 x input rate, read = 0.10 x input rate.
"""
import pytest

from fabri.core.llm import LLMUsage
from fabri.pricing import (
    PRICING,
    _CACHE_READ_MULT,
    _CACHE_WRITE_MULT,
    _PER_MTOK,
    cost_for,
)

M = 1_000_000


def _expected(model, inp=0, out=0, cc=0, cr=0):
    """Re-derive the expected USD straight from the published rate pair."""
    in_rate, out_rate = PRICING[model]
    return round(
        (
            inp * in_rate
            + out * out_rate
            + cc * in_rate * _CACHE_WRITE_MULT
            + cr * in_rate * _CACHE_READ_MULT
        )
        / _PER_MTOK,
        6,
    )


# ---- per-model: input only / output only / combined ------------------------

@pytest.mark.parametrize("model", list(PRICING.keys()))
def test_input_only_priced_at_input_rate(model):
    in_rate, _ = PRICING[model]
    u = LLMUsage(input_tokens=M, model=model)
    assert cost_for(u) == in_rate  # 1M @ in_rate/MTok == in_rate dollars


@pytest.mark.parametrize("model", list(PRICING.keys()))
def test_output_only_priced_at_output_rate(model):
    _, out_rate = PRICING[model]
    u = LLMUsage(output_tokens=M, model=model)
    assert cost_for(u) == out_rate


@pytest.mark.parametrize("model", list(PRICING.keys()))
def test_input_plus_output_combined(model):
    in_rate, out_rate = PRICING[model]
    u = LLMUsage(input_tokens=M, output_tokens=M, model=model)
    assert cost_for(u) == round(in_rate + out_rate, 6)


# ---- specific tier sanity checks (named values from the spec) --------------

def test_sonnet_rates_3_15():
    assert cost_for(LLMUsage(input_tokens=M, output_tokens=M, model="claude-sonnet-4-6")) == 18.0


def test_haiku_rates_1_5():
    assert cost_for(LLMUsage(input_tokens=M, output_tokens=M, model="claude-haiku-4-5")) == 6.0


def test_opus_rates_5_25_all_variants():
    for m in ("claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6"):
        assert cost_for(LLMUsage(input_tokens=M, model=m)) == 5.0
        assert cost_for(LLMUsage(output_tokens=M, model=m)) == 25.0
        assert cost_for(LLMUsage(input_tokens=M, output_tokens=M, model=m)) == 30.0


def test_gpt4o_rates_2_5_10():
    assert cost_for(LLMUsage(input_tokens=M, model="gpt-4o")) == 2.5
    assert cost_for(LLMUsage(output_tokens=M, model="gpt-4o")) == 10.0
    assert cost_for(LLMUsage(input_tokens=M, output_tokens=M, model="gpt-4o")) == 12.5


def test_gemini_25_pro_rates_1_25_10():
    assert cost_for(LLMUsage(input_tokens=M, model="gemini-2.5-pro")) == 1.25
    assert cost_for(LLMUsage(output_tokens=M, model="gemini-2.5-pro")) == 10.0


def test_gemini_flash_variants_priced():
    assert abs(cost_for(LLMUsage(input_tokens=M, output_tokens=M, model="gemini-2.5-flash")) - 2.80) < 1e-9
    assert abs(cost_for(LLMUsage(input_tokens=M, output_tokens=M, model="gemini-2.5-flash-lite")) - 0.50) < 1e-9
    assert abs(cost_for(LLMUsage(input_tokens=M, output_tokens=M, model="gemini-2.0-flash")) - 0.50) < 1e-9


def test_prefix_match_gemini_preview_suffix():
    # google ships preview/date-suffixed ids; the bare base id must still price.
    u = LLMUsage(input_tokens=M, model="gemini-2.5-pro-preview-06-05")
    assert cost_for(u) == 1.25  # resolves to gemini-2.5-pro


def test_openrouter_anthropic_haiku_priced_at_haiku_rate():
    """OpenRouter ids are namespaced (`<vendor>/<model>`). The explicit
    entry matches the underlying Anthropic Haiku rate -- reconciled to the
    OpenRouter invoice once at adoption time."""
    usage = LLMUsage(input_tokens=M, output_tokens=M, model="anthropic/claude-haiku-4-5")
    # haiku: $1 input + $5 output = $6 per 1M tokens
    assert cost_for(usage) == 6.0


def test_openrouter_openai_gpt4o_mini_priced_at_mini_rate():
    usage = LLMUsage(input_tokens=M, output_tokens=M, model="openai/gpt-4o-mini")
    # gpt-4o-mini: $0.15 input + $0.60 output = $0.75 per 1M tokens
    assert abs(cost_for(usage) - 0.75) < 1e-9


def test_unknown_openrouter_id_returns_none_with_warning(caplog):
    """A truly novel OpenRouter id with no explicit entry returns None and
    logs one warning -- same behavior as any other unknown model."""
    import logging
    caplog.set_level(logging.WARNING, logger="fabri")
    usage = LLMUsage(input_tokens=M, model="meta-llama/some-future-model")
    assert cost_for(usage) is None
    assert any("no pricing entry" in rec.message for rec in caplog.records)


# ---- AWS Bedrock: vendor-namespaced ids, incl. region-profile prefixes -----

def test_bedrock_claude_sonnet_priced_at_sonnet_rate():
    """Bare Bedrock Claude id, plus a date-suffixed model id, both resolve via
    the `anthropic.claude-3-5-sonnet` prefix entry to the Sonnet rate."""
    bare = LLMUsage(input_tokens=M, output_tokens=M, model="anthropic.claude-3-5-sonnet")
    suffixed = LLMUsage(input_tokens=M, output_tokens=M, model="anthropic.claude-3-5-sonnet-20241022-v2:0")
    assert cost_for(bare) == 18.0  # $3 input + $15 output
    assert cost_for(suffixed) == 18.0


def test_bedrock_region_prefixed_claude_needs_own_entry():
    """`_rates_for` anchors `startswith` at position 0, so a `us.`-prefixed
    inference-profile id does NOT match the bare `anthropic.…` key -- it has its
    own entry and must still price."""
    usage = LLMUsage(input_tokens=M, output_tokens=M, model="us.anthropic.claude-3-5-sonnet-20241022-v2:0")
    assert cost_for(usage) == 18.0


def test_bedrock_openai_gpt_oss_priced():
    usage = LLMUsage(input_tokens=M, output_tokens=M, model="openai.gpt-oss-120b")
    # gpt-oss-120b: $0.15 input + $0.60 output = $0.75 per 1M tokens
    assert abs(cost_for(usage) - 0.75) < 1e-9


# ---- all four token buckets in one usage -----------------------------------

def test_all_four_buckets_sonnet_hand_computed():
    # Sonnet input $3, output $15. cache write 1.25x input = $3.75, read 0.10x = $0.30.
    #   in:   2M * 3   /1M = 6.0
    #   out:  1M * 15  /1M = 15.0
    #   cc:   4M * 3.75/1M = 15.0
    #   cr:  10M * 0.30/1M = 3.0
    # total = 39.0
    u = LLMUsage(
        input_tokens=2 * M,
        output_tokens=1 * M,
        cache_creation_input_tokens=4 * M,
        cache_read_input_tokens=10 * M,
        model="claude-sonnet-4-6",
    )
    assert cost_for(u) == 39.0
    assert cost_for(u) == _expected("claude-sonnet-4-6", 2 * M, 1 * M, 4 * M, 10 * M)


def test_all_four_buckets_gpt4o_hand_computed():
    # gpt-4o input $2.5, output $10. cc 1.25x=$3.125, cr 0.10x=$0.25.
    #   in:  1M * 2.5  /1M = 2.5
    #   out: 1M * 10   /1M = 10.0
    #   cc:  1M * 3.125/1M = 3.125
    #   cr:  1M * 0.25 /1M = 0.25
    # total = 15.875
    u = LLMUsage(
        input_tokens=M,
        output_tokens=M,
        cache_creation_input_tokens=M,
        cache_read_input_tokens=M,
        model="gpt-4o",
    )
    assert cost_for(u) == 15.875


# ---- cache multipliers, verified precisely ---------------------------------

def test_cache_write_is_1_25x_input_sonnet():
    # 1M cache-creation @ sonnet input $3 * 1.25 = $3.75.
    u = LLMUsage(cache_creation_input_tokens=M, model="claude-sonnet-4-6")
    assert cost_for(u) == round(3.0 * _CACHE_WRITE_MULT, 6) == 3.75


def test_cache_read_is_0_10x_input_sonnet():
    # 1M cache-read @ sonnet input $3 * 0.10 = $0.30.
    u = LLMUsage(cache_read_input_tokens=M, model="claude-sonnet-4-6")
    assert cost_for(u) == round(3.0 * _CACHE_READ_MULT, 6) == 0.3


def test_cache_write_is_1_25x_input_haiku():
    # 1M cache-creation @ haiku input $1 * 1.25 = $1.25.
    u = LLMUsage(cache_creation_input_tokens=M, model="claude-haiku-4-5")
    assert cost_for(u) == 1.25


def test_cache_read_is_0_10x_input_haiku():
    # 1M cache-read @ haiku input $1 * 0.10 = $0.10.
    u = LLMUsage(cache_read_input_tokens=M, model="claude-haiku-4-5")
    assert cost_for(u) == 0.1


def test_cache_write_plus_read_combined_haiku():
    u = LLMUsage(
        cache_creation_input_tokens=M,
        cache_read_input_tokens=M,
        model="claude-haiku-4-5",
    )
    assert cost_for(u) == round(1.25 + 0.10, 6) == 1.35


# ---- prefix matching -------------------------------------------------------

def test_prefix_match_date_suffixed_haiku():
    u = LLMUsage(input_tokens=M, model="claude-haiku-4-5-20251001")
    assert cost_for(u) == 1.0  # resolves to claude-haiku-4-5


def test_prefix_match_date_suffixed_sonnet_future_date():
    u = LLMUsage(input_tokens=M, model="claude-sonnet-4-6-20990101")
    assert cost_for(u) == 3.0  # resolves to claude-sonnet-4-6


def test_prefix_match_opus_variant_suffix():
    u = LLMUsage(output_tokens=M, model="claude-opus-4-8-20260101")
    assert cost_for(u) == 25.0


def test_longest_prefix_wins_when_ambiguous():
    # Construct an ambiguous table where one key is a strict prefix of another,
    # so the model id matches BOTH; the longest matching base id must win.
    import fabri.pricing as pricing

    original = dict(pricing.PRICING)
    try:
        pricing.PRICING.clear()
        pricing.PRICING.update(
            {
                "foo-model": (1.0, 2.0),  # short prefix, would price input at $1
                "foo-model-pro": (7.0, 9.0),  # longer prefix, the correct match
            }
        )
        # "foo-model-pro-2026" startswith both keys; longest (foo-model-pro) wins.
        u = LLMUsage(input_tokens=M, model="foo-model-pro-2026")
        assert cost_for(u) == 7.0
    finally:
        pricing.PRICING.clear()
        pricing.PRICING.update(original)


# ---- unknown / absent model ------------------------------------------------

def test_unknown_model_is_none():
    assert cost_for(LLMUsage(input_tokens=100, model="totally-made-up-model")) is None


def test_model_none_is_none():
    assert cost_for(LLMUsage(input_tokens=100, model=None)) is None


def test_empty_string_model_is_none():
    # `not model` short-circuits on "" the same as None -> None.
    assert cost_for(LLMUsage(input_tokens=100, model="")) is None


def test_partial_nonmatching_prefix_is_none():
    # A model that is itself a prefix of a known key (but not a superstring of
    # one) must NOT match: "claude-haiku" does not start-with "claude-haiku-4-5".
    assert cost_for(LLMUsage(input_tokens=100, model="claude-haiku")) is None


# ---- zero tokens with a known model is a real 0, not None ------------------

def test_zero_tokens_known_model_is_zero_not_none():
    u = LLMUsage(model="claude-sonnet-4-6")  # all token buckets default to 0
    out = cost_for(u)
    assert out == 0.0
    assert out is not None


def test_zero_tokens_prefix_model_is_zero():
    u = LLMUsage(model="claude-haiku-4-5-20251001")
    assert cost_for(u) == 0.0


# ---- rounding to 6 decimals ------------------------------------------------

def test_rounds_to_six_decimals():
    # 1 cache-read token @ haiku input $1 * 0.10 / 1e6 = 1e-7, which rounds to
    # 0.0 at 6 dp. 1 input token @ haiku = 1e-6 = 0.000001 exactly.
    assert cost_for(LLMUsage(cache_read_input_tokens=1, model="claude-haiku-4-5")) == 0.0
    assert cost_for(LLMUsage(input_tokens=1, model="claude-haiku-4-5")) == 0.000001


def test_rounding_does_not_truncate_below_six_dp():
    # 3 input tokens @ sonnet $3 = 9 / 1e6 = 9e-6 = 0.000009 (exact at 6 dp).
    assert cost_for(LLMUsage(input_tokens=3, model="claude-sonnet-4-6")) == 0.000009


def test_result_is_rounded_float_for_messy_value():
    # Pick tokens that produce a value with >6 decimal places before rounding.
    # 7 cache-read tokens @ sonnet $3 * 0.10 = 2.1 / 1e6 = 2.1e-6 -> 0.000002.
    val = cost_for(LLMUsage(cache_read_input_tokens=7, model="claude-sonnet-4-6"))
    assert val == 0.000002
    # The returned value must already be rounded (no long float tail).
    assert val == round(val, 6)
