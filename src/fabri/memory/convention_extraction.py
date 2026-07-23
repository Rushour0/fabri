"""Engine-side typed extraction of conditional response conventions."""
from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import replace

from fabri.core.llm import LLMBackend
from fabri.core.logging_setup import get_logger
from fabri.memory.conventions import ConventionRecord, convention_quarantine_reason

logger = get_logger()

_MINED_ORIGINS = frozenset({"task", "model", "tool"})
_UNRESOLVED_REFERENT = re.compile(
    r"""
    (?:
        ^\s*(?:(?:if|when|unless)\s+)?(?:this|that|these|those|it|they)\b
        |\b(?:the|as)\s+(?:above|below|former|latter|same)\b
        |\baforementioned\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

CONVENTION_EXTRACTION_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["candidates"],
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "source_id",
                    "scope",
                    "key",
                    "version",
                    "effect_class",
                    "conditions",
                    "branches",
                    "response_schema",
                ],
                "properties": {
                    "source_id": {"type": "string", "minLength": 1},
                    "scope": {"type": "string", "enum": ["agent", "agency", "company"]},
                    "key": {"type": "string", "minLength": 1},
                    "version": {"type": "string", "minLength": 1},
                    "effect_class": {"type": "string", "minLength": 1},
                    "conditions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["branch_id", "condition_text"],
                            "properties": {
                                "branch_id": {"type": "string", "minLength": 1},
                                "condition_text": {"type": "string"},
                            },
                        },
                    },
                    "branches": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["branch_id", "fields"],
                            "properties": {
                                "branch_id": {"type": "string", "minLength": 1},
                                "fields": {"type": "object"},
                            },
                        },
                    },
                    "response_schema": {"type": "object"},
                },
            },
        },
    },
}


class _SchemaMismatch(ValueError):
    """The extraction response did not match the closed JSON shape."""


def _is_string(value: object, *, nonempty: bool = False) -> bool:
    return isinstance(value, str) and (not nonempty or bool(value.strip()))


def _validate_string(
    value: object,
    *,
    path: str,
    nonempty: bool = False,
    allowed: frozenset[str] | None = None,
) -> str:
    if not _is_string(value, nonempty=nonempty):
        raise _SchemaMismatch(f"{path} must be a string")
    assert isinstance(value, str)
    if allowed is not None and value not in allowed:
        raise _SchemaMismatch(f"{path} is not an allowed value")
    return value


def _validate_exact_keys(value: object, expected: set[str], *, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise _SchemaMismatch(f"{path} has unexpected or missing fields")
    return value


def _validate_response(value: object) -> list[Mapping[str, object]]:
    root = _validate_exact_keys(value, {"candidates"}, path="$")
    candidates = root["candidates"]
    if not isinstance(candidates, list):
        raise _SchemaMismatch("$.candidates must be an array")

    expected_candidate_keys = {
        "source_id",
        "scope",
        "key",
        "version",
        "effect_class",
        "conditions",
        "branches",
        "response_schema",
    }
    validated: list[Mapping[str, object]] = []
    for candidate_index, raw_candidate in enumerate(candidates):
        path = f"$.candidates[{candidate_index}]"
        candidate = _validate_exact_keys(
            raw_candidate,
            expected_candidate_keys,
            path=path,
        )
        _validate_string(candidate["source_id"], path=f"{path}.source_id", nonempty=True)
        _validate_string(
            candidate["scope"],
            path=f"{path}.scope",
            allowed=frozenset({"agent", "agency", "company"}),
        )
        for field_name in ("key", "version", "effect_class"):
            _validate_string(
                candidate[field_name],
                path=f"{path}.{field_name}",
                nonempty=True,
            )

        conditions = candidate["conditions"]
        if not isinstance(conditions, list):
            raise _SchemaMismatch(f"{path}.conditions must be an array")
        for condition_index, raw_condition in enumerate(conditions):
            condition_path = f"{path}.conditions[{condition_index}]"
            condition = _validate_exact_keys(
                raw_condition,
                {"branch_id", "condition_text"},
                path=condition_path,
            )
            _validate_string(
                condition["branch_id"],
                path=f"{condition_path}.branch_id",
                nonempty=True,
            )
            _validate_string(
                condition["condition_text"],
                path=f"{condition_path}.condition_text",
            )

        branches = candidate["branches"]
        if not isinstance(branches, list):
            raise _SchemaMismatch(f"{path}.branches must be an array")
        for branch_index, raw_branch in enumerate(branches):
            branch_path = f"{path}.branches[{branch_index}]"
            branch = _validate_exact_keys(
                raw_branch,
                {"branch_id", "fields"},
                path=branch_path,
            )
            _validate_string(
                branch["branch_id"],
                path=f"{branch_path}.branch_id",
                nonempty=True,
            )
            if not isinstance(branch["fields"], Mapping):
                raise _SchemaMismatch(f"{branch_path}.fields must be an object")
        if not isinstance(candidate["response_schema"], Mapping):
            raise _SchemaMismatch(f"{path}.response_schema must be an object")
        validated.append(candidate)
    return validated


def _source_documents(
    trace_events_or_task_texts: Sequence[Mapping[str, object] | str],
) -> list[dict[str, str]]:
    documents: list[dict[str, str]] = []

    def add(source_id: str, origin: str, content: object) -> None:
        if origin not in _MINED_ORIGINS:
            return
        if isinstance(content, str):
            text = content.strip()
        else:
            try:
                text = json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)
            except (TypeError, ValueError):
                return
        if text:
            documents.append(
                {"source_id": source_id, "origin": origin, "content": text}
            )

    for index, item in enumerate(trace_events_or_task_texts):
        if isinstance(item, str):
            add(f"task_text:{index}", "task", item)
            continue
        if not isinstance(item, Mapping):
            continue
        event_type = item.get("type")
        if event_type == "start":
            add(f"event:{index}:task", "task", item.get("task"))
        elif event_type == "tool_call":
            if "args" in item:
                add(
                    f"event:{index}:model",
                    "model",
                    {"tool_name": item.get("name"), "args": item.get("args")},
                )
            if "result" in item:
                add(
                    f"event:{index}:tool",
                    "tool",
                    {"tool_name": item.get("name"), "result": item.get("result")},
                )
        elif event_type in {"thought", "final"}:
            add(
                f"event:{index}:model",
                "model",
                item.get("text") or item.get("content"),
            )
    return documents


def _memory_config(config: Mapping[str, object]) -> Mapping[str, object]:
    memory = config.get("memory")
    return memory if isinstance(memory, Mapping) else config


def _provenance_prefix(config: Mapping[str, object]) -> str:
    memory = _memory_config(config)
    value = memory.get("_convention_provenance_prefix")
    return value if isinstance(value, str) and value else "convention_extraction"


def _normalized_condition(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(normalized.split()).strip(" \t\r\n.,;:")


def convention_candidate_gate_reason(
    record: ConventionRecord,
    *,
    config: Mapping[str, object],
) -> str | None:
    """Return the Stage 1 or extraction-specific fail-closed gate reason."""
    reason = convention_quarantine_reason(record, config=config)
    if reason is not None:
        return reason
    condition_texts = [
        condition.get("condition_text")
        for condition in record.conditions
    ]
    if not condition_texts or any(
        not isinstance(text, str) or not text.strip()
        for text in condition_texts
    ):
        return "missing_conditions"
    if any(_UNRESOLVED_REFERENT.search(text) for text in condition_texts):
        return "unresolved_referent"
    normalized = [_normalized_condition(text) for text in condition_texts]
    if any(not text for text in normalized) or len(normalized) != len(set(normalized)):
        return "duplicate_conditions"
    return None


def extract_convention_candidates(
    trace_events_or_task_texts: Sequence[Mapping[str, object] | str],
    extraction_llm: LLMBackend,
    *,
    config: Mapping[str, object],
) -> list[ConventionRecord]:
    """Extract typed candidates in one call; malformed output fails closed."""
    sources = _source_documents(trace_events_or_task_texts)
    if not sources:
        return []
    sources_by_id = {source["source_id"]: source for source in sources}
    prompt = (
        "Extract only explicit, reusable conditional response-mapping protocols "
        "supported by the source documents. Return JSON only, with no markdown. "
        "Each candidate must cite exactly one source_id that contains the protocol. "
        "Do not infer missing branches or conditions. Return an empty candidates "
        "array when no complete protocol is present.\n\n"
        f"STRICT JSON SCHEMA:\n{json.dumps(CONVENTION_EXTRACTION_SCHEMA, sort_keys=True)}"
        f"\n\nSOURCE DOCUMENTS:\n{json.dumps(sources, ensure_ascii=False, sort_keys=True)}"
    )
    try:
        response = extraction_llm.step(
            "You perform typed, evidence-anchored convention extraction.",
            [{"role": "user", "content": prompt}],
        )
    except Exception:  # noqa: BLE001 -- extraction is a best-effort mining side effect
        logger.warning("convention extraction LLM call failed", exc_info=True)
        return []
    final_text = getattr(response, "final_text", None)
    if not isinstance(final_text, str) or not final_text.strip():
        logger.warning("convention extraction returned no JSON text")
        return []
    try:
        payload = json.loads(final_text)
        candidates = _validate_response(payload)
    except (json.JSONDecodeError, _SchemaMismatch):
        logger.warning("convention extraction response failed strict validation", exc_info=True)
        return []

    records: list[ConventionRecord] = []
    for candidate in candidates:
        source_id = candidate["source_id"]
        assert isinstance(source_id, str)
        source = sources_by_id.get(source_id)
        if source is None:
            logger.warning(
                "dropping convention candidate with unknown source_id %r",
                source_id,
            )
            continue
        conditions = candidate["conditions"]
        branches = candidate["branches"]
        response_schema = candidate["response_schema"]
        assert isinstance(conditions, list)
        assert isinstance(branches, list)
        assert isinstance(response_schema, Mapping)
        record = ConventionRecord(
            scope=str(candidate["scope"]),
            key=str(candidate["key"]),
            version=str(candidate["version"]),
            effect_class=str(candidate["effect_class"]),
            conditions=[dict(condition) for condition in conditions],
            branches=[dict(branch) for branch in branches],
            origin=source["origin"],
            provenance=f"{_provenance_prefix(config)}:{source_id}",
            response_schema=dict(response_schema),
        )
        reason = convention_candidate_gate_reason(record, config=config)
        if reason in {"missing_conditions", "unresolved_referent", "duplicate_conditions"}:
            record = replace(record, provenance=f"{record.provenance};gate={reason}")
        if reason is not None:
            logger.info(
                "convention candidate %s/%s requires quarantine: %s",
                record.scope,
                record.key,
                reason,
            )
        records.append(record)
    return records
