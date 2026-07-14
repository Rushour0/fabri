from enum import Enum


class Outcome(str, Enum):
    SUCCESS = "success"  # final text produced, no tool failures in the run
    SUCCESS_WITH_RECOVERY = "success_with_recovery"  # final text produced, but >=1 tool call failed along the way
    INCOMPLETE = "incomplete"  # hit MAX_STEPS with no final text, no tool failures
    INCOMPLETE_WITH_TOOL_FAILURE = "incomplete_with_tool_failure"  # hit MAX_STEPS AND >=1 tool call failed -- the run is more likely "every tool failed" than "out of steps"
    BUDGET_EXCEEDED = "budget_exceeded"  # G9: agent.max_cost_usd was set and the run's COGS hit it before a final answer
    FAILED = "failed"  # unrecoverable LLM error: API/rate-limit failure or a truncated response (see core.llm.LLMError)
    INVALID_OUTPUT = "invalid_output"  # O1: agent.response_schema was set, the final answer never matched it after retries, and error_strategy="strict"
    ALL_TOOLS_FAILED = "all_tools_failed"  # G-outcome: final text produced, but every single dispatched tool call in the run failed (>=2 calls, 100% failure) -- prevents 'the model apologized in prose' from reading as SUCCESS/SUCCESS_WITH_RECOVERY
