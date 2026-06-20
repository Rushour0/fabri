"""Unit tests for the inline decompose pseudo-tool. It's a single extra LLM
step that returns the standard {ok, result?, error?} tool shape, not a
sub-agent -- the tests pin that contract so a future change to the prompt
or parsing doesn't silently break the agent loop's tool dispatch."""
import pytest

from fabri.core.decompose import DEFAULT_MAX_SUBQUESTIONS, decompose
from fabri.core.llm import LLMResponse, ScriptedLLMBackend


def test_decompose_returns_subquestions_on_valid_json():
    llm = ScriptedLLMBackend([LLMResponse(final_text='["q1", "q2", "q3"]')])
    result = decompose(llm, "research X")
    assert result == {"ok": True, "result": {"subquestions": ["q1", "q2", "q3"]}}


def test_decompose_truncates_to_max_subquestions():
    llm = ScriptedLLMBackend([LLMResponse(final_text='["a","b","c","d","e","f","g"]')])
    result = decompose(llm, "x", max_subquestions=3)
    assert result["result"]["subquestions"] == ["a", "b", "c"]


def test_decompose_default_max_is_five():
    llm = ScriptedLLMBackend([LLMResponse(final_text='["1","2","3","4","5","6","7"]')])
    result = decompose(llm, "x")
    assert len(result["result"]["subquestions"]) == DEFAULT_MAX_SUBQUESTIONS


def test_decompose_malformed_json_is_normalized_failure():
    llm = ScriptedLLMBackend([LLMResponse(final_text="not json at all")])
    result = decompose(llm, "x")
    assert result["ok"] is False
    assert "malformed" in result["error"]


def test_decompose_non_list_json_is_failure():
    llm = ScriptedLLMBackend([LLMResponse(final_text='{"q": "x"}')])
    result = decompose(llm, "x")
    assert result["ok"] is False


def test_decompose_empty_response_is_failure():
    llm = ScriptedLLMBackend([LLMResponse(final_text="")])
    result = decompose(llm, "x")
    assert result["ok"] is False


def test_decompose_strips_whitespace_around_json():
    llm = ScriptedLLMBackend([LLMResponse(final_text='  \n["a"]\n  ')])
    result = decompose(llm, "x")
    assert result["ok"] is True
    assert result["result"]["subquestions"] == ["a"]


def test_decompose_accepts_toon_output_when_opted_in():
    llm = ScriptedLLMBackend([LLMResponse(final_text="[3]: q1,q2,q3")])
    result = decompose(llm, "x", output_format="toon")
    assert result == {"ok": True, "result": {"subquestions": ["q1", "q2", "q3"]}}


def test_decompose_toon_mode_still_accepts_json_fallback():
    # reliability: a model that ignores the TOON instruction and answers JSON works
    llm = ScriptedLLMBackend([LLMResponse(final_text='["a", "b"]')])
    result = decompose(llm, "x", output_format="toon")
    assert result["ok"] is True
    assert result["result"]["subquestions"] == ["a", "b"]
