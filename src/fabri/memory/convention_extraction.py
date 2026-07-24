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
_CONVENTION_SCOPES = frozenset({"agent", "agency", "company"})
# Intentionally dumb and conservative: retry only when the text contains either
# a declaration verb plus a convention/branch noun, or a protocol noun plus an
# explicit branch/mapping noun. This avoids spending a second LLM call on prose
# that merely uses "if" or "when".
_DECLARATION_SIGNAL_KEYWORDS = {
    "declaration": (
        "declare",
        "declared",
        "declares",
        "decree",
        "decreed",
        "decrees",
        "establish",
        "established",
        "establishes",
        "define",
        "defined",
        "defines",
    ),
    "protocol": (
        "protocol",
        "protocols",
        "convention",
        "conventions",
        "policy",
        "policies",
    ),
    "structure": ("branch", "branches", "mapping", "mappings"),
}
_DECLARATION_SIGNAL_PATTERNS = {
    category: re.compile(
        rf"\b(?:{'|'.join(re.escape(keyword) for keyword in keywords)})\b",
        re.IGNORECASE,
    )
    for category, keywords in _DECLARATION_SIGNAL_KEYWORDS.items()
}
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
                    "effect_class": {"type": "string", "enum": ["response_mapping"]},
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


def _normalize_effect_class(value: str) -> str:
    """Coerce model-authored effect-class prose to the canonical literal.

    The extraction schema enums this to "response_mapping", but providers that
    ignore enum constraints still free-text it (live smoke: a perfect two-
    branch record quarantined over "customer-evidence response mapping").
    Deterministic and conservative: any string whose word set contains both
    "response" and "mapping" collapses to the literal; everything else passes
    through unchanged for the gate to reject.
    """
    words = set(re.sub(r"[^a-z0-9]+", " ", value.lower()).split())
    if {"response", "mapping"} <= words:
        return "response_mapping"
    return value


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


def _validate_response(
    value: object,
    *,
    default_scope: str | None = None,
) -> list[Mapping[str, object]]:
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
        if isinstance(raw_candidate, Mapping):
            candidate_with_scope = dict(raw_candidate)
            raw_scope = candidate_with_scope.get("scope")
            if default_scope is not None and (
                not isinstance(raw_scope, str) or raw_scope not in _CONVENTION_SCOPES
            ):
                candidate_with_scope["scope"] = default_scope
            raw_candidate = candidate_with_scope
        candidate = _validate_exact_keys(
            raw_candidate,
            expected_candidate_keys,
            path=path,
        )
        _validate_string(candidate["source_id"], path=f"{path}.source_id", nonempty=True)
        _validate_string(
            candidate["scope"],
            path=f"{path}.scope",
            allowed=_CONVENTION_SCOPES,
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


def _action_scope_context(config: Mapping[str, object]) -> dict[str, str]:
    """Return only the non-sensitive scope labels useful to the extractor."""
    action_scope = _memory_config(config).get("action_scope")
    if not isinstance(action_scope, Mapping):
        return {}
    return {
        key: value
        for key in ("company", "agency", "role")
        if isinstance((value := action_scope.get(key)), str) and value
    }


def _root_manager_default_scope(config: Mapping[str, object]) -> str | None:
    """Company compiles stamp their root manager with agency='company'."""
    action_scope = _action_scope_context(config)
    if action_scope.get("company") and action_scope.get("agency") == "company":
        return "company"
    return None


def _has_declared_protocol_signal(sources: Sequence[Mapping[str, str]]) -> bool:
    """Cheap retry gate; keyword co-occurrence is deliberate, not semantic."""
    text = "\n".join(source["content"] for source in sources)
    has_declaration = bool(_DECLARATION_SIGNAL_PATTERNS["declaration"].search(text))
    has_protocol = bool(_DECLARATION_SIGNAL_PATTERNS["protocol"].search(text))
    has_structure = bool(_DECLARATION_SIGNAL_PATTERNS["structure"].search(text))
    return (has_declaration and (has_protocol or has_structure)) or (
        has_protocol and has_structure
    )


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
    """Extract typed candidates with at most one declaration-triggered retry."""
    sources = _source_documents(trace_events_or_task_texts)
    if not sources:
        return []
    sources_by_id = {source["source_id"]: source for source in sources}
    action_scope = _action_scope_context(config)
    default_scope = _root_manager_default_scope(config)
    base_prompt = (
        "Extract only explicit, reusable conditional response-mapping protocols "
        "supported by the source documents. Return JSON only, with no markdown. "
        "Each candidate must cite exactly one source_id that contains the protocol. "
        "A protocol that is DECLARED, decreed, or established in a task or trace "
        "must be captured even when this run exercised only one branch or no branch. "
        "Preserve EVERY explicitly declared branch verbatim, including its condition "
        "and response fields; never emit only the branch that was applied. Do not "
        "infer branches or conditions that the source does not declare. When the "
        "declaring agent manages a company or agency, emit the candidate at that "
        "company or agency scope using the config scope context below. Return an "
        "empty candidates array only when no complete declared protocol is present."
        f"\n\nCONFIG SCOPE CONTEXT:\n{json.dumps(action_scope, sort_keys=True)}\n\n"
        f"STRICT JSON SCHEMA:\n{json.dumps(CONVENTION_EXTRACTION_SCHEMA, sort_keys=True)}"
        f"\n\nSOURCE DOCUMENTS:\n{json.dumps(sources, ensure_ascii=False, sort_keys=True)}"
    )

    def request_candidates(prompt: str) -> list[Mapping[str, object]]:
        try:
            response = extraction_llm.step(
                "You perform typed, evidence-anchored convention extraction.",
                [{"role": "user", "content": prompt}],
            )
        except Exception:  # noqa: BLE001 -- best-effort mining side effect
            logger.warning("convention extraction LLM call failed", exc_info=True)
            return []
        final_text = getattr(response, "final_text", None)
        if not isinstance(final_text, str) or not final_text.strip():
            logger.warning("convention extraction returned no JSON text")
            return []
        try:
            payload = json.loads(final_text)
            return _validate_response(payload, default_scope=default_scope)
        except (json.JSONDecodeError, _SchemaMismatch):
            logger.warning(
                "convention extraction response failed strict validation",
                exc_info=True,
            )
            return []

    candidates = request_candidates(base_prompt)
    if not candidates and _has_declared_protocol_signal(sources):
        retry_instruction = (
            "RETRY CORRECTION: You missed a declared protocol. Re-scan every source "
            "for a protocol that was declared or decreed but only partly exercised. "
            "Return every explicitly declared branch verbatim, not merely the applied "
            "branch. Do not invent undeclared content.\n\n"
        )
        candidates = request_candidates(retry_instruction + base_prompt)

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
            effect_class=_normalize_effect_class(str(candidate["effect_class"])),
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
