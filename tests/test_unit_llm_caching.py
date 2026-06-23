"""Verifies the Anthropic prompt-caching wrapping in AnthropicLLMBackend.

The point of this test is the *shape* of the call we make into the SDK, not
its return value: a regression here silently turns the system prefix into
non-cached billable tokens every step, which is the exact thing the
v0.3.0 change is meant to prevent.
"""
from types import SimpleNamespace

import pytest

from fabri.core.llm import AnthropicLLMBackend


class _StubClient:
    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []
        self.messages = self  # so `.messages.create(...)` works

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


@pytest.fixture
def stub_response():
    # One text block, no tool_use; a `usage` with the cache fields so the
    # logging branch in step() is exercised.
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=2,
            cache_creation_input_tokens=8,
            cache_read_input_tokens=0,
        ),
    )


def _make_backend(enable: bool, tools=None):
    # Construct without going through __init__'s real client/env setup.
    b = AnthropicLLMBackend.__new__(AnthropicLLMBackend)
    b._model = "claude-test"
    b._max_tokens = 32
    b._tools = tools or []
    b._enable_prompt_cache = enable
    b._cache_messages = False  # G21: tests don't exercise message caching here
    return b


def test_system_block_wrapped_with_cache_control_when_enabled(stub_response):
    b = _make_backend(enable=True)
    b._client = _StubClient(stub_response)
    b.step("you are an agent", [{"role": "user", "content": "hi"}])
    sent = b._client.calls[0]["system"]
    assert isinstance(sent, list) and len(sent) == 1
    assert sent[0]["type"] == "text"
    assert sent[0]["text"] == "you are an agent"
    assert sent[0]["cache_control"] == {"type": "ephemeral"}


def test_system_block_passes_through_when_cache_disabled(stub_response):
    b = _make_backend(enable=False)
    b._client = _StubClient(stub_response)
    b.step("you are an agent", [{"role": "user", "content": "hi"}])
    assert b._client.calls[0]["system"] == "you are an agent"


def test_last_tool_tagged_when_enabled(stub_response):
    tools = [{"name": "a", "description": "A", "input_schema": {}},
             {"name": "b", "description": "B", "input_schema": {}}]
    b = _make_backend(enable=True, tools=tools)
    b._client = _StubClient(stub_response)
    b.step("sys", [{"role": "user", "content": "hi"}])
    sent_tools = b._client.calls[0]["tools"]
    # First tool untouched; the last carries the cache marker.
    assert "cache_control" not in sent_tools[0]
    assert sent_tools[1]["cache_control"] == {"type": "ephemeral"}
    # Original list is not mutated (would corrupt subsequent steps).
    assert "cache_control" not in tools[-1]


def test_no_tools_does_not_crash(stub_response):
    b = _make_backend(enable=True, tools=[])
    b._client = _StubClient(stub_response)
    b.step("sys", [{"role": "user", "content": "hi"}])
    assert b._client.calls[0]["tools"] == []
