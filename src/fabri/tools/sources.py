"""One declaration format for every dynamic tool source.

    sources:
      - kind: openapi              # many tools from one OpenAPI 3 document
        name: pets
        spec: specs/petstore.yaml  # URL, path (relative to the declaring file) or inline mapping
        base_url: https://petstore.test
        methods: [GET, POST]
        headers: {Authorization: {secret_ref: "pets:prod", prefix: "Bearer "}}
        dry_run_header: {name: X-Dry-Run, value: "1"}
      - kind: http                 # one endpoint, one tool
        name: weather_now
        url: https://weather.test/now/{city}
        method: GET
        description: Current weather for a city.
        input_schema: {type: object, properties: {city: {type: string}}, required: [city]}
      - kind: mcp                  # an MCP server (stdio `command` or http `url`)
        name: files
        command: [mcp-files, /srv/data]
      - kind: python               # an in-process callable
        name: score_lead
        callable: myapp.tools:score_lead
        input_schema: {type: object}

`build_tool_source(decl)` returns ``[(manifest, handler), ...]`` ready for
``ToolRegistry.register_callable``. `register_tool_sources` does that for a
list. A ToolPolicy, when given, is enforced while building.
"""
from __future__ import annotations

import importlib
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from fabri.tools.approval import APPROVAL_CLASSES, check_approval, default_approval_for_method
from fabri.tools.credential_store import CredentialStore
from fabri.tools.http_call import check_header_specs, dry_run_headers, resolve_headers, send
from fabri.tools.manifest_schema import ToolManifest
from fabri.tools.openapi import (
    OpenAPIOperation,
    default_base_url,
    filter_operations,
    load_openapi,
    operation_handler,
    parse_operations,
)
from fabri.tools.policy import SOURCE_KINDS, PolicyError, ToolPolicy
from fabri.tools.result import tool_error, tool_ok

logger = logging.getLogger("fabri.tools.sources")

Handler = Callable[[dict], dict]
TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
_PARAM = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

COMMON_KEYS = frozenset({"kind", "name", "description", "approval", "enabled", "timeout_s", "version", "title"})
KIND_KEYS: dict[str, frozenset[str]] = {
    "openapi": frozenset({"spec", "base_url", "allow", "deny", "methods", "tags", "headers",
                          "dry_run_header", "description_cap", "naming", "names", "strip_prefixes"}),
    "http": frozenset({"url", "method", "input_schema", "headers", "dry_run_header"}),
    "mcp": frozenset({"command", "url", "env", "headers"}),
    "python": frozenset({"callable", "input_schema"}),
}


class ToolSourceError(ValueError):
    """A source declaration is malformed."""


@dataclass
class SourceTool:
    """One built tool plus where it came from."""

    manifest: ToolManifest
    handler: Handler
    kind: str
    source: str
    method: str | None = None
    path: str | None = None
    operation: OpenAPIOperation | None = None
    extra: dict = field(default_factory=dict)


def _approval_for(decl: Mapping[str, Any], tool_name: str, default: str) -> str:
    value = decl.get("approval")
    if isinstance(value, Mapping):
        value = value.get(tool_name)
    return check_approval(value or default)


def validate_source_decl(decl: Any, policy: ToolPolicy | None = None) -> list[str]:
    """Check a declaration's shape (and the policy, when given) without
    building it. Returns warnings (unknown keys); raises ToolSourceError or
    PolicyError for anything that would not build."""
    if not isinstance(decl, Mapping):
        raise ToolSourceError("a tool source must be a mapping")
    kind, name = decl.get("kind"), decl.get("name")
    if kind not in SOURCE_KINDS:
        raise ToolSourceError(f"tool source kind must be one of {', '.join(SOURCE_KINDS)} (got {kind!r})")
    if not isinstance(name, str) or not TOOL_NAME.fullmatch(name):
        raise ToolSourceError(f"tool source name {name!r} must match {TOOL_NAME.pattern}")
    approval = decl.get("approval")
    if isinstance(approval, Mapping):
        for value in approval.values():
            check_approval(value)
    elif approval is not None:
        check_approval(approval)
    try:
        check_header_specs(decl.get("headers"))
    except ValueError as exc:
        raise ToolSourceError(f"source {name}: {exc}") from None
    if kind == "openapi" and not decl.get("spec"):
        raise ToolSourceError(f"openapi source {name}: `spec` is required")
    if kind == "http":
        if not decl.get("url"):
            raise ToolSourceError(f"http source {name}: `url` is required")
        _check_schema(decl.get("input_schema"), name)
    if kind == "python":
        target = decl.get("callable")
        if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z_][\w.]*(:[A-Za-z_]\w*)?", target):
            raise ToolSourceError(f"python source {name}: `callable` must be a dotted path")
        _check_schema(decl.get("input_schema"), name)
    if kind == "mcp" and bool(decl.get("command")) == bool(decl.get("url")):
        raise ToolSourceError(f"mcp source {name}: set exactly one of `command` or `url`")
    if policy is not None:
        policy.check_kind(kind)
        if kind in ("http", "mcp"):
            policy.check_host(decl.get("url"), f"source {name}")
        if kind == "openapi":
            policy.check_host(decl.get("base_url"), f"source {name} base_url")
            spec = decl.get("spec")
            if isinstance(spec, str) and spec.startswith(("http://", "https://")):
                policy.check_host(spec, f"source {name} spec")
        if kind == "http" and str(decl.get("method") or "GET").upper() == "DELETE" and not policy.allow_delete:
            raise PolicyError(f"http source {name}: DELETE is not allowed by the policy")
        if kind == "python" and not policy.module_allowed(str(decl["callable"])):
            raise PolicyError(f"python source {name}: module is outside the policy's python_modules")
    unknown = set(decl) - COMMON_KEYS - KIND_KEYS[kind]
    return [f"source {name}: unknown keys {sorted(unknown)}"] if unknown else []


def _check_schema(schema: Any, name: str) -> None:
    if schema is None:
        return
    if not isinstance(schema, Mapping) or schema.get("type", "object") != "object":
        raise ToolSourceError(f"source {name}: input_schema must be a JSON Schema object (type: object)")
    if "properties" in schema and not isinstance(schema["properties"], Mapping):
        raise ToolSourceError(f"source {name}: input_schema.properties must be a mapping")
    if "required" in schema and not isinstance(schema["required"], list):
        raise ToolSourceError(f"source {name}: input_schema.required must be a list")


def build_source_tools(
    decl: Mapping[str, Any],
    *,
    base_dir: str | Path | None = None,
    policy: ToolPolicy | None = None,
    transport: httpx.BaseTransport | None = None,
    store: CredentialStore | None = None,
    dry_run: bool = False,
) -> list[SourceTool]:
    """Build one declaration into SourceTools (manifest, handler, origin)."""
    validate_source_decl(decl, policy)
    kind = decl["kind"]
    builder = {"openapi": _build_openapi, "http": _build_http, "mcp": _build_mcp, "python": _build_python}[kind]
    dry: dict[str, str] = {}
    if dry_run:
        # The declaration's header and the caller's policy header are both
        # sent, so a host can always mark dry runs its own way.
        dry.update(dry_run_headers(decl.get("dry_run_header")))
        dry.update(dry_run_headers(policy.dry_run_header if policy else None))
    return builder(decl, base_dir=base_dir, policy=policy, transport=transport, store=store, dry=dry)


def build_tool_source(decl: Mapping[str, Any], **kwargs: Any) -> list[tuple[ToolManifest, Handler]]:
    """Build one declaration into (manifest, handler) pairs. Keyword
    arguments are those of build_source_tools."""
    return [(t.manifest, t.handler) for t in build_source_tools(decl, **kwargs)]


def register_tool_sources(registry: Any, decls: Iterable[Mapping[str, Any]], **kwargs: Any) -> list[str]:
    """Build and register every declaration. A source that fails to build is
    skipped and its error returned, so one bad source never takes down the
    rest."""
    errors: list[str] = []
    for decl in decls:
        try:
            for tool in build_source_tools(decl, **kwargs):
                registry.register_callable(tool.manifest, tool.handler)
        except Exception as exc:  # noqa: BLE001 -- reported, not fatal
            errors.append(f"{(decl or {}).get('name', '?')}: {exc}")
    return errors


# ---------------------------------------------------------------- builders


def _build_openapi(decl, *, base_dir, policy, transport, store, dry) -> list[SourceTool]:
    timeout = float(decl.get("timeout_s") or 30.0)
    doc = load_openapi(decl["spec"], base_dir=base_dir, transport=transport, timeout_s=timeout)
    base_url = decl.get("base_url") or default_base_url(doc)
    if not base_url:
        raise ToolSourceError(f"openapi source {decl['name']}: no base_url and no absolute servers[].url")
    if policy is not None:
        policy.check_host(base_url, f"source {decl['name']} base_url")
    approval = decl.get("approval") if isinstance(decl.get("approval"), Mapping) else None
    ops = parse_operations(
        doc,
        naming=decl.get("naming") or "readable",
        names=decl.get("names"),
        strip_prefixes=decl.get("strip_prefixes") or ("api",),
        approval=approval,
        description_cap=int(decl.get("description_cap") or 1500),
    )
    ops = filter_operations(
        ops, allow=decl.get("allow"), deny=decl.get("deny"), methods=decl.get("methods"),
        tags=decl.get("tags"), allow_delete=policy.allow_delete if policy is not None else True,
    )
    if isinstance(decl.get("approval"), str):
        for op in ops:
            op.approval = check_approval(decl["approval"])
    client = httpx.Client(transport=transport, timeout=timeout)
    out = []
    for op in ops:
        handler = operation_handler(op, base_url=base_url, client=client, headers=decl.get("headers"),
                                    store=store, extra_headers=dry, timeout_s=timeout)
        out.append(SourceTool(op.manifest(timeout), handler, "openapi", decl["name"], op.method, op.path, op,
                              {"base_url": base_url}))
    return out


def _build_http(decl, *, base_dir, policy, transport, store, dry) -> list[SourceTool]:
    name, url = decl["name"], str(decl["url"])
    method = str(decl.get("method") or "GET").upper()
    timeout = float(decl.get("timeout_s") or 30.0)
    schema = dict(decl.get("input_schema") or {"type": "object", "properties": {}})
    placeholders = _PARAM.findall(url)
    client = httpx.Client(transport=transport, timeout=timeout)
    headers = decl.get("headers")

    def handler(args: dict) -> dict:
        args = dict(args or {})
        target = url
        for key in placeholders:
            if key not in args:
                return tool_error(f"{name}: missing path parameter {key!r}", result={"status": "error", "code": None})
            target = target.replace("{" + key + "}", quote(str(args.pop(key)), safe=""))
        if method in ("GET", "HEAD", "OPTIONS", "DELETE"):
            return send(client, method, target, headers=headers, store=store, extra_headers=dry,
                        params=args, timeout_s=timeout, tool_name=name)
        return send(client, method, target, headers=headers, store=store, extra_headers=dry,
                    json_body=args, timeout_s=timeout, tool_name=name)

    manifest = ToolManifest(
        name=name, description=str(decl.get("description") or f"{method} {url}"), command=[],
        input_schema=schema, output_schema={"type": "object"}, timeout_s=timeout,
        approval=_approval_for(decl, name, default_approval_for_method(method)),
        title=decl.get("title"),
    )
    return [SourceTool(manifest, handler, "http", name, method, url)]


def _build_python(decl, *, base_dir, policy, transport, store, dry) -> list[SourceTool]:
    name, target = decl["name"], str(decl["callable"])
    module_name, _, attr = target.partition(":")
    if not attr:
        module_name, _, attr = target.rpartition(".")
    fn = getattr(importlib.import_module(module_name), attr)
    if not callable(fn):
        raise ToolSourceError(f"python source {name}: {target} is not callable")

    def handler(args: dict) -> dict:
        result = fn(dict(args or {}))
        if isinstance(result, dict) and "ok" in result:
            return result
        return tool_ok({"status": "ok", "data": result})

    manifest = ToolManifest(
        name=name, description=str(decl.get("description") or (fn.__doc__ or name).strip().splitlines()[0]),
        command=[], input_schema=dict(decl.get("input_schema") or {"type": "object"}),
        output_schema={"type": "object"}, timeout_s=float(decl.get("timeout_s") or 30.0),
        approval=_approval_for(decl, name, "write"), title=decl.get("title"),
    )
    return [SourceTool(manifest, handler, "python", name)]


def _build_mcp(decl, *, base_dir, policy, transport, store, dry) -> list[SourceTool]:
    from fabri.tools.mcp_client import build_mcp_tools

    cfg: dict[str, Any] = {"name": decl["name"], "timeout_s": float(decl.get("timeout_s") or 30.0)}
    if decl.get("url"):
        cfg["url"] = decl["url"]
        cfg["headers"] = {**resolve_headers(decl.get("headers"), store), **dry}
    else:
        cfg["command"] = list(decl["command"])
        if decl.get("env"):
            cfg["env"] = resolve_headers(decl.get("env"), store)
    client, pairs = build_mcp_tools(cfg)
    out = []
    for manifest, handler in pairs:
        manifest.approval = _approval_for(decl, manifest.name, "write")
        out.append(SourceTool(manifest, handler, "mcp", decl["name"], extra={"client": client}))
    return out


__all__ = [
    "APPROVAL_CLASSES",
    "SourceTool",
    "ToolSourceError",
    "build_source_tools",
    "build_tool_source",
    "register_tool_sources",
    "validate_source_decl",
]
