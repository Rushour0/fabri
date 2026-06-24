"""End-to-end tests for O1 structured output through run_agent, driven by a
ScriptedLLMBackend (no live LLM). Exercises the validate-then-retry path, the
three error strategies, and the new Outcome.INVALID_OUTPUT. A live Qdrant is
required, matching the rest of the e2e suite."""
import os
import uuid
from pathlib import Path

from fabri import QdrantMemoryStore, ScriptedLLMBackend, ToolRegistry, run_agent
from fabri.core.llm import LLMResponse
from fabri.core.outcome import Outcome
from fabri.events import EventType
from fabri.orchestrator.traces import read_trace

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "src" / "fabri" / "tools" / "examples"

SCHEMA = {
    "type": "object",
    "required": ["answer", "confidence"],
    "properties": {"answer": {"type": "string"}, "confidence": {"type": "number"}},
}


def _tools(tmp_path: Path) -> ToolRegistry:
    os.environ["FABRI_SANDBOX_ROOT"] = str(tmp_path)
    return ToolRegistry(EXAMPLES_DIR)


def _store() -> QdrantMemoryStore:
    return QdrantMemoryStore(collection=f"so_{uuid.uuid4().hex[:10]}")


def _structured_events(session_id: str) -> list[dict]:
    return [e for e in read_trace(session_id)
            if e.get("type") == EventType.STRUCTURED_OUTPUT.value]


def test_valid_first_try(tmp_path):
    script = [LLMResponse(final_text='{"answer": "42", "confidence": 0.9}')]
    out = run_agent(
        "q", ScriptedLLMBackend(script), _tools(tmp_path), _store(),
        response_schema=SCHEMA,
    )
    assert out["success"] is True
    assert out["outcome"] == Outcome.SUCCESS.value
    assert out["structured_output"] == {"answer": "42", "confidence": 0.9}
    evs = _structured_events(out["session_id"])
    assert len(evs) == 1 and evs[0]["valid"] is True


def test_invalid_then_valid_after_one_retry(tmp_path):
    # First answer is missing `confidence`; the loop re-prompts and the model
    # corrects on the retry.
    script = [
        LLMResponse(final_text='{"answer": "42"}'),
        LLMResponse(final_text='{"answer": "42", "confidence": 0.8}'),
    ]
    out = run_agent(
        "q", ScriptedLLMBackend(script), _tools(tmp_path), _store(),
        response_schema=SCHEMA, response_retries=1,
    )
    assert out["success"] is True
    assert out["structured_output"] == {"answer": "42", "confidence": 0.8}
    evs = _structured_events(out["session_id"])
    assert [e["valid"] for e in evs] == [False, True]
    assert evs[0]["attempt"] == 0 and evs[1]["attempt"] == 1


def test_strict_fails_after_retries(tmp_path):
    script = [
        LLMResponse(final_text='{"answer": "42"}'),
        LLMResponse(final_text='still missing confidence'),
    ]
    out = run_agent(
        "q", ScriptedLLMBackend(script), _tools(tmp_path), _store(),
        response_schema=SCHEMA, response_retries=1, error_strategy="strict",
    )
    assert out["success"] is False
    assert out["outcome"] == Outcome.INVALID_OUTPUT.value
    assert out["structured_output"] is None


def test_strict_zero_retries_fails_immediately(tmp_path):
    script = [LLMResponse(final_text='{"answer": "42"}')]
    out = run_agent(
        "q", ScriptedLLMBackend(script), _tools(tmp_path), _store(),
        response_schema=SCHEMA, response_retries=0,
    )
    assert out["outcome"] == Outcome.INVALID_OUTPUT.value
    assert len(_structured_events(out["session_id"])) == 1


def test_warn_returns_unvalidated_text_as_success(tmp_path):
    script = [LLMResponse(final_text='{"answer": "42"}')]
    out = run_agent(
        "q", ScriptedLLMBackend(script), _tools(tmp_path), _store(),
        response_schema=SCHEMA, response_retries=0, error_strategy="warn",
    )
    assert out["success"] is True
    assert out["outcome"] == Outcome.SUCCESS.value
    assert out["final_text"] == '{"answer": "42"}'


def test_fallback_substitutes_configured_value(tmp_path):
    fallback = {"answer": "unknown", "confidence": 0.0}
    script = [LLMResponse(final_text="not even json")]
    out = run_agent(
        "q", ScriptedLLMBackend(script), _tools(tmp_path), _store(),
        response_schema=SCHEMA, response_retries=0,
        error_strategy="fallback", response_fallback=fallback,
    )
    assert out["success"] is True
    assert out["structured_output"] == fallback


def test_no_schema_is_unchanged_passthrough(tmp_path):
    # No response_schema -> free-text answer, no structured_output, no events.
    script = [LLMResponse(final_text="just a plain answer")]
    out = run_agent("q", ScriptedLLMBackend(script), _tools(tmp_path), _store())
    assert out["success"] is True
    assert out["final_text"] == "just a plain answer"
    assert out["structured_output"] is None
    assert _structured_events(out["session_id"]) == []
