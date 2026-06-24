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


def _error_signature(result: dict) -> str:
    """A short, stable signature for a tool error so two runs that hit the
    same failure mode group together. First line of the error, truncated."""
    err = (result or {}).get("error") or ""
    first = err.strip().splitlines()[0] if err.strip() else "unknown"
    return first[:50]


def build_postmortem_text(events: list[dict], max_task_chars: int = 140) -> str:
    """M1: a compact, DETERMINISTIC whole-run summary — no LLM call. Captures
    the signal the planner wants on a similar future task ("you tried X, it
    failed N times, the run cost K steps") so it can be retrieved by task
    similarity instead of rediscovered. Embeds the task text so vector search
    over a new task surfaces it.

    Deterministic on purpose: the volatile parts (counts) stay small while the
    stable parts (task + failing tool/error signatures) dominate, so repeat
    runs of the same task dedup/merge into one entry with a rising hit_count
    rather than spawning near-duplicates."""
    task = next((e.get("task", "") for e in events if e.get("type") == EventType.START.value), "")
    outcome = next(
        (e.get("outcome") for e in events
         if e.get("type") in (EventType.FINAL.value, EventType.FAILED.value, EventType.INCOMPLETE.value)
         and e.get("outcome")),
        "?",
    )
    usage = next((e for e in events if e.get("type") == EventType.USAGE.value), {})
    step_count = usage.get("step_count")
    if step_count is None:
        step_count = sum(1 for e in events if e.get("type") == EventType.STEP_STARTED.value)

    tool_calls = [e for e in events if e.get("type") == EventType.TOOL_CALL.value]
    failures = [e for e in tool_calls if is_error(e.get("result", {}))]

    # Group failures by (tool, error-signature) so "foo failed 3× [timeout]"
    # is one phrase, not three lines. dict preserves first-seen order.
    grouped: dict[tuple[str, str], int] = {}
    for e in failures:
        key = (e.get("name", "?"), _error_signature(e.get("result", {})))
        grouped[key] = grouped.get(key, 0) + 1
    if grouped:
        repeats = "; ".join(
            f"{name}×{n} [{sig}]" for (name, sig), n in grouped.items()
        )
    else:
        repeats = "none"

    task_str = task[:max_task_chars] + ("…" if len(task) > max_task_chars else "")
    return (
        f"Run postmortem — task: {task_str!r}. "
        f"outcome={outcome}, steps={step_count}, "
        f"tool_calls={len(tool_calls)} ({len(failures)} failed). "
        f"Repeated failures: {repeats}."
    )


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
    record_postmortem: bool = False,
) -> list[MemoryEntry]:
    """Mine a session's trace for failures, synthesize each into a compressed
    guideline, and ingest it into memory (dedup/promote per pruning rules).
    This is the lifecycle step that closes the loop: today's failure becomes
    tomorrow's retrieved context.

    M1: when `record_postmortem` is set (memory.record_postmortems in config),
    ALSO ingest one deterministic whole-run postmortem regardless of outcome —
    the "you tried X N times last week" signal the planner retrieves by task
    similarity. Off by default so memory-store contents and entry counts are
    unchanged for callers that don't opt in."""
    events = read_trace(session_id)
    task = next((e["task"] for e in events if e.get("type") == EventType.START.value), "")
    failures = [e for e in events if is_tool_failure(e)]
    logger.info("processing trace %s: %d failure(s) found", session_id, len(failures))

    new_entries: list[MemoryEntry] = []

    if record_postmortem and events:
        postmortem_text = build_postmortem_text(events)
        # Tag with the tools that actually failed so a tool-name match in a
        # future task pulls the postmortem via the tag path too, not just
        # vector similarity.
        failed_tools = list(dict.fromkeys(e["name"] for e in failures if e.get("name")))
        entry = ingest_guideline(
            store,
            postmortem_text,
            session_id,
            tools=failed_tools,
            similarity_threshold=similarity_threshold,
            promotion_threshold_sessions=promotion_threshold_sessions,
            kind="postmortem",
        )
        logger.debug("recorded postmortem: %r", postmortem_text)
        new_entries.append(entry)

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
