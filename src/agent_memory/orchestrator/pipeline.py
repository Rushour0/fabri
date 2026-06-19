from agent_memory.core.llm import LLMBackend
from agent_memory.core.logging_setup import get_logger
from agent_memory.memory.compress import DEFAULT_MAX_TOKENS, count_tokens, synthesize_guideline
from agent_memory.memory.pruning import PROMOTION_THRESHOLD_SESSIONS, SIMILARITY_THRESHOLD, ingest_guideline
from agent_memory.memory.schema import MemoryEntry
from agent_memory.memory.store import QdrantMemoryStore
from agent_memory.orchestrator.traces import read_trace

logger = get_logger()


def is_tool_failure(event: dict) -> bool:
    return event.get("type") == "tool_call" and not event.get("result", {}).get("ok", True)


def process_trace(
    session_id: str,
    store: QdrantMemoryStore,
    llm: LLMBackend,
    guideline_max_tokens: int = DEFAULT_MAX_TOKENS,
    similarity_threshold: float = SIMILARITY_THRESHOLD,
    promotion_threshold_sessions: int = PROMOTION_THRESHOLD_SESSIONS,
) -> list[MemoryEntry]:
    """Mine a session's trace for failures, synthesize each into a compressed
    guideline, and ingest it into memory (dedup/promote per pruning rules).
    This is the lifecycle step that closes the loop: today's failure becomes
    tomorrow's retrieved context."""
    events = read_trace(session_id)
    task = next((e["task"] for e in events if e.get("type") == "start"), "")
    failures = [e for e in events if is_tool_failure(e)]
    logger.info("processing trace %s: %d failure(s) found", session_id, len(failures))

    new_entries = []
    for event in failures:
        failure_summary = (
            f"Task: {task}\nTool: {event['name']}\nArgs: {event['args']}\n"
            f"Failure: {event['result'].get('error')}"
        )
        guideline_text = synthesize_guideline(failure_summary, llm, max_tokens=guideline_max_tokens)
        logger.debug(
            "synthesized guideline (%d tokens) for tool %s: %r",
            count_tokens(guideline_text),
            event["name"],
            guideline_text,
        )
        entry = ingest_guideline(
            store,
            guideline_text,
            session_id,
            tools=[event["name"]],
            similarity_threshold=similarity_threshold,
            promotion_threshold_sessions=promotion_threshold_sessions,
        )
        new_entries.append(entry)

    return new_entries
