"""AnthropicLLMBackend retries ONCE at a higher cap on a max_tokens truncation
before failing the run -- so a single content-heavy turn (e.g. writing several
files) no longer nukes a whole multi-step build. We still fail loud if even the
retry truncates, and we never report a truncated answer as success.
"""
from types import SimpleNamespace

import pytest

from fabri.core.llm import (
    ANTHROPIC_MAX_TOKENS_CEILING,
    AnthropicLLMBackend,
    LLMError,
)


class _StreamCtx:
    """Stands in for anthropic's messages.stream() context manager."""

    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._resp


class _SeqClient:
    """Returns a pre-set sequence of responses, one per stream()/create() call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.streamed = False
        self.messages = self

    def create(self, **kwargs):
        resp = self._responses[len(self.calls)]
        self.calls.append(kwargs)
        return resp

    def stream(self, **kwargs):
        # step() streams; record the kwargs (so max_tokens assertions still hold)
        # and hand back the sequenced response via get_final_message().
        self.streamed = True
        resp = self._responses[len(self.calls)]
        self.calls.append(kwargs)
        return _StreamCtx(resp)


def _resp(stop_reason, *, out=2, inp=10):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=inp,
            output_tokens=out,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


def _backend(responses, max_tokens=4096):
    b = AnthropicLLMBackend.__new__(AnthropicLLMBackend)
    b._model = "claude-sonnet-4-6"
    b._max_tokens = max_tokens
    b._tools = []
    b._enable_prompt_cache = False
    b._cache_messages = False  # G21: not exercised by these tests
    b._client = _SeqClient(responses)
    return b


def test_truncation_retries_once_at_double_cap_then_succeeds():
    b = _backend([_resp("max_tokens", out=4096), _resp("end_turn", out=50)])
    resp = b.step("sys", [{"role": "user", "content": "go"}])
    # Two calls: the first at the configured cap, the retry at 2x (bounded).
    assert len(b._client.calls) == 2
    assert b._client.calls[0]["max_tokens"] == 4096
    assert b._client.calls[1]["max_tokens"] == 8192
    assert resp.final_text == "ok"
    # COGS: the discarded truncated attempt was still billed, so its tokens fold
    # into the reported usage (4096 + 50 output).
    assert resp.usage.output_tokens == 4096 + 50
    assert resp.usage.input_tokens == 10 + 10


def test_truncation_twice_fails_loud():
    b = _backend([_resp("max_tokens"), _resp("max_tokens")])
    with pytest.raises(LLMError, match="even after retry"):
        b.step("sys", [{"role": "user", "content": "go"}])
    assert len(b._client.calls) == 2  # retried exactly once, then gave up


def test_no_truncation_is_a_single_call():
    b = _backend([_resp("end_turn", out=20)])
    resp = b.step("sys", [{"role": "user", "content": "go"}])
    assert len(b._client.calls) == 1
    assert resp.usage.output_tokens == 20


def test_retry_cap_is_bounded_by_ceiling():
    # Streaming lets the retry reach the model's real output ceiling, but no
    # further: a very high configured cap still clamps to ANTHROPIC_MAX_TOKENS_CEILING.
    b = _backend([_resp("max_tokens"), _resp("end_turn")], max_tokens=40000)
    b.step("sys", [{"role": "user", "content": "go"}])
    # 40000*2 = 80000, clamped to the 64000 ceiling.
    assert b._client.calls[1]["max_tokens"] == ANTHROPIC_MAX_TOKENS_CEILING


def test_step_uses_streaming_transport():
    # The Anthropic completion path must stream (so large max_tokens don't trip
    # the SDK's non-streaming HTTP timeout), not call messages.create().
    b = _backend([_resp("end_turn", out=5)])
    b.step("sys", [{"role": "user", "content": "go"}])
    assert b._client.streamed is True
