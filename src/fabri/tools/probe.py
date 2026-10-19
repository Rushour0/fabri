"""Opt-in probing: call the safe operations of a tool set to learn what the
API returns, then keep a redacted sample and the ids, slugs and enum-like
values as `options` in the catalog.

Rules:

- Only GET and OPTIONS operations (and ``http`` tools declared with GET) are
  called, plus operations marked ``x-fabri-probe-safe: true``. Nothing else,
  ever, even when named in `ops`.
- Every call carries the policy's dry-run header (``X-Dry-Run: true`` when the
  policy sets none) and the source's own dry-run header if it has one.
- Calls are bounded: a timeout per call and at most `max_calls_per_op` per
  operation.
- Every captured value is truncated and redacted. Keys that look like
  personal data or credentials (email, phone, token, password, address and
  similar) are never stored, and neither are values that look like an email,
  a phone number or a credential.

Results land in ``probe.json`` in the setup's state directory with a
timestamp, and the catalog picks them up on the next resolve.
``refresh_probes`` is the entry point for a scheduler or a periodic job.
"""
from __future__ import annotations

import dataclasses
import re
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import httpx

from fabri.tools.catalog import read_state, state_dir_for, write_state
from fabri.tools.credential_store import CredentialStore
from fabri.tools.policy import ToolPolicy

DEFAULT_DRY_RUN_HEADER = {"name": "X-Dry-Run", "value": "true"}
SAFE_METHODS = frozenset({"GET", "OPTIONS"})
DENY_KEY_PARTS = ("email", "phone", "mobile", "token", "secret", "password", "passwd", "apikey",
                  "authorization", "cookie", "session", "ssn", "address", "birth", "iban", "creditcard",
                  "cardnumber", "firstname", "lastname", "fullname", "credential", "privatekey")
DENY_KEYS_EXACT = frozenset({"ip", "dob", "card", "cvv", "pin", "auth", "key"})
OPTION_KEYS = frozenset({"id", "slug", "uuid", "key", "code", "status", "state", "type", "kind"})
MAX_STRING = 64
MAX_OPTIONS = 20
REDACTED = "[redacted]"
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(\+\d[\d\s().-]{6,}\d)|(\(\d{3}\)\s?\d{3}[\s.-]?\d{4})|(\b\d{3}[\s.-]\d{3}[\s.-]\d{4}\b)")


def _norm(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def denied_key(key: str) -> bool:
    norm = _norm(key)
    return norm in DENY_KEYS_EXACT or any(part in norm for part in DENY_KEY_PARTS)


def _secret_value(text: str) -> bool:
    from fabri.tools.registration import looks_like_secret

    if _EMAIL.search(text):
        return True
    match = _PHONE.search(text)
    if match and sum(c.isdigit() for c in match.group(0)) >= 7:
        return True
    return looks_like_secret(text)


def sanitize(value: Any, depth: int = 0) -> Any:
    """A redacted, truncated copy: at most 3 list items, depth 4, strings
    cut at 64 chars, denylisted keys and secret-looking values replaced."""
    if depth > 4:
        return "..."
    if isinstance(value, Mapping):
        return {str(k): (REDACTED if denied_key(str(k)) else sanitize(v, depth + 1)) for k, v in list(value.items())[:30]}
    if isinstance(value, list):
        return [sanitize(v, depth + 1) for v in value[:3]]
    if isinstance(value, str):
        if _secret_value(value):
            return REDACTED
        return value if len(value) <= MAX_STRING else value[:MAX_STRING] + "..."
    return value


def shape(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "..."
    if isinstance(value, Mapping):
        return {str(k): shape(v, depth + 1) for k, v in list(value.items())[:30]}
    if isinstance(value, list):
        return [shape(value[0], depth + 1)] if value else []
    return type(value).__name__ if value is not None else "null"


def _records(data: Any) -> list[Mapping[str, Any]]:
    if isinstance(data, list):
        return [r for r in data if isinstance(r, Mapping)]
    if isinstance(data, Mapping):
        for key in ("data", "items", "results", "records", "objects"):
            if isinstance(data.get(key), list):
                return _records(data[key])
        return [data]
    return []


def _option_key(key: str) -> bool:
    lower = key.lower()
    return lower in OPTION_KEYS or lower.endswith("_id") or key.endswith("Id") or lower.endswith("_slug")


def capture_options(data: Any) -> dict[str, list]:
    """Values of id-like and enum-like fields across the response records."""
    records = _records(data)
    found: dict[str, list] = {}
    for record in records[:200]:
        for key, value in record.items():
            if denied_key(str(key)) or not isinstance(value, (str, int)) or isinstance(value, bool):
                continue
            if isinstance(value, str) and (len(value) > MAX_STRING or _secret_value(value)):
                continue
            bucket = found.setdefault(str(key), [])
            if value not in bucket:
                bucket.append(value)
    out = {}
    for key, values in found.items():
        enum_like = all(isinstance(v, str) for v in values) and len(records) >= 2 and len(values) <= 10 \
            and len(values) < len(records)
        if _option_key(key) or enum_like:
            out[key] = values[:MAX_OPTIONS]
    return out


def _param_candidates(param: str, resource: str) -> list[tuple[str, str]]:
    """(resource, field) pairs whose captured values fit `param`: the field of
    the same name on this resource, this resource's `id` for ``id`` /
    ``<resource>Id`` / ``<resource>_id``, and another resource's `id` for
    ``<other>_id``."""
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", param).lower()
    last = resource.rsplit("_", 1)[-1]
    out = [(resource, param), (resource, snake)]
    for suffix in ("id", "slug"):
        if snake == suffix or snake in (f"{resource}_{suffix}", f"{last}_{suffix}"):
            out.append((resource, suffix))
        elif snake.endswith(f"_{suffix}"):
            out.append((snake[: -len(suffix) - 1], suffix))
    return list(dict.fromkeys(out))


def _lookup(resource_options: Mapping[tuple[str, str], Mapping[str, list]], source: str, param: str,
            resource: str) -> list | None:
    for res, key in _param_candidates(param, resource):
        values = resource_options.get((source, res), {}).get(key)
        if values:
            return values
    return None


def _probeable(tool) -> bool:
    if tool.operation is not None:
        return tool.operation.method in SAFE_METHODS or tool.operation.probe_safe
    return tool.kind == "http" and (tool.method or "").upper() in SAFE_METHODS


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def probe_tools(
    setup: Any,
    *,
    policy: ToolPolicy | None = None,
    global_allow: Iterable[str] | None = None,
    ops: Iterable[str] | None = None,
    max_calls_per_op: int = 3,
    timeout_s: float = 5.0,
    transport: httpx.BaseTransport | None = None,
    store: CredentialStore | None = None,
    state_dir: str | Path | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """Probe the safe operations of a setup's tool set. Returns a report
    ``{probed, skipped, calls, state_file}`` and, with `write`, merges the
    results into ``probe.json``."""
    from fabri.setups.resolver import ResolvedSetup, resolve_setup
    from fabri.setups.toolset import _build

    resolved = setup if isinstance(setup, ResolvedSetup) else resolve_setup(setup)
    pol = policy or ToolPolicy.permissive()
    if global_allow is not None:
        pol = pol.with_allow(global_allow)
    if not pol.dry_run_header:
        pol = dataclasses.replace(pol, dry_run_header=DEFAULT_DRY_RUN_HEADER)
    # Rebuild the sources with a short timeout and dry-run headers on.
    capped_sources = []
    for item in resolved.tools["sources"]:
        decl = dict(item["decl"])
        if decl.get("kind") not in ("openapi", "http"):
            continue
        decl["timeout_s"] = min(float(decl.get("timeout_s") or timeout_s), timeout_s)
        capped_sources.append({**item, "decl": decl})
    probe_setup = dataclasses.replace(resolved, tools={**resolved.tools, "sources": capped_sources})
    toolset = _build(probe_setup, pol, transport, store, True)

    wanted = None
    if ops is not None:
        wanted = set()
        for name in ops:
            tool = toolset.get(name)
            if tool is not None:
                wanted.add(tool.name)
    results: dict[str, dict[str, Any]] = {}
    skipped: dict[str, str] = {}
    calls = 0
    resource_options: dict[tuple[str, str], dict[str, list]] = {}

    def remember(tool, options: Mapping[str, list]) -> None:
        if tool.operation is None:
            return
        bucket = resource_options.setdefault((tool.source, tool.operation.resource), {})
        for key, values in options.items():
            merged = bucket.setdefault(key, [])
            merged.extend(v for v in values if v not in merged)

    candidates = [t for t in toolset.tools.values() if wanted is None or t.name in wanted]
    for tool in candidates:
        if not _probeable(tool):
            skipped[tool.name] = f"{tool.method or tool.kind} is not probe-safe"
    safe = [t for t in candidates if _probeable(t)]

    def required_params(tool) -> list[str]:
        return list((tool.manifest.input_schema or {}).get("required") or [])

    # Pass 1: operations with no required params (collections). Pass 2: the rest.
    for tool in sorted(safe, key=lambda t: (bool(required_params(t)), t.name)):
        required = required_params(tool)
        arg_sets: list[dict[str, Any]] = []
        if not required:
            arg_sets = [{}]
        elif tool.operation is not None:
            values_per_param = {}
            for param in required:
                values = _lookup(resource_options, tool.source, param, tool.operation.resource)
                if not values:
                    break
                values_per_param[param] = values
            if len(values_per_param) == len(required):
                for i in range(max_calls_per_op):
                    if any(i >= len(v) for v in values_per_param.values()):
                        break
                    arg_sets.append({p: v[i] for p, v in values_per_param.items()})
        if not arg_sets:
            skipped[tool.name] = f"no known values for required params {required}"
            continue
        examples, statuses, options, shapes = [], [], {}, None
        for args in arg_sets[:max_calls_per_op]:
            out = tool.handler(args)
            calls += 1
            result = out.get("result") or {}
            statuses.append(result.get("code"))
            data = result.get("data")
            if out.get("ok"):
                shapes = shapes or shape(data)
                examples.append({"args": sanitize(args), "response": sanitize(data)})
                for key, values in capture_options(data).items():
                    merged = options.setdefault(key, [])
                    merged.extend(v for v in values if v not in merged)
        remember(tool, options)
        results[tool.name] = {"probed_at": _now(), "statuses": statuses, "shape": shapes,
                              "examples": examples[:2], "captured": {k: v[:MAX_OPTIONS] for k, v in options.items()}}

    # Map captured values onto every tool's params (write ops included, never called).
    for tool in toolset.tools.values():
        props = (tool.manifest.input_schema or {}).get("properties") or {}
        if tool.operation is None:
            continue
        param_options = {}
        for param in props:
            values = _lookup(resource_options, tool.source, param, tool.operation.resource)
            if values:
                param_options[param] = values[:MAX_OPTIONS]
        if param_options:
            results.setdefault(tool.name, {})["options"] = param_options

    directory = state_dir_for(resolved, state_dir)
    state_file = None
    if write and directory is not None:
        state = read_state(directory, "probe.json")
        tools_state = dict(state.get("tools") or {})
        for name, entry in results.items():
            tools_state[name] = {**tools_state.get(name, {}), **entry}
        state_file = str(write_state(directory, "probe.json", {"version": 1, "updated_at": _now(), "tools": tools_state}))
    return {"probed": sorted(n for n, r in results.items() if "probed_at" in r), "skipped": skipped,
            "calls": calls, "state_file": state_file, "results": results}


def refresh_probes(layers: Any, **kwargs: Any) -> dict[str, Any]:
    """Probe and write, for a scheduler or periodic job. Same arguments as
    probe_tools."""
    kwargs.setdefault("write", True)
    return probe_tools(layers, **kwargs)
