from enum import Enum


class Outcome(str, Enum):
    SUCCESS = "success"  # final text produced, no tool failures in the run
    SUCCESS_WITH_RECOVERY = "success_with_recovery"  # final text produced, but >=1 tool call failed along the way
    INCOMPLETE = "incomplete"  # hit MAX_STEPS with no final text
    FAILED = "failed"  # unrecoverable LLM error: API/rate-limit failure or a truncated response (see core.llm.LLMError)
