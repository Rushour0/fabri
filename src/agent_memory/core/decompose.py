import json

from agent_memory import toon
from agent_memory.core.llm import LLMBackend

DEFAULT_MAX_SUBQUESTIONS = 5


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
        return {"ok": False, "error": f"decompose: malformed response: {text!r}"}
    return {"ok": True, "result": {"subquestions": subquestions[:max_subquestions]}}


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
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _try_toon(text: str):
    try:
        return toon.decode(text)
    except Exception:
        return None
