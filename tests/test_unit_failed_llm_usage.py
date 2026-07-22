from __future__ import annotations

import pytest

from fabri import ToolRegistry, run_agent
from fabri.core.llm import LLMError, LLMUsage
from fabri.orchestrator.retrieval import RetrievalConfig

pytestmark = pytest.mark.unit


class _UnusedStore:
    """Retrieval is disabled; touching the store would be a test failure."""

    def count(self) -> int:
        raise AssertionError("disabled retrieval must not access memory")


class _TruncatedBackend:
    def set_tools(self, _tool_definitions: list[dict]) -> None:
        return None

    def step(self, _system: str, _messages: list[dict]) -> object:
        raise LLMError(
            "response truncated after retry",
            usage=LLMUsage(
                input_tokens=200,
                output_tokens=384,
                max_token_retries=1,
                model="test-model",
            ),
        )


def test_failed_llm_retry_remains_visible_in_run_usage() -> None:
    result = run_agent(
        "write a detailed brief",
        _TruncatedBackend(),
        ToolRegistry([]),
        _UnusedStore(),
        retrieval_config=RetrievalConfig(retrieval_enabled=False),
    )

    assert result["outcome"] == "failed"
    assert result["usage"]["input_tokens"] == 200
    assert result["usage"]["output_tokens"] == 384
    assert result["usage"]["max_token_retries"] == 1
