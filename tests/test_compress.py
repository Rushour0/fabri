from agent_memory.memory.compress import count_tokens, enforce_token_cap


def test_short_text_is_unchanged():
    text = "Short guideline text."
    assert enforce_token_cap(text, max_tokens=30) == text


def test_long_text_is_truncated_to_cap():
    long_text = " ".join(["word"] * 200)
    capped = enforce_token_cap(long_text, max_tokens=10)
    # +3 tokens of slack for the "..." suffix encoding
    assert count_tokens(capped) <= 13
    assert capped.endswith("...")
