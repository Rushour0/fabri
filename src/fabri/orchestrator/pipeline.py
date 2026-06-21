from fabri.core.llm import LLMBackend
from fabri.core.logging_setup import get_logger
from fabri.events import EventType
from fabri.memory.compress import (
    DEFAULT_MAX_TOKENS,
    count_tokens,
    synthesize_guideline,
    synthesize_success_pattern,
)
from fabri.memory.pruning import PROMOTION_THRESHOLD_SESSIONS, SIMILARITY_THRESHOLD, ingest_guideline
from fabri.memory.schema import MemoryEntry
from fabri.memory.store import QdrantMemoryStore
from fabri.orchestrator.traces import read_trace
from fabri.tools.result import is_error

logger = get_logger()


def is_tool_failure(event: dict) -> bool:
    return event.get("type") == EventType.TOOL_CALL.value and is_error(event.get("result", {}))


def is_discrepancy(event: dict) -> bool:
    return event.get("type") == EventType.DISCREPANCY.value


def _discrepancy_guideline_text(path: str) -> str:
    """Canonical phrasing matching the surrounding guideline style: terse,
    imperative, names the corrective behavior. Keep the wording stable so
    repeated drift on the same path dedupes via cosine similarity rather than
    accumulating near-duplicates."""
    return (
        f"After write_file/edit_file at {path}, re-read the file in the same "
        f"step to confirm the write persisted."
    )


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
    task = next((e["task"] for e in events if e.get("type") == EventType.START.value), "")
    failures = [e for e in events if is_tool_failure(e)]
    logger.info("processing trace %s: %d failure(s) found", session_id, len(failures))

    new_entries: list[MemoryEntry] = []

    # A4: mine *successes* too. A run that ended with a `final` outcome and at
    # least one ok=true tool_call yields a "what worked" guideline keyed on
    # (task, plan_summary). Without this, every fresh run rediscovers the same
    # decomposition; the orchestrator prompt tells the model to "reuse prior
    # successes" but the memory store contained zero success patterns to reuse.
    final_event = next((e for e in events if e.get("type") == EventType.FINAL.value), None)
    if final_event is not None:
        ok_tool_calls = [
            e for e in events
            if e.get("type") == EventType.TOOL_CALL.value and (e.get("result") or {}).get("ok") is True
        ]
        if ok_tool_calls:
            tool_names = [e["name"] for e in ok_tool_calls]
            unique_tools = list(dict.fromkeys(tool_names))
            success_summary = (
                f"Task: {task}\n"
                f"Plan: tools used in order = {tool_names}\n"
                f"Outcome: {final_event.get('outcome', 'success')}"
            )
            success_text = synthesize_success_pattern(success_summary, llm, max_tokens=guideline_max_tokens)
            logger.debug(
                "synthesized success pattern (%d tokens): %r",
                count_tokens(success_text), success_text,
            )
            entry = ingest_guideline(
                store,
                success_text,
                session_id,
                tools=unique_tools,
                similarity_threshold=similarity_threshold,
                promotion_threshold_sessions=promotion_threshold_sessions,
                kind="success_pattern",
            )
            new_entries.append(entry)

    for event in events:
        if not is_discrepancy(event):
            continue
        path = event.get("path", "<unknown>")
        guideline_text = _discrepancy_guideline_text(path)
        entry = ingest_guideline(
            store,
            guideline_text,
            session_id,
            tools=["write_file", "edit_file"],
            similarity_threshold=similarity_threshold,
            promotion_threshold_sessions=promotion_threshold_sessions,
        )
        new_entries.append(entry)

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
