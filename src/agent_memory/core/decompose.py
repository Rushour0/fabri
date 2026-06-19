import json

from agent_memory.core.llm import LLMBackend

DEFAULT_MAX_SUBQUESTIONS = 5


def decompose(llm: LLMBackend, task: str, max_subquestions: int = DEFAULT_MAX_SUBQUESTIONS) -> dict:
    """Ask the LLM (a separate step() call, not a recursive run_agent) to break a
    research task into concrete sub-questions. Returns the same {ok, result}
    shape tools.invoke() returns, so the caller's message-append and trace
    logging stay unmodified -- this is structured planning, not a sub-agent."""
    prompt = (
        f"Break this task into at most {max_subquestions} concrete, separately "
        f"answerable sub-questions. Return ONLY a JSON list of strings.\n\nTask: {task}"
    )
    response = llm.step(
        "You decompose research tasks into concrete sub-questions.",
        [{"role": "user", "content": prompt}],
    )
    text = (response.final_text or "").strip()
    try:
        subquestions = json.loads(text)
        if not isinstance(subquestions, list):
            raise ValueError("not a list")
    except (json.JSONDecodeError, ValueError) as e:
        return {"ok": False, "error": f"decompose: malformed response: {e}"}

    return {"ok": True, "result": {"subquestions": subquestions[:max_subquestions]}}
