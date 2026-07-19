import hashlib
import re
from typing import Callable

from fabri.core.llm import LLMBackend, LLMUsage
from fabri.core.logging_setup import get_logger
from fabri.events import EventType
from fabri.memory.compress import (
    DEFAULT_MAX_TOKENS,
    count_tokens,
    synthesize_guideline,
    synthesize_success_pattern,
)
from fabri.memory.output import split_agent_output
from fabri.memory.embeddings import embeddings_available
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


def guideline_dedup_key(
    kind: str,
    task: str = "",
    tool_names: list[str] | None = None,
    failed_tool_name: str = "",
    error_text: str = "",
    path: str = "",
) -> str:
    """Return a stable key for a mined lesson, independent of LLM wording."""
    normalized_task = re.sub(r"\s+", " ", task).strip().lower()
    if kind == "success_pattern":
        signature = f"success_pattern::{normalized_task}::{'|'.join(tool_names or [])}"
    elif kind == "tactical":
        first_line = error_text.strip().splitlines()[0] if error_text.strip() else "unknown"
        error_signature = re.sub(r"\s+", " ", first_line).strip().lower()[:160]
        signature = f"tactical::{normalized_task}::{failed_tool_name}::{error_signature}"
    elif kind == "postmortem":
        signature = f"postmortem::{normalized_task}"
    elif kind == "discrepancy":
        signature = f"discrepancy::{path}"
    else:
        raise ValueError(f"unsupported guideline dedup kind: {kind}")
    return hashlib.sha256(signature.encode()).hexdigest()


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


def extract_agent_memory(events: list[dict]) -> dict | None:
    """B5: when an agent's final text carries a ``<!-- AGENT_MEMORY -->`` block,
    return its parsed structured form so the miner can fold the agent's own
    self-reported memory into what it learns from the run. Returns None when the
    run has no `final` event or the final text has no marker -- so the miner's
    behaviour is unchanged for the (overwhelming) marker-free case."""
    final = next((e for e in events if e.get("type") == EventType.FINAL.value), None)
    if final is None:
        return None
    _, memory = split_agent_output(final.get("text") or "")
    return memory


def _deterministic_failure_text(task: str, event: dict, max_task_chars: int = 80) -> str:
    """LLM-free counterpart to `synthesize_guideline`. Stable phrasing keyed on
    (tool, error-signature) so the same failure mode dedups/merges via cosine
    similarity + rising hit_count instead of spawning near-duplicates — exactly
    like `build_postmortem_text`. Embeds a short task prefix so vector search
    over a future task can still surface it; the stable tool/error part
    dominates so repeats collapse. This is what powers the $0 ingest path: a
    huge external log mines into memory without a single LLM call."""
    sig = _error_signature(event.get("result", {}))
    name = event.get("name", "?")
    t = task[:max_task_chars] + ("…" if len(task) > max_task_chars else "")
    return f"On tasks like {t!r}: {name} previously failed [{sig}] — anticipate and avoid it."


def _deterministic_success_text(
    task: str, tool_names: list[str], outcome: str, max_task_chars: int = 80
) -> str:
    """LLM-free counterpart to `synthesize_success_pattern`. Records the winning
    tool sequence for a task so a similar future task can reuse the plan without
    the miner paying for an LLM compression call."""
    unique = list(dict.fromkeys(tool_names))
    t = task[:max_task_chars] + ("…" if len(task) > max_task_chars else "")
    return f"On tasks like {t!r}: this plan worked (tools in order: {unique}); outcome={outcome}."


def file_recovery_evidence(events: list[dict]) -> list[str]:
    """Extract bounded recovery decisions without retaining user file paths."""
    evidence: list[str] = []
    for index, event in enumerate(events):
        if event.get("name") != "read_file" or not is_tool_failure(event):
            continue
        failed_path = (event.get("args") or {}).get("path")
        if not isinstance(failed_path, str) or "/" not in failed_path:
            continue
        parent = failed_path.rsplit("/", 1)[0]
        later = events[index + 1 :]
        listed_parent = any(
            candidate.get("name") == "list_dir"
            and (candidate.get("args") or {}).get("path") == parent
            and (candidate.get("result") or {}).get("ok") is True
            for candidate in later
        )
        read_alternative = any(
            candidate.get("name") == "read_file"
            and isinstance((candidate.get("args") or {}).get("path"), str)
            and (candidate.get("args") or {}).get("path", "").startswith(parent + "/")
            and (candidate.get("args") or {}).get("path") != failed_path
            and (candidate.get("result") or {}).get("ok") is True
            for candidate in later
        )
        if listed_parent and read_alternative:
            evidence.append(
                "Recovery observed: after an exact file read failed, listing its parent "
                "and verifying an alternate file allowed the task to continue."
            )
    return list(dict.fromkeys(evidence))


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
    on_usage: Callable[[LLMUsage], None] | None = None,
    events: list[dict] | None = None,
    synthesize: bool = True,
    max_entries: int | None = None,
    eviction_half_life_days: float = 30.0,
    eviction_strategy: str = "delete",
) -> list[MemoryEntry]:
    """Mine a session's trace for failures, synthesize each into a compressed
    guideline, and ingest it into memory (dedup/promote per pruning rules).
    This is the lifecycle step that closes the loop: today's failure becomes
    tomorrow's retrieved context.

    M1: when `record_postmortem` is set (memory.record_postmortems in config),
    ALSO ingest one deterministic whole-run postmortem regardless of outcome —
    the "you tried X N times last week" signal the planner retrieves by task
    similarity. Off by default so memory-store contents and entry counts are
    unchanged for callers that don't opt in.

    Ingest layer (the Improver / `fabri.readlogs`): pass `events` to mine an
    in-memory event list built by a log adapter instead of reading a native
    trace file off disk — `session_id` is then only used to tag provenance on
    the resulting entries. `synthesize=False` swaps the two LLM miners (failure
    guideline, success pattern) for deterministic, keyed text so ingesting an
    arbitrarily large external log costs $0; `llm` is never invoked in that
    mode. Both default to today's behaviour (read from disk, LLM synthesis) so
    every existing caller is byte-identical."""
    if not embeddings_available():
        logger.info("memory learning disabled — install fabri[embeddings] to enable")
        return []

    if events is None:
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
            dedup_key=guideline_dedup_key("postmortem", task=task),
            max_entries=max_entries,
            eviction_half_life_days=eviction_half_life_days,
            eviction_strategy=eviction_strategy,
            eviction_llm=llm,
            eviction_guideline_max_tokens=guideline_max_tokens,
            on_eviction_usage=on_usage,
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
            recovery_evidence = file_recovery_evidence(events)
            if recovery_evidence:
                success_summary += "\n" + "\n".join(recovery_evidence)
            # B5: if the agent emitted a machine-readable memory block, fold its
            # self-reported facts into the summary the synthesizer sees. Guarded
            # on the marker being present, so a marker-free run is unaffected.
            agent_memory = extract_agent_memory(events)
            if agent_memory:
                memory_lines = "\n".join(f"{k}: {v}" for k, v in agent_memory.items())
                success_summary += f"\nAgent-reported memory:\n{memory_lines}"
            if synthesize:
                success_text = synthesize_success_pattern(
                    success_summary, llm, max_tokens=guideline_max_tokens, on_usage=on_usage,
                )
            else:
                success_text = _deterministic_success_text(
                    task, tool_names, final_event.get("outcome", "success"),
                )
            logger.debug(
                "success pattern (%d tokens): %r",
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
                dedup_key=guideline_dedup_key(
                    "success_pattern", task=task, tool_names=tool_names,
                ),
                max_entries=max_entries,
                eviction_half_life_days=eviction_half_life_days,
                eviction_strategy=eviction_strategy,
                eviction_llm=llm,
                eviction_guideline_max_tokens=guideline_max_tokens,
                on_eviction_usage=on_usage,
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
            dedup_key=guideline_dedup_key("discrepancy", path=path),
            max_entries=max_entries,
            eviction_half_life_days=eviction_half_life_days,
            eviction_strategy=eviction_strategy,
            eviction_llm=llm,
            eviction_guideline_max_tokens=guideline_max_tokens,
            on_eviction_usage=on_usage,
        )
        new_entries.append(entry)

    for event in failures:
        if synthesize:
            # `args` is optional on externally-ingested events (a raw log may
            # not carry the tool's inputs); default so mining never crashes.
            failure_summary = (
                f"Task: {task}\nTool: {event['name']}\nArgs: {event.get('args', {})}\n"
                f"Failure: {(event.get('result') or {}).get('error')}"
            )
            guideline_text = synthesize_guideline(
                failure_summary, llm, max_tokens=guideline_max_tokens, on_usage=on_usage,
            )
        else:
            guideline_text = _deterministic_failure_text(task, event)
        logger.debug(
            "guideline (%d tokens) for tool %s: %r",
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
            dedup_key=guideline_dedup_key(
                "tactical",
                task=task,
                failed_tool_name=event["name"],
                error_text=(event.get("result") or {}).get("error") or "",
            ),
            max_entries=max_entries,
            eviction_half_life_days=eviction_half_life_days,
            eviction_strategy=eviction_strategy,
            eviction_llm=llm,
            eviction_guideline_max_tokens=guideline_max_tokens,
            on_eviction_usage=on_usage,
        )
        new_entries.append(entry)

    return new_entries
