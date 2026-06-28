"""Behavioral tests for GeminiLLMBackend (native google-genai SDK).

The google-genai SDK is NOT a hard dependency, so every test injects a fake
`google.genai` package (types + errors + Client) into sys.modules and builds the
backend via __new__ with a recording client -- no install, no env var, no
network. Mirrors the stubbing style of test_llm_backends_thorough.py.

Focus areas, all Gemini-specific:
  * JSON-schema sanitization in set_tools (Gemini rejects $schema/additionalProperties/title)
  * outbound translation incl. the id->name function_response mapping
  * system prompt routed to system_instruction, not into contents
  * response parsing (text + function_call parts) + usage_metadata -> LLMUsage
  * MAX_TOKENS retry-once-then-fail parity with the other backends
  * provider APIError wrapped in LLMError
"""
import sys
import types as pytypes
from types import SimpleNamespace

import pytest

from fabri.core.llm import GeminiLLMBackend, LLMError


# --------------------------------------------------------------------------- #
# Fake google.genai package
# --------------------------------------------------------------------------- #
def _install_fake_genai(monkeypatch):
    """Inject google / google.genai / .types / .errors with just enough surface
    for the backend's lazy imports. Returns the fake errors module so a test can
    raise the SDK's exception types."""
    def _mk(**kw):
        return SimpleNamespace(**kw)

    google_mod = pytypes.ModuleType("google")
    genai_mod = pytypes.ModuleType("google.genai")
    types_mod = pytypes.ModuleType("google.genai.types")
    errors_mod = pytypes.ModuleType("google.genai.errors")

    for name in (
        "Part", "Content", "FunctionCall", "FunctionResponse",
        "Tool", "FunctionDeclaration", "GenerateContentConfig",
    ):
        setattr(types_mod, name, _mk)

    class APIError(Exception):
        pass

    class ServerError(APIError):
        pass

    errors_mod.APIError = APIError
    errors_mod.ServerError = ServerError

    genai_mod.types = types_mod
    genai_mod.errors = errors_mod
    genai_mod.Client = _mk
    google_mod.genai = genai_mod

    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.genai", genai_mod)
    monkeypatch.setitem(sys.modules, "google.genai.types", types_mod)
    monkeypatch.setitem(sys.modules, "google.genai.errors", errors_mod)
    return errors_mod


class _GeminiClient:
    """Mimics genai.Client: `.models.generate_content(...)` over a fixed seq.
    A scripted item that is an exception instance is raised instead of returned,
    so a test can exercise the transient-retry path."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.models = self

    def generate_content(self, **kwargs):
        item = self._responses[len(self.calls)]
        self.calls.append(kwargs)
        if isinstance(item, BaseException):
            raise item
        return item


class _RaisingClient:
    def __init__(self, exc):
        self._exc = exc
        self.models = self

    def generate_content(self, **kwargs):
        raise self._exc


def _gemini_backend(responses, *, max_tokens=4096, model="gemini-test", tools=None):
    b = GeminiLLMBackend.__new__(GeminiLLMBackend)
    b._model = model
    b._max_tokens = max_tokens
    b._tools = tools or []
    b._client = _GeminiClient(responses)
    return b


def _gum(prompt=10, candidates=2, cached=0):
    return SimpleNamespace(
        prompt_token_count=prompt,
        candidates_token_count=candidates,
        cached_content_token_count=cached,
    )


def _text_resp(text="ok", finish="STOP", usage=None):
    part = SimpleNamespace(text=text, function_call=None)
    candidate = SimpleNamespace(
        content=SimpleNamespace(parts=[part]),
        finish_reason=SimpleNamespace(name=finish),
    )
    return SimpleNamespace(candidates=[candidate], usage_metadata=usage or _gum())


def _tool_resp(finish="STOP", thinking="let me check", usage=None):
    parts = []
    if thinking is not None:
        parts.append(SimpleNamespace(text=thinking, function_call=None))
    parts.append(
        SimpleNamespace(
            text=None,
            function_call=SimpleNamespace(name="read_file", args={"path": "x.txt"}),
        )
    )
    candidate = SimpleNamespace(
        content=SimpleNamespace(parts=parts),
        finish_reason=SimpleNamespace(name=finish),
    )
    return SimpleNamespace(candidates=[candidate], usage_metadata=usage or _gum())


# --------------------------------------------------------------------------- #
# set_tools schema sanitization (no SDK needed -- pure dict work)
# --------------------------------------------------------------------------- #
def test_set_tools_strips_gemini_unsupported_schema_keys():
    b = GeminiLLMBackend.__new__(GeminiLLMBackend)
    b.set_tools([
        {
            "name": "read_file",
            "description": "read a file",
            "input_schema": {
                "type": "object",
                "$schema": "http://json-schema.org/draft-07/schema#",
                "additionalProperties": False,
                "title": "ReadFile",
                "properties": {"path": {"type": "string", "title": "Path", "default": "x"}},
            },
        }
    ])
    params = b._tools[0]["parameters"]
    assert "$schema" not in params
    assert "additionalProperties" not in params
    assert "title" not in params
    # nested objects are sanitized too
    assert "title" not in params["properties"]["path"]
    assert "default" not in params["properties"]["path"]
    assert params["properties"]["path"]["type"] == "string"
    assert params["type"] == "object"


def test_set_tools_defaults_empty_schema_to_object():
    b = GeminiLLMBackend.__new__(GeminiLLMBackend)
    b.set_tools([{"name": "noop", "description": "d"}])
    assert b._tools[0]["parameters"] == {"type": "object"}


# --------------------------------------------------------------------------- #
# Outbound translation + id->name function_response mapping
# --------------------------------------------------------------------------- #
def test_outbound_translation_resolves_function_response_name_by_id(monkeypatch):
    _install_fake_genai(monkeypatch)
    b = _gemini_backend([])
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "checking"},
            {"type": "tool_use", "name": "read_file", "input": {"path": "x"}, "id": "tu_1"},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "file contents"},
        ]},
    ]
    contents = b._to_gemini_contents(messages)
    assert contents[0].role == "user"
    assert contents[0].parts[0].text == "go"
    # assistant -> model, text + function_call parts
    assert contents[1].role == "model"
    assert contents[1].parts[0].text == "checking"
    assert contents[1].parts[1].function_call.name == "read_file"
    assert contents[1].parts[1].function_call.args == {"path": "x"}
    # tool_result -> function_response tagged with the NAME, looked up by id
    fr = contents[2].parts[0].function_response
    assert contents[2].role == "user"
    assert fr.name == "read_file"
    assert fr.response == {"result": "file contents"}


def test_outbound_dict_tool_result_passes_through_as_response(monkeypatch):
    _install_fake_genai(monkeypatch)
    b = _gemini_backend([])
    messages = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "name": "lookup", "input": {}, "id": "tu_9"},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_9", "content": {"rows": 3}},
        ]},
    ]
    contents = b._to_gemini_contents(messages)
    fr = contents[1].parts[0].function_response
    assert fr.name == "lookup"
    assert fr.response == {"rows": 3}


# --------------------------------------------------------------------------- #
# step(): system routing, response parsing, usage
# --------------------------------------------------------------------------- #
def test_step_routes_system_prompt_to_system_instruction(monkeypatch):
    _install_fake_genai(monkeypatch)
    b = _gemini_backend([_text_resp(text="hi")])
    b.step("YOU ARE FABRI", [{"role": "user", "content": "hi"}])
    cfg = b._client.calls[0]["config"]
    assert cfg.system_instruction == "YOU ARE FABRI"
    # system is NOT folded into contents -- the first content is the user turn
    contents = b._client.calls[0]["contents"]
    assert contents[0].role == "user"


def test_step_text_response_parsing_and_usage(monkeypatch):
    _install_fake_genai(monkeypatch)
    b = _gemini_backend(
        [_text_resp(text="done", usage=_gum(prompt=12, candidates=4, cached=3))],
        model="gemini-2.5-flash",
    )
    resp = b.step("sys", [{"role": "user", "content": "go"}])
    assert resp.final_text == "done"
    assert resp.tool_calls == []
    assert resp.usage.model == "gemini-2.5-flash"
    assert resp.usage.input_tokens == 12
    assert resp.usage.output_tokens == 4
    assert resp.usage.cache_read_input_tokens == 3
    assert len(b._client.calls) == 1


def test_step_tool_call_parsing_synthesizes_id_and_captures_thinking(monkeypatch):
    _install_fake_genai(monkeypatch)
    b = _gemini_backend([_tool_resp(thinking="let me check existing files")])
    resp = b.step("sys", [{"role": "user", "content": "go"}])
    assert resp.final_text is None
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.name == "read_file"
    assert tc.args == {"path": "x.txt"}
    assert tc.id  # Gemini gives no id; backend synthesizes a non-empty one
    assert resp.thinking_text == "let me check existing files"


def test_step_tool_call_without_text_has_no_thinking_text(monkeypatch):
    _install_fake_genai(monkeypatch)
    b = _gemini_backend([_tool_resp(thinking=None)])
    resp = b.step("sys", [{"role": "user", "content": "go"}])
    assert len(resp.tool_calls) == 1
    assert resp.thinking_text is None


# --------------------------------------------------------------------------- #
# MAX_TOKENS retry parity
# --------------------------------------------------------------------------- #
def test_step_max_tokens_retries_once_then_succeeds(monkeypatch):
    _install_fake_genai(monkeypatch)
    b = _gemini_backend(
        [
            _text_resp(finish="MAX_TOKENS", usage=_gum(prompt=10, candidates=4096)),
            _text_resp(finish="STOP", text="done", usage=_gum(prompt=10, candidates=50)),
        ],
        max_tokens=4096,
    )
    resp = b.step("sys", [{"role": "user", "content": "go"}])
    assert len(b._client.calls) == 2
    assert b._client.calls[0]["config"].max_output_tokens == 4096
    assert b._client.calls[1]["config"].max_output_tokens == 8192  # 2x, under ceiling
    assert resp.final_text == "done"
    # discarded truncated attempt's tokens fold into reported usage
    assert resp.usage.output_tokens == 4096 + 50
    assert resp.usage.input_tokens == 10 + 10


def test_step_max_tokens_twice_fails_loud(monkeypatch):
    _install_fake_genai(monkeypatch)
    b = _gemini_backend(
        [_text_resp(finish="MAX_TOKENS"), _text_resp(finish="MAX_TOKENS")],
        max_tokens=4096,
    )
    with pytest.raises(LLMError, match="even after retry"):
        b.step("sys", [{"role": "user", "content": "go"}])
    assert len(b._client.calls) == 2  # retried exactly once, then gave up


def test_step_no_truncation_is_a_single_call(monkeypatch):
    _install_fake_genai(monkeypatch)
    b = _gemini_backend([_text_resp(finish="STOP", usage=_gum(candidates=20))])
    resp = b.step("sys", [{"role": "user", "content": "go"}])
    assert len(b._client.calls) == 1
    assert resp.usage.output_tokens == 20


# --------------------------------------------------------------------------- #
# Error wrapping
# --------------------------------------------------------------------------- #
def test_step_api_error_wrapped_in_llmerror(monkeypatch):
    errors_mod = _install_fake_genai(monkeypatch)
    b = GeminiLLMBackend.__new__(GeminiLLMBackend)
    b._model = "gemini-test"
    b._max_tokens = 1024
    b._tools = []
    b._client = _RaisingClient(errors_mod.APIError("boom"))
    with pytest.raises(LLMError, match="gemini API error"):
        b.step("sys", [{"role": "user", "content": "go"}])


# --------------------------------------------------------------------------- #
# Parallel tool calls in one turn
# --------------------------------------------------------------------------- #
def test_step_parallel_function_calls_yield_distinct_tool_calls(monkeypatch):
    """A single Gemini turn can carry several function_call parts (parallel tool
    use). All must round-trip, in order, each with its own synthesized id so the
    agent loop can build a matching tool_result per call."""
    _install_fake_genai(monkeypatch)
    parts = [
        SimpleNamespace(text="reading two files", function_call=None),
        SimpleNamespace(text=None, function_call=SimpleNamespace(name="read_file", args={"path": "a"})),
        SimpleNamespace(text=None, function_call=SimpleNamespace(name="read_file", args={"path": "b"})),
    ]
    candidate = SimpleNamespace(
        content=SimpleNamespace(parts=parts),
        finish_reason=SimpleNamespace(name="STOP"),
    )
    resp = SimpleNamespace(candidates=[candidate], usage_metadata=_gum())
    b = _gemini_backend([resp])
    out = b.step("sys", [{"role": "user", "content": "go"}])
    assert [c.name for c in out.tool_calls] == ["read_file", "read_file"]
    assert [c.args["path"] for c in out.tool_calls] == ["a", "b"]
    ids = [c.id for c in out.tool_calls]
    assert all(ids) and len(set(ids)) == 2  # distinct, non-empty
    assert out.final_text is None
    assert out.thinking_text == "reading two files"


# --------------------------------------------------------------------------- #
# Multi-turn id->name mapping across several assistant turns
# --------------------------------------------------------------------------- #
def test_outbound_id_name_map_spans_multiple_assistant_turns(monkeypatch):
    """Two assistant turns call two different tools; each later tool_result must
    resolve to the right function name even though only ids appear on the
    tool_result blocks."""
    _install_fake_genai(monkeypatch)
    b = _gemini_backend([])
    messages = [
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "name": "read_file", "input": {"path": "a"}, "id": "tu_1"},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "A"},
        ]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "name": "list_dir", "input": {"path": "."}, "id": "tu_2"},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_2", "content": "B"},
        ]},
    ]
    contents = b._to_gemini_contents(messages)
    first_fr = contents[2].parts[0].function_response
    second_fr = contents[4].parts[0].function_response
    assert first_fr.name == "read_file"
    assert second_fr.name == "list_dir"


def test_outbound_assistant_string_content_maps_to_model_text(monkeypatch):
    _install_fake_genai(monkeypatch)
    b = _gemini_backend([])
    contents = b._to_gemini_contents([{"role": "assistant", "content": "plain answer"}])
    assert contents[0].role == "model"
    assert contents[0].parts[0].text == "plain answer"


def test_outbound_list_tool_result_is_json_wrapped(monkeypatch):
    """A non-str / non-dict tool_result payload is JSON-serialized under a
    `result` key so FunctionResponse.response is always a dict."""
    _install_fake_genai(monkeypatch)
    b = _gemini_backend([])
    messages = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "name": "grep", "input": {}, "id": "tu_5"},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_5", "content": ["hit1", "hit2"]},
        ]},
    ]
    contents = b._to_gemini_contents(messages)
    resp = contents[1].parts[0].function_response.response
    assert resp == {"result": '["hit1", "hit2"]'}


def test_outbound_non_tool_result_user_block_becomes_text(monkeypatch):
    """A plain text block inside a user turn (not a tool_result) survives as a
    text part rather than being dropped."""
    _install_fake_genai(monkeypatch)
    b = _gemini_backend([])
    contents = b._to_gemini_contents([
        {"role": "user", "content": [{"type": "text", "text": "follow-up question"}]},
    ])
    assert contents[0].role == "user"
    assert contents[0].parts[0].text == "follow-up question"


# --------------------------------------------------------------------------- #
# Tool wiring: set_tools order + _build_tools SDK shape
# --------------------------------------------------------------------------- #
def test_set_tools_preserves_order_and_names():
    b = GeminiLLMBackend.__new__(GeminiLLMBackend)
    b.set_tools([
        {"name": "read_file", "description": "r", "input_schema": {"type": "object"}},
        {"name": "write_file", "description": "w", "input_schema": {"type": "object"}},
    ])
    assert [t["name"] for t in b._tools] == ["read_file", "write_file"]


def test_build_tools_emits_single_tool_with_all_declarations(monkeypatch):
    _install_fake_genai(monkeypatch)
    b = GeminiLLMBackend.__new__(GeminiLLMBackend)
    b.set_tools([
        {"name": "read_file", "description": "r", "input_schema": {"type": "object"}},
        {"name": "write_file", "description": "w", "input_schema": {"type": "object"}},
    ])
    tools = b._build_tools()
    assert len(tools) == 1
    decls = tools[0].function_declarations
    assert [d.name for d in decls] == ["read_file", "write_file"]
    assert decls[0].description == "r"


def test_build_tools_is_none_when_no_tools(monkeypatch):
    _install_fake_genai(monkeypatch)
    b = _gemini_backend([])
    assert b._build_tools() is None


def test_step_passes_tools_into_config(monkeypatch):
    _install_fake_genai(monkeypatch)
    b = _gemini_backend([_text_resp()])
    b.set_tools([{"name": "read_file", "description": "r", "input_schema": {"type": "object"}}])
    b.step("sys", [{"role": "user", "content": "go"}])
    cfg = b._client.calls[0]["config"]
    assert cfg.tools is not None
    assert cfg.max_output_tokens == b._max_tokens


# --------------------------------------------------------------------------- #
# finish_reason normalization (enum-like vs plain string)
# --------------------------------------------------------------------------- #
def test_finish_reason_plain_string_normalizes():
    candidate = SimpleNamespace(finish_reason="STOP")
    resp = SimpleNamespace(candidates=[candidate])
    assert GeminiLLMBackend._finish_reason(resp) == "STOP"


def test_finish_reason_enum_like_uses_name():
    candidate = SimpleNamespace(finish_reason=SimpleNamespace(name="MAX_TOKENS"))
    resp = SimpleNamespace(candidates=[candidate])
    assert GeminiLLMBackend._finish_reason(resp) == "MAX_TOKENS"


def test_finish_reason_absent_is_none():
    resp = SimpleNamespace(candidates=[SimpleNamespace(finish_reason=None)])
    assert GeminiLLMBackend._finish_reason(resp) is None


# --------------------------------------------------------------------------- #
# Transient ServerError handling via _call_with_retry
# --------------------------------------------------------------------------- #
def test_step_transient_server_error_is_retried_then_succeeds(monkeypatch):
    errors_mod = _install_fake_genai(monkeypatch)
    monkeypatch.setattr("fabri.core.llm.time.sleep", lambda *a, **k: None)
    b = _gemini_backend([errors_mod.ServerError("503"), _text_resp(text="recovered")])
    resp = b.step("sys", [{"role": "user", "content": "go"}])
    assert resp.final_text == "recovered"
    assert len(b._client.calls) == 2  # one failed attempt, one success


def test_step_transient_server_error_exhausts_retries_to_llmerror(monkeypatch):
    errors_mod = _install_fake_genai(monkeypatch)
    monkeypatch.setattr("fabri.core.llm.time.sleep", lambda *a, **k: None)
    b = _gemini_backend([errors_mod.ServerError("x") for _ in range(3)])
    with pytest.raises(LLMError, match="failed after 3 attempts"):
        b.step("sys", [{"role": "user", "content": "go"}])
    assert len(b._client.calls) == 3


# --------------------------------------------------------------------------- #
# Empty response (no parts) -> a None final answer, not a crash
# --------------------------------------------------------------------------- #
def test_step_empty_candidate_parts_yields_none_final_text(monkeypatch):
    _install_fake_genai(monkeypatch)
    candidate = SimpleNamespace(
        content=SimpleNamespace(parts=[]),
        finish_reason=SimpleNamespace(name="STOP"),
    )
    resp = SimpleNamespace(candidates=[candidate], usage_metadata=_gum())
    b = _gemini_backend([resp])
    out = b.step("sys", [{"role": "user", "content": "go"}])
    assert out.tool_calls == []
    assert out.final_text is None
    assert out.usage.model == b._model
