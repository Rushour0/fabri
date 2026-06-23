import json
import re

from fabri import toon
from fabri.core.llm import LLMBackend
from fabri.tools.result import tool_error, tool_ok

DEFAULT_MAX_SUBQUESTIONS = 5

# P3 hardening: models often wrap structured output in markdown code fences
# ("```json\n...\n```") even when asked for raw output. Strip them before
# json.loads / toon.decode so a well-formed-but-fenced response isn't
# misclassified as "malformed".
_FENCE_RE = re.compile(r"^```(?:json|toon)?\s*\n?|\n?```\s*$", re.MULTILINE)


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text).strip()


def decompose(
    llm: LLMBackend,
    task: str,
    max_subquestions: int = DEFAULT_MAX_SUBQUESTIONS,
    output_format: str = "json",
) -> dict:
    """Ask the LLM (a separate step() call, not a recursive run_agent) to break a
    research task into concrete sub-questions. Returns the same {ok, result}
    shape tools.invoke() returns, so the caller's message-append and trace
    logging stay unmodified -- this is structured planning, not a sub-agent.

    `output_format` is the format the model is asked to emit. "json" is the
    reliable default; "toon" is opt-in and saves a few output tokens, but we
    always accept either on parse so a model that ignores the instruction (or
    emits slightly-off TOON) still works."""
    if output_format == "toon":
        shape = "a TOON array of strings, e.g. `[3]: first question,second question,third`"
    else:
        shape = 'a JSON list of strings, e.g. ["first question", "second question"]'
    prompt = (
        f"Break this task into at most {max_subquestions} concrete, separately "
        f"answerable sub-questions. Return ONLY {shape}.\n\nTask: {task}"
    )
    response = llm.step(
        "You decompose research tasks into concrete sub-questions.",
        [{"role": "user", "content": prompt}],
    )
    text = (response.final_text or "").strip()
    subquestions = _parse_string_list(text, prefer=output_format)
    if subquestions is None:
        return tool_error(f"decompose: malformed response: {text!r}")
    return tool_ok({"subquestions": subquestions[:max_subquestions]})


def _parse_string_list(text: str, prefer: str) -> list | None:
    """Parse a list of strings from the model, trying the preferred format first
    and falling back to the other -- a model may answer in either."""
    parsers = [_try_toon, _try_json] if prefer == "toon" else [_try_json, _try_toon]
    for parse in parsers:
        value = parse(text)
        if isinstance(value, list):
            return value
    return None


def _try_json(text: str):
    try:
        return json.loads(_strip_fences(text))
    except (json.JSONDecodeError, ValueError):
        return None


def _try_toon(text: str):
    try:
        return toon.decode(_strip_fences(text))
    except Exception:
        return None
