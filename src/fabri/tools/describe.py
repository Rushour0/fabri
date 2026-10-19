"""Optional LLM rewrite of tool titles and descriptions.

The deterministic text in fabri.tools.catalog is always there. This module
asks a model (any fabri ``llm:`` block, or a backend object) to rewrite it,
once per tool, cached in ``descriptions.json`` by a hash of the operation
schema plus its probe examples. A rewrite is kept only when it is grounded:

- the prompt holds only the schema and the redacted probe examples
- the output may name only declared params, the tool's own names and its
  sibling tools, and only declared status codes
- the title is at most 60 chars and the description at most 1,200

A rewrite that ``fabri.style.slop_report`` flags is regenerated once with
the findings; if it still has tells, or fails grounding, the deterministic
text stays and the report says why.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from fabri.style import PLAIN_WRITING_RULES, slop_report
from fabri.tools.catalog import (
    DESCRIPTION_CAP,
    TITLE_CAP,
    describe_cache_key,
    read_state,
    state_dir_for,
    write_state,
)
from fabri.tools.policy import ToolPolicy

SYSTEM = (
    "You write the title and description an AI agent reads to choose and call an API tool.\n"
    "Use only the facts in the input. Do not invent parameters, status codes, fields or behaviour.\n"
    "Reply with JSON only: {\"title\": \"...\", \"description\": \"...\"}.\n"
    f"title: at most {TITLE_CAP} characters, a short label such as 'List pets'.\n"
    f"description: at most {DESCRIPTION_CAP} characters, in short lines: what it does (one sentence); "
    "when to use it and which sibling tool to use instead; key params, required first, with allowed values; "
    "errors by status code with what to do; the approval class.\n\n" + PLAIN_WRITING_RULES
)
_IDENT = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+|[a-z]+[A-Z][A-Za-z0-9]*)\b")
_STATUS = re.compile(r"\b([1-5][0-9]{2})\b")


def _facts(tool, entry, siblings: list[str]) -> dict[str, Any]:
    op = tool.operation
    return {
        "name": tool.name, "aliases": tool.aliases, "approval": tool.approval,
        "method": tool.method, "path": tool.path,
        "summary": op.summary if op else None,
        "description": (op.text if op else tool.base_description) or None,
        "params": [{"name": p.name, "type": p.type, "required": p.required, "enum": p.enum,
                    "known_values": (p.options or [])[:5]} for p in entry.params],
        "responses": {code: info.get("reasons") or [] for code, info in (op.responses if op else {}).items()},
        "sibling_tools": siblings,
        "examples": entry.examples[:2],
    }


def grounding_errors(text: str, facts: Mapping[str, Any]) -> list[str]:
    """Identifiers and status codes in `text` that the facts do not hold."""
    allowed = {facts["name"], *facts.get("aliases", []), *facts.get("sibling_tools", [])}
    allowed |= {p["name"] for p in facts.get("params", [])}
    for p in facts.get("params", []):
        allowed |= {str(v) for v in (p.get("enum") or []) if v is not None}
    allowed |= {r for reasons in facts.get("responses", {}).values() for r in reasons}
    allowed |= {"always_approve"}
    errors = [f"unknown identifier {m}" for m in sorted(set(_IDENT.findall(text))) if m not in allowed]
    codes = set(facts.get("responses", {}))
    errors += [f"undeclared status code {c}" for c in sorted(set(_STATUS.findall(text))) if c not in codes]
    return errors


def _parse(text: str | None) -> dict[str, str] | None:
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("title"), str) or not isinstance(data.get("description"), str):
        return None
    return {"title": data["title"].strip(), "description": data["description"].strip()}


def _backend(llm: Any):
    if hasattr(llm, "step"):
        return llm
    from fabri.config import _normalize_llm_roles
    from fabri.runtime import build_llm

    return build_llm(_normalize_llm_roles({"llm": dict(llm)}), [])


def describe_with_llm(
    setup: Any,
    llm: Any,
    *,
    policy: ToolPolicy | None = None,
    global_allow: Iterable[str] | None = None,
    names: Iterable[str] | None = None,
    transport=None,
    state_dir: str | Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Rewrite titles and descriptions for a setup's tools. Returns
    ``{written, cached, fallback}`` where fallback maps a tool to why the
    deterministic text was kept."""
    from fabri.setups.resolver import ResolvedSetup, resolve_setup
    from fabri.setups.toolset import resolve_tools

    resolved = setup if isinstance(setup, ResolvedSetup) else resolve_setup(setup)
    toolset = resolve_tools(resolved, policy=policy, global_allow=global_allow, transport=transport,
                            state_dir=state_dir, use_cache=False)
    directory = state_dir_for(resolved, state_dir)
    state = read_state(directory, "descriptions.json")
    cache = dict(state.get("entries") or {})
    probes = read_state(directory, "probe.json").get("tools") or {}
    backend = _backend(llm)
    report: dict[str, Any] = {"written": [], "cached": [], "fallback": {}}
    wanted = set(names) if names is not None else None
    for tool in toolset.tools.values():
        if wanted is not None and tool.name not in wanted:
            continue
        key = describe_cache_key(tool, probes.get(tool.name))
        if key in cache and not force:
            report["cached"].append(tool.name)
            continue
        entry = toolset.catalog.entries[tool.name]
        siblings = [t.name for t in toolset.tools.values()
                    if t is not tool and t.operation and tool.operation and t.source == tool.source
                    and t.operation.resource == tool.operation.resource]
        facts = _facts(tool, entry, siblings)
        user = "Input:\n" + json.dumps(facts, indent=1, default=str)
        result, reason = None, "no reply"
        for attempt in range(2):
            reply = backend.step(SYSTEM, [{"role": "user", "content": user}])
            parsed = _parse(getattr(reply, "final_text", None))
            if parsed is None:
                reason = "reply was not the requested JSON"
                break
            if len(parsed["title"]) > TITLE_CAP or len(parsed["description"]) > DESCRIPTION_CAP:
                reason = "reply was over the length caps"
                break
            errors = grounding_errors(parsed["title"] + "\n" + parsed["description"], facts)
            if errors:
                reason = "not grounded: " + "; ".join(errors[:5])
                break
            slop = slop_report(parsed["title"] + "\n" + parsed["description"])
            if slop.total and attempt == 0:
                user += ("\n\nYour last reply had writing tells: "
                         + ", ".join(f"{k} ({', '.join(v)})" for k, v in slop.examples.items())
                         + ". Rewrite without them.")
                reason = "writing tells after one rewrite"
                continue
            if slop.total:
                reason = "writing tells after one rewrite"
                break
            result = parsed
            break
        if result is None:
            report["fallback"][tool.name] = reason
            continue
        cache[key] = {**result, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        report["written"].append(tool.name)
    if directory is not None and report["written"]:
        write_state(directory, "descriptions.json", {"version": 1, "entries": cache})
    return report
