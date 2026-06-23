"""Thorough behavioral tests for the fabri LLM provider backends.

Covers the bits NOT already pinned by test_unit_llm_caching.py (call shape) and
test_unit_max_tokens_retry.py (Anthropic retry): the OpenAI backend's truncation
retry + parity, tool_call/usage round-trips on BOTH providers, prompt-cache token
surfacing, model-id stamping on LLMUsage, and prewarm behavior.

Backends are constructed via `__new__` + manual attribute set so no real SDK
client / env var is touched; the SDK client is replaced with a recording stub.
"""
from types import SimpleNamespace

import pytest

from fabri.core.llm import (
    AnthropicLLMBackend,
    LLMError,
    MAX_TOKENS_RETRY_CEILING,
    OpenAILLMBackend,
    ScriptedLLMBackend,
    LLMUsage,
)

# OpenAILLMBackend.step()/prewarm() lazily `import openai`; skip those cases if
# the SDK isn't installed in the test venv.
openai = pytest.importorskip("openai")


# --------------------------------------------------------------------------- #
# Anthropic stubs / builders
# --------------------------------------------------------------------------- #
class _AnthropicSeqClient:
    """Returns a pre-set sequence of responses, one per create() call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.messages = self  # so `.messages.create(...)` works

    def create(self, **kwargs):
        resp = self._responses[len(self.calls)]
        self.calls.append(kwargs)
        return resp


def _au(input_tokens=10, output_tokens=2, cache_creation=0, cache_read=0):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
    )


def _anthropic_text_resp(stop_reason="end_turn", text="ok", usage=None):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=usage or _au(),
    )


def _anthropic_tool_resp(stop_reason="tool_use", thinking="let me check", usage=None):
    content = []
    if thinking is not None:
        content.append(SimpleNamespace(type="text", text=thinking))
    content.append(
        SimpleNamespace(type="tool_use", name="read_file", input={"path": "x.txt"}, id="toolu_1")
    )
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=usage or _au())


def _anthropic_backend(responses, *, max_tokens=4096, enable_cache=False, model="claude-test"):
    b = AnthropicLLMBackend.__new__(AnthropicLLMBackend)
    b._model = model
    b._max_tokens = max_tokens
    b._tools = []
    b._enable_prompt_cache = enable_cache
    b._client = _AnthropicSeqClient(responses)
    return b


# --------------------------------------------------------------------------- #
# OpenAI stubs / builders
# --------------------------------------------------------------------------- #
class _OpenAICompletions:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        resp = self._responses[len(self.calls)]
        self.calls.append(kwargs)
        return resp


class _OpenAISeqClient:
    """Mimics openai.OpenAI: `.chat.completions.create(...)`."""

    def __init__(self, responses):
        self.completions = _OpenAICompletions(responses)
        self.chat = SimpleNamespace(completions=self.completions)

    @property
    def calls(self):
        return self.completions.calls


def _ou(prompt_tokens=10, completion_tokens=2, cached_tokens=0):
    return SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
    )


def _openai_text_resp(finish_reason="stop", content="ok", usage=None):
    msg = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish_reason, message=msg)],
        usage=usage or _ou(),
    )


def _openai_tool_resp(finish_reason="tool_calls", usage=None):
    tc = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name="read_file", arguments='{"path": "x.txt"}'),
    )
    msg = SimpleNamespace(content=None, tool_calls=[tc])
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish_reason, message=msg)],
        usage=usage or _ou(),
    )


def _openai_backend(responses, *, max_tokens=4096, model="gpt-test"):
    b = OpenAILLMBackend.__new__(OpenAILLMBackend)
    b._model = model
    b._max_tokens = max_tokens
    b._tools = []
    b._client = _OpenAISeqClient(responses)
    return b


# ========================================================================== #
# LLMUsage.model stamping
# ========================================================================== #
def test_anthropic_normal_step_stamps_model_on_usage():
    b = _anthropic_backend([_anthropic_text_resp(usage=_au(input_tokens=11, output_tokens=3))],
                           model="claude-sonnet-4-6")
    resp = b.step("sys", [{"role": "user", "content": "hi"}])
    assert resp.usage.model == "claude-sonnet-4-6"
    assert resp.usage.input_tokens == 11
    assert resp.usage.output_tokens == 3
    assert resp.final_text == "ok"
    assert len(b._client.calls) == 1


def test_openai_normal_step_stamps_model_on_usage():
    b = _openai_backend([_openai_text_resp(usage=_ou(prompt_tokens=12, completion_tokens=4))],
                        model="gpt-4o")
    resp = b.step("sys", [{"role": "user", "content": "hi"}])
    assert resp.usage.model == "gpt-4o"
    assert resp.usage.input_tokens == 12
    assert resp.usage.output_tokens == 4
    assert resp.final_text == "ok"
    assert len(b._client.calls) == 1


# ========================================================================== #
# Tool-call round-trips
# ========================================================================== #
def test_anthropic_tool_use_returns_tool_calls_and_thinking_text():
    b = _anthropic_backend([_anthropic_tool_resp(thinking="Let me check existing files")])
    resp = b.step("sys", [{"role": "user", "content": "go"}])
    assert resp.final_text is None
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.name == "read_file"
    assert tc.args == {"path": "x.txt"}
    assert tc.id == "toolu_1"
    # Accompanying text block surfaces as thinking_text.
    assert resp.thinking_text == "Let me check existing files"
    assert resp.usage.model == "claude-test"


def test_anthropic_tool_use_without_text_has_no_thinking_text():
    b = _anthropic_backend([_anthropic_tool_resp(thinking=None)])
    resp = b.step("sys", [{"role": "user", "content": "go"}])
    assert len(resp.tool_calls) == 1
    assert resp.thinking_text is None


def test_openai_tool_calls_round_trip():
    b = _openai_backend([_openai_tool_resp()])
    resp = b.step("sys", [{"role": "user", "content": "go"}])
    assert resp.final_text is None
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.name == "read_file"
    assert tc.args == {"path": "x.txt"}
    assert tc.id == "call_1"
    assert resp.stop_reason == "tool_calls"
    assert resp.usage.model == "gpt-test"


# ========================================================================== #
# Prompt-cache token surfacing
# ========================================================================== #
def test_anthropic_cache_tokens_surface_onto_usage():
    resp_usage = _au(input_tokens=20, output_tokens=5, cache_creation=100, cache_read=40)
    b = _anthropic_backend([_anthropic_text_resp(usage=resp_usage)])
    resp = b.step("sys", [{"role": "user", "content": "hi"}])
    assert resp.usage.cache_creation_input_tokens == 100
    assert resp.usage.cache_read_input_tokens == 40
    assert resp.usage.input_tokens == 20


def test_openai_cached_tokens_map_to_cache_read():
    b = _openai_backend([_openai_text_resp(usage=_ou(prompt_tokens=50, completion_tokens=5, cached_tokens=30))])
    resp = b.step("sys", [{"role": "user", "content": "hi"}])
    assert resp.usage.cache_read_input_tokens == 30
    assert resp.usage.input_tokens == 50
    # OpenAI backend doesn't model a separate cache-write field.
    assert resp.usage.cache_creation_input_tokens == 0


# ========================================================================== #
# OpenAI truncation retry (parity with Anthropic)
# ========================================================================== #
def test_openai_truncation_retries_once_then_succeeds():
    b = _openai_backend(
        [
            _openai_text_resp(finish_reason="length", usage=_ou(prompt_tokens=10, completion_tokens=4096)),
            _openai_text_resp(finish_reason="stop", usage=_ou(prompt_tokens=10, completion_tokens=50)),
        ],
        max_tokens=4096,
    )
    resp = b.step("sys", [{"role": "user", "content": "go"}])
    assert len(b._client.calls) == 2
    assert b._client.calls[0]["max_tokens"] == 4096
    assert b._client.calls[1]["max_tokens"] == 8192  # 2x, under the ceiling
    assert resp.final_text == "ok"
    # Discarded truncated attempt's tokens fold into reported usage.
    assert resp.usage.output_tokens == 4096 + 50
    assert resp.usage.input_tokens == 10 + 10


def test_openai_truncation_twice_fails_loud():
    b = _openai_backend(
        [_openai_text_resp(finish_reason="length"), _openai_text_resp(finish_reason="length")],
        max_tokens=4096,
    )
    with pytest.raises(LLMError, match="even after retry"):
        b.step("sys", [{"role": "user", "content": "go"}])
    assert len(b._client.calls) == 2  # retried exactly once, then gave up


def test_openai_no_truncation_is_a_single_call():
    b = _openai_backend([_openai_text_resp(finish_reason="stop", usage=_ou(completion_tokens=20))])
    resp = b.step("sys", [{"role": "user", "content": "go"}])
    assert len(b._client.calls) == 1
    assert resp.usage.output_tokens == 20


def test_openai_retry_cap_bounded_by_ceiling():
    b = _openai_backend(
        [_openai_text_resp(finish_reason="length"), _openai_text_resp(finish_reason="stop")],
        max_tokens=12000,
    )
    b.step("sys", [{"role": "user", "content": "go"}])
    assert b._client.calls[1]["max_tokens"] == MAX_TOKENS_RETRY_CEILING  # 16000, not 24000


def test_openai_truncation_retry_preserves_tool_calls():
    # If the retried (2nd) response carries tool_calls, they round-trip through.
    b = _openai_backend(
        [_openai_text_resp(finish_reason="length"), _openai_tool_resp(finish_reason="tool_calls")],
        max_tokens=4096,
    )
    resp = b.step("sys", [{"role": "user", "content": "go"}])
    assert len(b._client.calls) == 2
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "read_file"


# ========================================================================== #
# Anthropic retry preserves tool_calls on the retried response
# ========================================================================== #
def test_anthropic_retry_preserves_tool_calls():
    b = _anthropic_backend(
        [_anthropic_text_resp(stop_reason="max_tokens"), _anthropic_tool_resp(stop_reason="tool_use")]
    )
    resp = b.step("sys", [{"role": "user", "content": "go"}])
    assert len(b._client.calls) == 2
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "read_file"
    assert resp.tool_calls[0].id == "toolu_1"


def test_anthropic_cache_token_folding_across_retry():
    # attempt1 truncated with a cache_creation charge; retry hit a warm cache.
    attempt1 = _anthropic_text_resp(
        stop_reason="max_tokens",
        usage=_au(input_tokens=10, output_tokens=4096, cache_creation=200, cache_read=0),
    )
    retry = _anthropic_text_resp(
        stop_reason="end_turn",
        usage=_au(input_tokens=5, output_tokens=50, cache_creation=0, cache_read=180),
    )
    b = _anthropic_backend([attempt1, retry])
    resp = b.step("sys", [{"role": "user", "content": "go"}])
    assert resp.usage.cache_creation_input_tokens == 200 + 0
    assert resp.usage.cache_read_input_tokens == 0 + 180
    assert resp.usage.input_tokens == 10 + 5
    assert resp.usage.output_tokens == 4096 + 50


# ========================================================================== #
# prewarm behavior
# ========================================================================== #
def test_anthropic_prewarm_enabled_calls_create_with_max_tokens_zero():
    warm_resp = SimpleNamespace(
        usage=_au(input_tokens=120, output_tokens=0, cache_creation=120, cache_read=0)
    )
    b = _anthropic_backend([warm_resp], enable_cache=True, model="claude-warm")
    usage = b.prewarm("you are an agent")
    assert len(b._client.calls) == 1
    assert b._client.calls[0]["max_tokens"] == 0
    assert usage.model == "claude-warm"
    assert usage.cache_creation_input_tokens == 120
    assert usage.cache_read_input_tokens == 0
    assert usage.input_tokens == 120


def test_anthropic_prewarm_disabled_is_noop():
    b = _anthropic_backend([], enable_cache=False, model="claude-cold")
    usage = b.prewarm("you are an agent")
    assert len(b._client.calls) == 0  # no client call at all
    assert usage.model == "claude-cold"
    assert usage.cache_creation_input_tokens == 0
    assert usage.cache_read_input_tokens == 0
    assert usage.input_tokens == 0


def test_anthropic_prewarm_reports_warm_cache_read():
    warm_resp = SimpleNamespace(
        usage=_au(input_tokens=120, output_tokens=0, cache_creation=0, cache_read=120)
    )
    b = _anthropic_backend([warm_resp], enable_cache=True)
    usage = b.prewarm("sys")
    assert usage.cache_read_input_tokens == 120
    assert usage.cache_creation_input_tokens == 0


def test_openai_prewarm_is_noop():
    b = _openai_backend([], model="gpt-4o")
    usage = b.prewarm("sys")
    assert isinstance(usage, LLMUsage)
    assert usage.model == "gpt-4o"
    assert len(b._client.calls) == 0
    assert usage.input_tokens == 0


def test_scripted_prewarm_is_noop():
    b = ScriptedLLMBackend([])
    usage = b.prewarm("sys")
    assert isinstance(usage, LLMUsage)
    # Scripted backend doesn't know its model.
    assert usage.model is None
    assert usage.input_tokens == 0


# ========================================================================== #
# Misc edge cases
# ========================================================================== #
def test_anthropic_no_truncation_single_call_text():
    b = _anthropic_backend([_anthropic_text_resp(stop_reason="end_turn", text="done")])
    resp = b.step("sys", [{"role": "user", "content": "go"}])
    assert len(b._client.calls) == 1
    assert resp.final_text == "done"
    assert resp.tool_calls == []


def test_openai_message_translation_includes_system_prompt():
    b = _openai_backend([_openai_text_resp()])
    b.step("YOU ARE FABRI", [{"role": "user", "content": "hi"}])
    sent_messages = b._client.calls[0]["messages"]
    assert sent_messages[0] == {"role": "system", "content": "YOU ARE FABRI"}
    assert sent_messages[1] == {"role": "user", "content": "hi"}
