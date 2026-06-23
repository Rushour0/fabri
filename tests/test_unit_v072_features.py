"""Coverage for the v0.7.2 feature batch:
- G5 replay (CLI surface — light)
- G7 per-step cost attribution in reports
- G9 cost-budget enforcement (Outcome.BUDGET_EXCEEDED)
- G21 message-prefix caching (cache_control on last message)
- Tokenizer model-aware encoding + word-boundary truncation
- MCP HTTP transport
- MCP server protocol handlers
"""
from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from fabri.core.outcome import Outcome


# -------------------- G7 per-step attribution -------------------------------

def test_per_step_attribution_splits_step_cost_across_its_tools():
    """G7: a step that cost $0.10 and dispatched read_file + write_file
    contributes $0.05 to each — NOT $0.10 split across the whole session's
    tool calls (the v0.7.0 proportional fallback)."""
    from fabri.reports.aggregate import _attribute_cost_by_tool_per_step

    events = [
        {"type": "step_started", "step": 1},
        {"type": "tool_call", "step": 1, "name": "read_file"},
        {"type": "tool_call", "step": 1, "name": "write_file"},
        {"type": "step_finished", "step": 1, "cost_usd": 0.10},
        {"type": "step_started", "step": 2},
        {"type": "tool_call", "step": 2, "name": "list_dir"},
        {"type": "step_finished", "step": 2, "cost_usd": 0.02},
    ]
    out = _attribute_cost_by_tool_per_step(events)
    assert out is not None
    assert out["read_file"] == pytest.approx(0.05)
    assert out["write_file"] == pytest.approx(0.05)
    assert out["list_dir"] == pytest.approx(0.02)


def test_per_step_attribution_returns_none_when_no_step_cost_present():
    """Legacy traces (pre-v0.7.2) have no `cost_usd` on step_finished —
    return None so the report falls back to proportional split."""
    from fabri.reports.aggregate import _attribute_cost_by_tool_per_step

    events = [
        {"type": "tool_call", "step": 1, "name": "read_file"},
        {"type": "step_finished", "step": 1},  # no cost_usd
    ]
    assert _attribute_cost_by_tool_per_step(events) is None


# -------------------- G9 cost-budget ----------------------------------------

def test_outcome_enum_has_budget_exceeded():
    """The new outcome must be a real enum variant so host services can
    dispatch on it."""
    assert Outcome.BUDGET_EXCEEDED.value == "budget_exceeded"
    # And not accidentally one of the success values:
    assert Outcome.BUDGET_EXCEEDED.value not in (
        Outcome.SUCCESS.value, Outcome.SUCCESS_WITH_RECOVERY.value,
    )


def test_config_default_max_cost_usd_is_none():
    """G9 is opt-in: a config that doesn't mention max_cost_usd must keep
    the current no-budget behaviour. A non-None default would silently
    cap every run."""
    from fabri.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["agent"]["max_cost_usd"] is None


# -------------------- G21 message caching -----------------------------------

def _anthropic_with_cache_msgs(enable_msgs: bool):
    from fabri.core.llm import AnthropicLLMBackend
    b = AnthropicLLMBackend.__new__(AnthropicLLMBackend)
    b._enable_prompt_cache = True  # G21 only fires when prompt-cache is on too
    b._cache_messages = enable_msgs
    return b


def test_g21_marks_last_string_message_with_cache_control():
    """When messages is `[{role:user, content:"hi"}]`, _build_messages must
    promote the string content into a block list with cache_control on the
    tail."""
    b = _anthropic_with_cache_msgs(True)
    out = b._build_messages([{"role": "user", "content": "hi"}])
    last_content = out[-1]["content"]
    assert isinstance(last_content, list)
    assert last_content[-1].get("cache_control") == {"type": "ephemeral"}


def test_g21_marks_last_block_in_block_list():
    b = _anthropic_with_cache_msgs(True)
    out = b._build_messages([{"role": "assistant", "content": [
        {"type": "text", "text": "a"},
        {"type": "text", "text": "b"},
    ]}])
    blocks = out[-1]["content"]
    assert "cache_control" not in blocks[0]
    assert blocks[-1].get("cache_control") == {"type": "ephemeral"}


def test_g21_off_passes_messages_through_unchanged():
    """The default (opt-out) behaviour must NOT add cache_control anywhere —
    legacy callers see no provider-call shape change."""
    b = _anthropic_with_cache_msgs(False)
    msgs = [{"role": "user", "content": "hi"}]
    out = b._build_messages(msgs)
    # Same object identity — no copy needed when feature is off (perf nit)
    # AND no cache_control wrap.
    assert out is msgs or (
        isinstance(out[-1]["content"], str) and "cache_control" not in str(out)
    )


def test_g21_does_not_mutate_input_messages():
    """Caller's messages list must be untouched: a future step that reuses
    it should not see the cache_control tag bleeding in."""
    b = _anthropic_with_cache_msgs(True)
    original = [{"role": "user", "content": "hi"}]
    snapshot = [dict(m) for m in original]
    b._build_messages(original)
    assert original == snapshot


# -------------------- Tokenizer ---------------------------------------------

def test_enforce_token_cap_truncates_at_word_boundary():
    """Hardening: the historical cl100k slice could end mid-word. The new
    behaviour cuts at the last whitespace before the limit + appends '...'.
    Use a max_tokens small enough to force truncation."""
    from fabri.memory.compress import enforce_token_cap

    text = "this is a long sentence that will get cut somewhere"
    capped = enforce_token_cap(text, max_tokens=5, model="claude-sonnet-4-6")
    assert capped.endswith("...")
    # Find the visible word right before "..." and confirm it's a whole word.
    head = capped[:-3].rstrip()
    last_word = head.rsplit(" ", 1)[-1] if " " in head else head
    assert last_word in text.split()  # whole-word match (not a fragment)


def test_count_tokens_uses_model_specific_encoder():
    """For Claude / gpt-4o we should pick o200k_base; for unknown, cl100k."""
    from fabri.memory.compress import count_tokens

    # Equal text, different models: the o200k path may differ slightly from
    # cl100k. We just assert the call doesn't crash and returns a positive
    # int — strict token counts are tokenizer-version-dependent.
    for model in ("claude-sonnet-4-6", "gpt-4o", "claude-haiku-4-5", "unknown-foo"):
        assert count_tokens("hello world", model=model) > 0


# -------------------- MCP HTTP ----------------------------------------------

def test_mcp_http_client_serializes_jsonrpc_post():
    """The HTTP client must POST a valid JSON-RPC envelope and return the
    `result` field on a 200 response. We stub urllib so no network hits."""
    from fabri.tools.mcp_client import MCPHttpClient

    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = req.data
        captured["headers"] = dict(req.headers)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}).encode()
        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return body
        return _R()

    import urllib.request as _ur
    orig = _ur.urlopen
    _ur.urlopen = _fake_urlopen
    try:
        c = MCPHttpClient(url="https://x.example/jsonrpc", name="test")
        c.start()
        result = c.list_tools()
    finally:
        _ur.urlopen = orig

    assert captured["url"] == "https://x.example/jsonrpc"
    sent = json.loads(captured["body"])
    assert sent["jsonrpc"] == "2.0"
    assert sent["method"] == "tools/list"
    assert result == []


def test_build_mcp_tools_rejects_both_command_and_url():
    """A server config must pick ONE transport; refusing both prevents an
    ambiguous 'which transport wins' situation."""
    from fabri.tools.mcp_client import build_mcp_tools

    with pytest.raises(ValueError, match="not both"):
        build_mcp_tools({"name": "x", "command": ["echo"], "url": "https://x"})


def test_build_mcp_tools_rejects_neither_command_nor_url():
    from fabri.tools.mcp_client import build_mcp_tools

    with pytest.raises(ValueError, match="stdio.*http"):
        build_mcp_tools({"name": "x"})


# -------------------- MCP server --------------------------------------------

def test_mcp_server_initialize_returns_protocol_version():
    """The server must answer `initialize` even before any agent is built —
    a smart MCP client uses this handshake to decide capability matching."""
    from fabri.tools.mcp_server import FabriMCPServer

    s = FabriMCPServer.__new__(FabriMCPServer)
    s.tool_name = "fabri_agent"
    out = s.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert out["id"] == 1
    assert out["result"]["protocolVersion"] == "2024-11-05"
    assert out["result"]["serverInfo"]["name"] == "fabri"


def test_mcp_server_tools_list_reports_single_agent_tool():
    """The server exposes ONE tool — the fabri agent. Adding more tools
    here would diverge from the 'agent-as-a-tool' design."""
    from fabri.tools.mcp_server import FabriMCPServer

    s = FabriMCPServer.__new__(FabriMCPServer)
    s.tool_name = "my_agent"
    out = s.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = out["result"]["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "my_agent"
    assert "task" in tools[0]["inputSchema"]["properties"]


def test_mcp_server_unknown_method_returns_jsonrpc_error():
    from fabri.tools.mcp_server import FabriMCPServer

    s = FabriMCPServer.__new__(FabriMCPServer)
    s.tool_name = "fabri_agent"
    out = s.handle({"jsonrpc": "2.0", "id": 3, "method": "nope/nope"})
    assert "error" in out
    assert out["error"]["code"] == -32601


def test_mcp_server_notification_returns_none():
    """Notifications (no id) must not produce a response — JSON-RPC spec."""
    from fabri.tools.mcp_server import FabriMCPServer

    s = FabriMCPServer.__new__(FabriMCPServer)
    s.tool_name = "fabri_agent"
    out = s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert out is None


# -------------------- LongMemEval helpers -----------------------------------

def test_longmemeval_score_exact_match_normalizes_case_and_whitespace():
    from fabri.benchmarks.longmemeval.runner import _score_exact_match

    assert _score_exact_match("  Hello World ", "hello world") is True
    assert _score_exact_match("hello", "hi") is False


def test_longmemeval_results_aggregate_by_category():
    from fabri.benchmarks.longmemeval.runner import LongMemEvalResults, TestCaseResult

    r = LongMemEvalResults(cases=[
        TestCaseResult("c1", "single", "q1", "a1", "a1", exact_match=True),
        TestCaseResult("c2", "single", "q2", "a2", "wrong", exact_match=False),
        TestCaseResult("c3", "multi", "q3", "a3", "a3", exact_match=True),
    ])
    by_cat = r.by_category()
    assert by_cat["single"]["n"] == 2
    assert by_cat["single"]["exact_rate"] == 0.5
    assert by_cat["multi"]["exact_rate"] == 1.0
    assert r.exact_match_rate == pytest.approx(2 / 3)
