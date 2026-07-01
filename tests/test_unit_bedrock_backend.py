"""Behavioral tests for BedrockLLMBackend (AWS Bedrock Converse API via boto3).

boto3 is imported lazily inside the backend, so every test that needs the error
types injects fake `boto3` / `botocore` / `botocore.exceptions` / `botocore.config`
modules into sys.modules and builds the backend via __new__ with a recording
client -- no install, no AWS creds, no network. Mirrors the stubbing style of
test_unit_gemini_backend.py.

Focus areas, all Bedrock-specific:
  * set_tools -> toolSpec/inputSchema.json shape; toolConfig omitted when empty
  * outbound translation to Converse content blocks incl. coalescing consecutive
    same-role turns (Converse requires strict alternation)
  * system routed to system=[{"text": ...}], maxTokens into inferenceConfig
  * response parsing by KEY PRESENCE (no `type` field); usage incl. cache keys
  * MAX_TOKENS retry-once-then-fail parity; terminal stopReasons -> LLMError
  * transient throttling retried via _call_with_retry; non-transient wrapped
"""
import sys
import types as pytypes

import pytest

from fabri.core.llm import BedrockLLMBackend, LLMError


# --------------------------------------------------------------------------- #
# Fake boto3 / botocore packages
# --------------------------------------------------------------------------- #
def _install_fake_botocore(monkeypatch):
    """Inject botocore / botocore.exceptions / botocore.config with the exception
    types the backend's step() references. The modeled Bedrock exceptions
    subclass the fake ClientError (mirrors botocore's errorfactory). Returns the
    fake exceptions module so a test can raise the SDK's types."""
    botocore_mod = pytypes.ModuleType("botocore")
    exc_mod = pytypes.ModuleType("botocore.exceptions")
    config_mod = pytypes.ModuleType("botocore.config")

    class BotoCoreError(Exception):
        pass

    class ClientError(Exception):
        # Lenient init so a test can raise ClientError() or
        # ClientError({"Error": {"Code": "AccessDenied"}}, "Converse").
        def __init__(self, error_response=None, operation_name=None):
            self.response = error_response or {}
            self.operation_name = operation_name
            super().__init__(str(error_response))

    class EndpointConnectionError(BotoCoreError):
        pass

    class NoRegionError(BotoCoreError):
        pass

    class NoCredentialsError(BotoCoreError):
        pass

    # Bedrock-runtime modeled exceptions (reachable via client.exceptions.*),
    # all subclasses of ClientError.
    class ThrottlingException(ClientError):
        pass

    class ModelTimeoutException(ClientError):
        pass

    class InternalServerException(ClientError):
        pass

    class ServiceUnavailableException(ClientError):
        pass

    class ModelNotReadyException(ClientError):
        pass

    class AccessDeniedException(ClientError):
        pass

    for name, obj in {
        "BotoCoreError": BotoCoreError,
        "ClientError": ClientError,
        "EndpointConnectionError": EndpointConnectionError,
        "NoRegionError": NoRegionError,
        "NoCredentialsError": NoCredentialsError,
        "ThrottlingException": ThrottlingException,
        "ModelTimeoutException": ModelTimeoutException,
        "InternalServerException": InternalServerException,
        "ServiceUnavailableException": ServiceUnavailableException,
        "ModelNotReadyException": ModelNotReadyException,
        "AccessDeniedException": AccessDeniedException,
    }.items():
        setattr(exc_mod, name, obj)

    config_mod.Config = lambda **kw: pytypes.SimpleNamespace(**kw)

    botocore_mod.exceptions = exc_mod
    botocore_mod.config = config_mod
    monkeypatch.setitem(sys.modules, "botocore", botocore_mod)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", exc_mod)
    monkeypatch.setitem(sys.modules, "botocore.config", config_mod)
    return exc_mod


class _BedrockClient:
    """Mimics a bedrock-runtime client: `.converse(**kwargs)` over a fixed seq.
    A scripted item that is an exception instance is raised instead of returned
    (exercises the transient-retry / error-wrap paths). `.exceptions` exposes the
    modeled exception types step() builds its transient tuple from."""

    def __init__(self, responses, exc_mod):
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.exceptions = pytypes.SimpleNamespace(
            ThrottlingException=exc_mod.ThrottlingException,
            ModelTimeoutException=exc_mod.ModelTimeoutException,
            InternalServerException=exc_mod.InternalServerException,
            ServiceUnavailableException=exc_mod.ServiceUnavailableException,
            ModelNotReadyException=exc_mod.ModelNotReadyException,
        )

    def converse(self, **kwargs):
        item = self._responses[len(self.calls)]
        self.calls.append(kwargs)
        if isinstance(item, BaseException):
            raise item
        return item


def _bedrock_backend(responses, exc_mod, *, max_tokens=4096, model="bedrock-test", tools=None):
    b = BedrockLLMBackend.__new__(BedrockLLMBackend)
    b._model = model
    b._max_tokens = max_tokens
    b._tools = tools or []
    b._client = _BedrockClient(responses, exc_mod)
    return b


def _usage(inp=10, out=2, cache_read=0, cache_write=0, include_cache=True):
    u = {"inputTokens": inp, "outputTokens": out, "totalTokens": inp + out}
    if include_cache:
        u["cacheReadInputTokens"] = cache_read
        u["cacheWriteInputTokens"] = cache_write
    return u


def _text_resp(text="ok", stop="end_turn", usage=None):
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": stop,
        "usage": usage or _usage(),
    }


def _tool_resp(stop="tool_use", thinking="let me check", usage=None):
    content = []
    if thinking is not None:
        content.append({"text": thinking})
    content.append({"toolUse": {"toolUseId": "tu_1", "name": "read_file", "input": {"path": "x.txt"}}})
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": stop,
        "usage": usage or _usage(),
    }


# --------------------------------------------------------------------------- #
# set_tools (pure dict work, no SDK needed)
# --------------------------------------------------------------------------- #
def test_set_tools_wraps_in_toolspec_with_inputschema_json():
    b = BedrockLLMBackend.__new__(BedrockLLMBackend)
    b.set_tools([
        {"name": "read_file", "description": "read a file", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}},
    ])
    spec = b._tools[0]["toolSpec"]
    assert spec["name"] == "read_file"
    assert spec["description"] == "read a file"
    assert spec["inputSchema"]["json"] == {"type": "object", "properties": {"path": {"type": "string"}}}


def test_set_tools_defaults_empty_schema_to_object():
    b = BedrockLLMBackend.__new__(BedrockLLMBackend)
    b.set_tools([{"name": "noop", "description": "d"}])
    assert b._tools[0]["toolSpec"]["inputSchema"]["json"] == {"type": "object"}


def test_set_tools_preserves_order():
    b = BedrockLLMBackend.__new__(BedrockLLMBackend)
    b.set_tools([
        {"name": "read_file", "description": "r", "input_schema": {"type": "object"}},
        {"name": "write_file", "description": "w", "input_schema": {"type": "object"}},
    ])
    assert [t["toolSpec"]["name"] for t in b._tools] == ["read_file", "write_file"]


# --------------------------------------------------------------------------- #
# Outbound translation -> Converse content blocks
# --------------------------------------------------------------------------- #
def test_outbound_string_content_becomes_text_block():
    msgs = [{"role": "user", "content": "go"}]
    out = BedrockLLMBackend._to_converse_messages(msgs)
    assert out == [{"role": "user", "content": [{"text": "go"}]}]


def test_outbound_assistant_text_plus_tool_use():
    msgs = [{"role": "assistant", "content": [
        {"type": "text", "text": "checking"},
        {"type": "tool_use", "name": "read_file", "input": {"path": "x"}, "id": "tu_1"},
    ]}]
    out = BedrockLLMBackend._to_converse_messages(msgs)
    assert out[0]["role"] == "assistant"
    assert out[0]["content"][0] == {"text": "checking"}
    assert out[0]["content"][1] == {"toolUse": {"toolUseId": "tu_1", "name": "read_file", "input": {"path": "x"}}}


def test_outbound_tool_result_string_is_text_block():
    msgs = [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu_1", "content": "file contents"},
    ]}]
    out = BedrockLLMBackend._to_converse_messages(msgs)
    tr = out[0]["content"][0]["toolResult"]
    assert tr["toolUseId"] == "tu_1"
    assert tr["content"] == [{"text": "file contents"}]


def test_outbound_tool_result_dict_is_json_block():
    msgs = [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu_9", "content": {"rows": 3}},
    ]}]
    out = BedrockLLMBackend._to_converse_messages(msgs)
    assert out[0]["content"][0]["toolResult"]["content"] == [{"json": {"rows": 3}}]


def test_outbound_tool_result_list_is_json_block():
    msgs = [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu_5", "content": ["a", "b"]},
    ]}]
    out = BedrockLLMBackend._to_converse_messages(msgs)
    assert out[0]["content"][0]["toolResult"]["content"] == [{"json": ["a", "b"]}]


def test_outbound_non_tool_result_user_text_block_survives():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "follow-up"}]}]
    out = BedrockLLMBackend._to_converse_messages(msgs)
    assert out[0]["content"] == [{"text": "follow-up"}]


def test_outbound_coalesces_consecutive_same_role_turns():
    """Converse requires strict user/assistant alternation. If the history ever
    has two same-role turns in a row, they must merge into one."""
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
    ]
    out = BedrockLLMBackend._to_converse_messages(msgs)
    assert len(out) == 1
    assert out[0]["role"] == "user"
    assert out[0]["content"] == [{"text": "first"}, {"text": "second"}]


def test_outbound_empty_text_blocks_skipped_and_alternation_preserved():
    """An empty assistant text turn is dropped; the surrounding user turns must
    NOT collapse into an illegal pair -- coalescing handles it."""
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": [{"type": "text", "text": "   "}]},
        {"role": "user", "content": "b"},
    ]
    out = BedrockLLMBackend._to_converse_messages(msgs)
    assert [m["role"] for m in out] == ["user"]
    assert out[0]["content"] == [{"text": "a"}, {"text": "b"}]


# --------------------------------------------------------------------------- #
# step(): system routing, maxTokens, response parsing, usage
# --------------------------------------------------------------------------- #
def test_step_routes_system_to_system_blocks_and_maxtokens_to_inferenceconfig(monkeypatch):
    exc = _install_fake_botocore(monkeypatch)
    b = _bedrock_backend([_text_resp(text="hi")], exc, max_tokens=1234)
    b.step("YOU ARE FABRI", [{"role": "user", "content": "hi"}])
    call = b._client.calls[0]
    assert call["system"] == [{"text": "YOU ARE FABRI"}]
    assert call["inferenceConfig"] == {"maxTokens": 1234}
    assert call["modelId"] == "bedrock-test"
    # system is NOT folded into messages
    assert call["messages"][0]["role"] == "user"


def test_step_empty_system_omits_system_kwarg(monkeypatch):
    exc = _install_fake_botocore(monkeypatch)
    b = _bedrock_backend([_text_resp()], exc)
    b.step("   ", [{"role": "user", "content": "go"}])
    assert "system" not in b._client.calls[0]


def test_step_no_tools_omits_toolconfig(monkeypatch):
    exc = _install_fake_botocore(monkeypatch)
    b = _bedrock_backend([_text_resp()], exc, tools=[])
    b.step("sys", [{"role": "user", "content": "go"}])
    assert "toolConfig" not in b._client.calls[0]


def test_step_with_tools_passes_toolconfig(monkeypatch):
    exc = _install_fake_botocore(monkeypatch)
    b = _bedrock_backend([_text_resp()], exc)
    b.set_tools([{"name": "read_file", "description": "r", "input_schema": {"type": "object"}}])
    b.step("sys", [{"role": "user", "content": "go"}])
    tc = b._client.calls[0]["toolConfig"]
    assert tc["tools"][0]["toolSpec"]["name"] == "read_file"


def test_step_text_response_parsing_and_usage(monkeypatch):
    exc = _install_fake_botocore(monkeypatch)
    b = _bedrock_backend(
        [_text_resp(text="done", usage=_usage(inp=12, out=4, cache_read=3, cache_write=1))],
        exc,
        model="us.anthropic.claude-3-5-sonnet",
    )
    resp = b.step("sys", [{"role": "user", "content": "go"}])
    assert resp.final_text == "done"
    assert resp.tool_calls == []
    assert resp.usage.model == "us.anthropic.claude-3-5-sonnet"
    assert resp.usage.input_tokens == 12
    assert resp.usage.output_tokens == 4
    assert resp.usage.cache_read_input_tokens == 3
    assert resp.usage.cache_creation_input_tokens == 1
    assert len(b._client.calls) == 1


def test_step_usage_without_cache_keys_defaults_to_zero(monkeypatch):
    exc = _install_fake_botocore(monkeypatch)
    b = _bedrock_backend([_text_resp(usage=_usage(inp=7, out=1, include_cache=False))], exc)
    resp = b.step("sys", [{"role": "user", "content": "go"}])
    assert resp.usage.input_tokens == 7
    assert resp.usage.cache_read_input_tokens == 0
    assert resp.usage.cache_creation_input_tokens == 0


def test_step_tool_call_parsing_and_thinking(monkeypatch):
    exc = _install_fake_botocore(monkeypatch)
    b = _bedrock_backend([_tool_resp(thinking="let me check existing files")], exc)
    resp = b.step("sys", [{"role": "user", "content": "go"}])
    assert resp.final_text is None
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.name == "read_file"
    assert tc.args == {"path": "x.txt"}
    assert tc.id == "tu_1"
    assert resp.thinking_text == "let me check existing files"


def test_step_tool_call_without_text_has_no_thinking(monkeypatch):
    exc = _install_fake_botocore(monkeypatch)
    b = _bedrock_backend([_tool_resp(thinking=None)], exc)
    resp = b.step("sys", [{"role": "user", "content": "go"}])
    assert len(resp.tool_calls) == 1
    assert resp.thinking_text is None


def test_step_parallel_tool_use_yields_distinct_calls(monkeypatch):
    exc = _install_fake_botocore(monkeypatch)
    resp_obj = {
        "output": {"message": {"role": "assistant", "content": [
            {"text": "reading two files"},
            {"toolUse": {"toolUseId": "tu_a", "name": "read_file", "input": {"path": "a"}}},
            {"toolUse": {"toolUseId": "tu_b", "name": "read_file", "input": {"path": "b"}}},
        ]}},
        "stopReason": "tool_use",
        "usage": _usage(),
    }
    b = _bedrock_backend([resp_obj], exc)
    out = b.step("sys", [{"role": "user", "content": "go"}])
    assert [c.name for c in out.tool_calls] == ["read_file", "read_file"]
    assert [c.args["path"] for c in out.tool_calls] == ["a", "b"]
    assert [c.id for c in out.tool_calls] == ["tu_a", "tu_b"]
    assert out.thinking_text == "reading two files"


def test_step_empty_content_yields_none_final_text(monkeypatch):
    exc = _install_fake_botocore(monkeypatch)
    resp_obj = {
        "output": {"message": {"role": "assistant", "content": []}},
        "stopReason": "end_turn",
        "usage": _usage(),
    }
    b = _bedrock_backend([resp_obj], exc)
    out = b.step("sys", [{"role": "user", "content": "go"}])
    assert out.tool_calls == []
    assert out.final_text is None
    assert out.usage.model == b._model


def test_step_no_truncation_is_a_single_call(monkeypatch):
    exc = _install_fake_botocore(monkeypatch)
    b = _bedrock_backend([_text_resp(stop="end_turn", usage=_usage(out=20))], exc)
    resp = b.step("sys", [{"role": "user", "content": "go"}])
    assert len(b._client.calls) == 1
    assert resp.usage.output_tokens == 20


# --------------------------------------------------------------------------- #
# MAX_TOKENS retry parity + terminal stopReasons
# --------------------------------------------------------------------------- #
def test_step_max_tokens_retries_once_then_succeeds(monkeypatch):
    exc = _install_fake_botocore(monkeypatch)
    b = _bedrock_backend(
        [
            _text_resp(stop="max_tokens", usage=_usage(inp=10, out=4096)),
            _text_resp(stop="end_turn", text="done", usage=_usage(inp=10, out=50)),
        ],
        exc,
        max_tokens=4096,
    )
    resp = b.step("sys", [{"role": "user", "content": "go"}])
    assert len(b._client.calls) == 2
    assert b._client.calls[0]["inferenceConfig"]["maxTokens"] == 4096
    assert b._client.calls[1]["inferenceConfig"]["maxTokens"] == 8192  # 2x, under ceiling
    assert resp.final_text == "done"
    # discarded truncated attempt's tokens fold into reported usage
    assert resp.usage.output_tokens == 4096 + 50
    assert resp.usage.input_tokens == 10 + 10


def test_step_max_tokens_twice_fails_loud(monkeypatch):
    exc = _install_fake_botocore(monkeypatch)
    b = _bedrock_backend(
        [_text_resp(stop="max_tokens"), _text_resp(stop="max_tokens")],
        exc,
        max_tokens=4096,
    )
    with pytest.raises(LLMError, match="even after retry"):
        b.step("sys", [{"role": "user", "content": "go"}])
    assert len(b._client.calls) == 2  # retried exactly once, then gave up


def test_step_context_window_exceeded_fails_without_retry(monkeypatch):
    """The INPUT is too big -- raising maxTokens can't help, so this must NOT go
    through the truncation retry; fail loud on the single call."""
    exc = _install_fake_botocore(monkeypatch)
    b = _bedrock_backend([_text_resp(stop="model_context_window_exceeded")], exc)
    with pytest.raises(LLMError, match="model_context_window_exceeded"):
        b.step("sys", [{"role": "user", "content": "go"}])
    assert len(b._client.calls) == 1


def test_step_content_filtered_maps_to_llmerror(monkeypatch):
    exc = _install_fake_botocore(monkeypatch)
    b = _bedrock_backend([_text_resp(stop="content_filtered")], exc)
    with pytest.raises(LLMError, match="content_filtered"):
        b.step("sys", [{"role": "user", "content": "go"}])


# --------------------------------------------------------------------------- #
# Error wrapping + transient retry
# --------------------------------------------------------------------------- #
def test_step_non_transient_client_error_wrapped(monkeypatch):
    """A non-transient ClientError (e.g. AccessDenied) wraps to LLMError on the
    first call -- NOT retried (only throttling/5xx are transient)."""
    exc = _install_fake_botocore(monkeypatch)
    err = exc.AccessDeniedException({"Error": {"Code": "AccessDeniedException"}}, "Converse")
    b = _bedrock_backend([err], exc)
    with pytest.raises(LLMError, match="bedrock API error"):
        b.step("sys", [{"role": "user", "content": "go"}])
    assert len(b._client.calls) == 1


def test_step_botocore_error_wrapped(monkeypatch):
    exc = _install_fake_botocore(monkeypatch)
    b = _bedrock_backend([exc.NoCredentialsError()], exc)
    with pytest.raises(LLMError, match="check AWS region/credentials"):
        b.step("sys", [{"role": "user", "content": "go"}])


def test_step_throttling_retried_then_succeeds(monkeypatch):
    exc = _install_fake_botocore(monkeypatch)
    monkeypatch.setattr("fabri.core.llm.time.sleep", lambda *a, **k: None)
    b = _bedrock_backend([exc.ThrottlingException(), _text_resp(text="recovered")], exc)
    resp = b.step("sys", [{"role": "user", "content": "go"}])
    assert resp.final_text == "recovered"
    assert len(b._client.calls) == 2


def test_step_throttling_exhausts_retries_to_llmerror(monkeypatch):
    exc = _install_fake_botocore(monkeypatch)
    monkeypatch.setattr("fabri.core.llm.time.sleep", lambda *a, **k: None)
    b = _bedrock_backend([exc.ThrottlingException() for _ in range(3)], exc)
    with pytest.raises(LLMError, match="failed after 3 attempts"):
        b.step("sys", [{"role": "user", "content": "go"}])
    assert len(b._client.calls) == 3


def test_step_endpoint_connection_error_retried(monkeypatch):
    exc = _install_fake_botocore(monkeypatch)
    monkeypatch.setattr("fabri.core.llm.time.sleep", lambda *a, **k: None)
    b = _bedrock_backend([exc.EndpointConnectionError(), _text_resp(text="ok")], exc)
    resp = b.step("sys", [{"role": "user", "content": "go"}])
    assert resp.final_text == "ok"
    assert len(b._client.calls) == 2


# --------------------------------------------------------------------------- #
# prewarm no-op
# --------------------------------------------------------------------------- #
def test_prewarm_is_noop_with_model_stamp():
    b = BedrockLLMBackend.__new__(BedrockLLMBackend)
    b._model = "bedrock-test"
    usage = b.prewarm("sys")
    assert usage.model == "bedrock-test"
    assert usage.input_tokens == 0
