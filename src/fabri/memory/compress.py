import tiktoken

from fabri.core.llm import LLMBackend

ENCODING = tiktoken.get_encoding("cl100k_base")
DEFAULT_MAX_TOKENS = 30


def count_tokens(text: str) -> int:
    return len(ENCODING.encode(text))


def enforce_token_cap(text: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
    """Hard backstop: truncate to max_tokens regardless of what the LLM returned,
    so a verbose synthesis never silently bloats the memory store."""
    tokens = ENCODING.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return ENCODING.decode(tokens[:max_tokens]).rstrip() + "..."


def synthesize_success_pattern(
    success_summary: str, llm: LLMBackend, max_tokens: int = DEFAULT_MAX_TOKENS
) -> str:
    """A4: compress a successful run summary into a short reusable guideline.
    Mirrors `synthesize_guideline` but framed as a 'what worked' pattern so
    later retrieval can blend it alongside the failure-derived guidelines."""
    prompt = (
        "Summarize the following successful agent run as one short, generalized "
        f"guideline (max {max_tokens} tokens) capturing what worked and would "
        f"help reproduce the success on a similar task:\n\n{success_summary}"
    )
    response = llm.step(
        "You compress agent successes into short reusable guidelines.",
        [{"role": "user", "content": prompt}],
    )
    text = response.final_text or success_summary
    return enforce_token_cap(text.strip(), max_tokens)


def synthesize_guideline(failure_summary: str, llm: LLMBackend, max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
    """Ask the LLM to compress a failure/trace summary into one short, generalized
    guideline, then enforce the token cap as a hard backstop regardless of output."""
    prompt = (
        "Summarize the following agent failure as one short, generalized guideline "
        f"(max {max_tokens} tokens) that would help avoid it next time:\n\n{failure_summary}"
    )
    response = llm.step(
        "You compress agent failures into short actionable guidelines.",
        [{"role": "user", "content": prompt}],
    )
    text = response.final_text or failure_summary
    return enforce_token_cap(text.strip(), max_tokens)
