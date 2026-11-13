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
      - kind: http                 # one mail message as the body
        name: mail_send
        url: https://mail.test/upload/send
        method: POST
        query: {uploadType: media}  # literal query pairs sent on every call
        body_format: rfc822        # to, cc, bcc, subject, body -> a message/rfc822 body
      - kind: mcp                  # an MCP server (stdio `command` or http `url`)
        name: files
        command: [mcp-files, /srv/data]
        env_mode: allowlist        # the default: a short list of host variables plus `env`
                                   # (`inherit` opts in to the whole host environment)
        approval_mode: tighten     # the class can only go up from fabri's own pick
        catalog: github            # id of a built-in connector: applies its name-based minimum
      - kind: python               # an in-process callable
        name: score_lead
        callable: myapp.tools:score_lead
        input_schema: {type: object}

`build_tool_source(decl)` returns ``[(manifest, handler), ...]`` ready for
``ToolRegistry.register_callable``. `register_tool_sources` does that for a
list. A ToolPolicy, when given, is enforced while building.

An http source with ``body_format: rfc822`` takes the arguments ``to``,
``cc``, ``bcc``, ``subject`` and ``body`` and sends one plain text mail
message as a ``message/rfc822`` body (``fabri.tools.mail_message``). Under
the broker the run sends those arguments and the broker builds the message
itself, so the approval card shows the recipients, subject and body as text.

An http or openapi tool whose request URL is on a catalog connector's host
(slack.com, for example) gets that connector's approval floor whatever the
declaration says; see ``_apply_floors``.

Under ``fabri serve`` a run holds no stored tokens. When a credential broker
client is passed (``broker=``), a source that names a ``secret_ref`` sends
its requests through the broker in the service process instead: http and
openapi tools hand the broker the tool name, the method and the raw path
parameters, and an http MCP source posts its JSON-RPC bodies through it.
Those handlers carry ``approval_gate = "broker"`` and
``register_tool_sources`` marks them on the registry, since the broker asks
a human itself before any ``always_approve`` request. Sources with only
``env:`` or literal headers keep sending in this process.
"""
from __future__ import annotations

import contextvars
import importlib
import logging
import os
import re
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from fabri.tools.approval import APPROVAL_CLASSES, check_approval, mcp_approval, safe_approval, stricter
from fabri.tools.credential_store import CredentialStore, credential_env_names
from fabri.tools.http_call import (
    PathParamError,
    _has_dot_segment,
    check_header_specs,
    credential_refused_hint,
    dry_run_headers,
    render_path_value,
    resolve_headers,
    secret_ref_provider,
    send,
    send_prepared,
)
from fabri.tools.mail_message import CONTENT_TYPE as MAIL_CONTENT_TYPE
from fabri.tools.mail_message import MessageError, build_message
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

if TYPE_CHECKING:
    from fabri.tools.credential_broker_client import BrokerClient

logger = logging.getLogger("fabri.tools.sources")

Handler = Callable[[dict], dict]
TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
_PARAM = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

COMMON_KEYS = frozenset({"kind", "name", "description", "approval", "enabled", "timeout_s", "version", "title",
                         "approval_mode", "catalog"})
KIND_KEYS: dict[str, frozenset[str]] = {
    "openapi": frozenset({"spec", "base_url", "allow", "deny", "methods", "tags", "headers",
                          "dry_run_header", "description_cap", "naming", "names", "strip_prefixes"}),
    "http": frozenset({"url", "method", "input_schema", "headers", "dry_run_header", "query", "body_format"}),
    "mcp": frozenset({"command", "url", "env", "headers", "env_mode"}),
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
    # The class fabri would pick with no override in the declaration.
    safe_approval: str = "write"
    # True when the handler sends through a live credential broker client.
    broker_gated: bool = False


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
        check_header_specs(decl.get("env"))
    except ValueError as exc:
        raise ToolSourceError(f"source {name}: {exc}") from None
    if kind == "openapi" and not decl.get("spec"):
        raise ToolSourceError(f"openapi source {name}: `spec` is required")
    if kind == "http":
        if not decl.get("url"):
            raise ToolSourceError(f"http source {name}: `url` is required")
        _check_schema(decl.get("input_schema"), name)
        _check_http_body(decl, name)
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
        policy.check_secret_refs(name, secret_refs(decl))
    if decl.get("approval_mode") not in (None, "tighten"):
        raise ToolSourceError(f"source {name}: approval_mode must be 'tighten' (got {decl['approval_mode']!r})")
    catalog = decl.get("catalog")
    if catalog is not None:
        from fabri.tools.connectors import CONNECTORS

        if not isinstance(catalog, str) or catalog not in CONNECTORS:
            raise ToolSourceError(f"source {name}: unknown catalog id {catalog!r}")
    if kind == "mcp" and decl.get("env_mode") not in (None, "inherit", "allowlist"):
        raise ToolSourceError(f"mcp source {name}: env_mode must be 'inherit' or 'allowlist' (got {decl['env_mode']!r})")
    unknown = set(decl) - COMMON_KEYS - KIND_KEYS[kind]
    return [f"source {name}: unknown keys {sorted(unknown)}"] if unknown else []


HTTP_BODY_FORMATS = ("json", "rfc822")


def _check_http_body(decl: Mapping[str, Any], name: str) -> None:
    """``query`` is a mapping of literal text pairs; ``body_format`` is json
    (the default) or rfc822, which needs a method that sends a body."""
    query = decl.get("query")
    if query is not None:
        if not isinstance(query, Mapping) or not all(
                isinstance(k, str) and k and isinstance(v, str) for k, v in query.items()):
            raise ToolSourceError(f"http source {name}: `query` must map names to text values")
    body_format = decl.get("body_format")
    if body_format is not None and body_format not in HTTP_BODY_FORMATS:
        raise ToolSourceError(f"http source {name}: `body_format` must be one of {', '.join(HTTP_BODY_FORMATS)}")
    if body_format == "rfc822":
        if str(decl.get("method") or "GET").upper() not in ("POST", "PUT"):
            raise ToolSourceError(f"http source {name}: body_format rfc822 needs method POST or PUT")
        if _PARAM.search(str(decl.get("url") or "")):
            raise ToolSourceError(f"http source {name}: body_format rfc822 takes no path parameters")


def _check_schema(schema: Any, name: str) -> None:
    if schema is None:
        return
    if not isinstance(schema, Mapping) or schema.get("type", "object") != "object":
        raise ToolSourceError(f"source {name}: input_schema must be a JSON Schema object (type: object)")
    if "properties" in schema and not isinstance(schema["properties"], Mapping):
        raise ToolSourceError(f"source {name}: input_schema.properties must be a mapping")
    if "required" in schema and not isinstance(schema["required"], list):
        raise ToolSourceError(f"source {name}: input_schema.required must be a list")


def secret_refs(decl: Mapping[str, Any]) -> list[str]:
    """Every ``secret_ref`` a declaration's headers and env name."""
    refs = []
    for specs in (decl.get("headers"), decl.get("env")):
        for spec in (specs or {}).values() if isinstance(specs, Mapping) else ():
            if isinstance(spec, Mapping) and "secret_ref" in spec:
                refs.append(str(spec["secret_ref"]))
    return refs


def build_source_tools(
    decl: Mapping[str, Any],
    *,
    base_dir: str | Path | None = None,
    policy: ToolPolicy | None = None,
    transport: httpx.BaseTransport | None = None,
    store: CredentialStore | None = None,
    dry_run: bool = False,
    broker: BrokerClient | None = None,
) -> list[SourceTool]:
    """Build one declaration into SourceTools (manifest, handler, origin).

    With `broker` set and a ``secret_ref`` in the declaration, http and
    openapi tools and an http MCP source send through the broker, and each
    built tool is flagged ``broker_gated``. A stdio MCP or python source
    with a ``secret_ref`` cannot be brokered and raises ToolSourceError.
    The approval classes in the manifests are the same either way."""
    validate_source_decl(decl, policy)
    kind = decl["kind"]
    uses_broker = broker is not None and bool(secret_refs(decl))
    if uses_broker and kind == "mcp" and decl.get("command"):
        raise ToolSourceError(f"source {decl['name']}: a stdio MCP server cannot use a stored credential under "
                              "fabri serve; use the http transport, or set FABRI_CRED_SNAPSHOT=1 on the service")
    if uses_broker and kind == "python":
        raise ToolSourceError(f"source {decl['name']}: a python source cannot use a stored credential under "
                              "fabri serve; use an http or openapi source, or set FABRI_CRED_SNAPSHOT=1 on the "
                              "service")
    builder = {"openapi": _build_openapi, "http": _build_http, "mcp": _build_mcp, "python": _build_python}[kind]
    dry: dict[str, str] = {}
    if dry_run:
        # The declaration's header and the caller's policy header are both
        # sent, so a host can always mark dry runs its own way.
        dry.update(dry_run_headers(decl.get("dry_run_header")))
        dry.update(dry_run_headers(policy.dry_run_header if policy else None))
    grants = policy.grants_for(decl["name"]) if policy is not None else None
    if grants is not None and not uses_broker:
        # The build-time check covers the declaration; this covers the store
        # at call time, so the source can never resolve anything else. With
        # the broker the run resolves nothing; the broker holds the grants.
        from fabri.tools.credential_store import ScopedCredentialStore
        from fabri.tools.secret_refs import default_credential_store

        store = ScopedCredentialStore(store or default_credential_store(), grants, actor=decl["name"])
    built = builder(decl, base_dir=base_dir, policy=policy, transport=transport, store=store, dry=dry,
                    broker=broker if uses_broker else None)
    if uses_broker:
        for tool in built:
            tool.broker_gated = True
            tool.handler.approval_gate = "broker"  # type: ignore[attr-defined]
    _apply_floors(decl, built)
    return built


def _request_url(tool: SourceTool) -> str | None:
    """The URL a built http or openapi tool sends to, placeholders and all."""
    if tool.kind == "http":
        return tool.path
    if tool.kind == "openapi" and tool.path is not None:
        return str(tool.extra.get("base_url") or "").rstrip("/") + tool.path
    return None


def _raise_to(tool: SourceTool, floor: str) -> None:
    tool.manifest.approval = stricter(tool.manifest.approval or "write", floor)
    # So approval_warnings and lint see the true default.
    tool.safe_approval = stricter(tool.safe_approval, floor)


def _apply_floors(decl: Mapping[str, Any], built: list[SourceTool]) -> None:
    """Raise approval classes to the floors fabri trusts.

    http and openapi tools: the floor is bound to the request target. When
    the URL's host belongs to a catalog connector, the call is always_approve
    unless its (method, path) is in that connector's read list, and the class
    can never drop below fabri's own pick. This does not look at the tool
    name, the ``catalog`` key or ``approval_mode``, so editing or removing
    them in the YAML does not remove the floor. A URL whose host is a
    placeholder could reach any host, so it is treated the same way.

    MCP tools have no URL per tool, so their floor is tool-name based and
    keyed on the connector: an http MCP source is matched by the host of its
    server URL, a stdio MCP source only by its ``catalog`` marker. A stdio
    MCP source with its markers removed is the operator's own responsibility;
    lint warns about any class looser than fabri's pick.

    Any source may also opt in with ``approval_mode: tighten`` (the class can
    only go up from fabri's own pick) and ``catalog: <id>`` (that connector's
    name-based ``minimum`` applies). Both still work as before.
    """
    from fabri.tools import connectors

    tighten = decl.get("approval_mode") == "tighten"
    marker = decl.get("catalog")
    mcp_catalogs: list[str] = []
    if decl.get("kind") == "mcp" and decl.get("url"):
        owner = connectors.catalog_for_url(str(decl["url"]))
        if owner:
            mcp_catalogs.append(owner)
    for tool in built:
        url = _request_url(tool)
        bound = bool(mcp_catalogs)
        floor = None
        if url is not None:
            owner, floor = connectors.request_floor(url, tool.method)
            if owner is None and _templated_host(url):
                owner, floor = "?", "always_approve"
            bound = owner is not None
        if tighten or bound:
            # Whatever the layer wrote, the class can only go up from fabri's own pick.
            tool.manifest.approval = stricter(tool.manifest.approval or "write", tool.safe_approval)
        if floor:
            _raise_to(tool, floor)
        for kind in dict.fromkeys([*([marker] if marker else []), *mcp_catalogs]):
            minimum = connectors.catalog_minimum(kind, tool.manifest.name, tool.method)
            if minimum:
                _raise_to(tool, minimum)


def _templated_host(url: str) -> bool:
    """True when the host part of `url` holds a ``{placeholder}`` that the
    caller fills in at call time."""
    rest = url.split("://", 1)[-1]
    return "{" in re.split(r"[/?#]", rest, maxsplit=1)[0]


def build_tool_source(decl: Mapping[str, Any], **kwargs: Any) -> list[tuple[ToolManifest, Handler]]:
    """Build one declaration into (manifest, handler) pairs. Keyword
    arguments are those of build_source_tools."""
    return [(t.manifest, t.handler) for t in build_source_tools(decl, **kwargs)]


def register_tool_sources(registry: Any, decls: Iterable[Mapping[str, Any]], **kwargs: Any) -> list[str]:
    """Build and register every declaration. A source that fails to build is
    skipped and its error returned, so one bad source never takes down the
    rest.

    A tool built to send through a live broker client (``broker=``) is
    marked with ``registry.mark_broker_gated`` right after it is
    registered, so the registry skips its own approval prompt for it (the
    broker asks instead). No tool is marked when no broker is passed. A
    registry stand-in without ``mark_broker_gated`` marks nothing, which
    keeps the normal approver in front of the tool."""
    errors: list[str] = []
    for decl in decls:
        try:
            for tool in build_source_tools(decl, **kwargs):
                registry.register_callable(tool.manifest, tool.handler)
                mark = getattr(registry, "mark_broker_gated", None)
                if tool.broker_gated and callable(mark):
                    mark(tool.manifest.name)
        except Exception as exc:  # noqa: BLE001 -- reported, not fatal
            errors.append(f"{(decl or {}).get('name', '?')}: {exc}")
    return errors


# ---------------------------------------------------------------- builders


def _build_openapi(decl, *, base_dir, policy, transport, store, dry, broker=None) -> list[SourceTool]:
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
    client = None if broker is not None else httpx.Client(transport=transport, timeout=timeout)
    out = []
    for op in ops:
        sender = broker.sender(decl["name"], op.name) if broker is not None else None
        handler = operation_handler(op, base_url=base_url, client=client, headers=decl.get("headers"),
                                    store=store, extra_headers=dry, timeout_s=timeout, sender=sender)
        out.append(SourceTool(op.manifest(timeout), handler, "openapi", decl["name"], op.method, op.path, op,
                              {"base_url": base_url}, safe_approval=op.safe_approval))
    return out


def _build_http(decl, *, base_dir, policy, transport, store, dry, broker=None) -> list[SourceTool]:
    name, url = decl["name"], str(decl["url"])
    method = str(decl.get("method") or "GET").upper()
    timeout = float(decl.get("timeout_s") or 30.0)
    schema = dict(decl.get("input_schema") or {"type": "object", "properties": {}})
    placeholders = _PARAM.findall(url)
    headers = decl.get("headers")
    query = {str(k): str(v) for k, v in (decl.get("query") or {}).items()}
    body_format = str(decl.get("body_format") or "json")
    # With the broker the run holds no token and opens no connection: the
    # broker renders the URL from its bound template with the raw values.
    sender = broker.sender(name, name) if broker is not None else None
    client = None if sender is not None else httpx.Client(transport=transport, timeout=timeout)

    def mail_handler(args: dict) -> dict:
        # body_format rfc822: the arguments become one message/rfc822 body.
        # The broker builds the same bytes itself from these arguments, so a
        # bad message is refused here first and never leaves the run.
        try:
            content = build_message(args)
        except MessageError as e:
            return tool_error(f"{name}: {e}", result={"status": "error", "code": None})
        if sender is not None:
            return sender(method, url, path_params={}, params=query or None, json_body=dict(args),
                          extra_headers=dry, timeout_s=timeout, tool_name=name, floor_url=url)
        if _has_dot_segment(url):
            return tool_error("request refused: the URL path holds a dot segment",
                              result={"status": "error", "code": None})
        request = client.build_request(method, url, params=query or None, content=content,
                                       headers={"Content-Type": MAIL_CONTENT_TYPE})
        return send_prepared(client, request, headers=headers, store=store, extra_headers=dry,
                             timeout_s=timeout, tool_name=name, floor_url=url)

    def handler(args: dict) -> dict:
        args = dict(args or {})
        if body_format == "rfc822":
            return mail_handler(args)
        target = url
        path_params: dict[str, Any] = {}
        for key in placeholders:
            if key not in args:
                return tool_error(f"{name}: missing path parameter {key!r}", result={"status": "error", "code": None})
            value = args.pop(key)
            try:
                rendered = render_path_value(key, value)
            except PathParamError as e:
                return tool_error(f"{name}: {e}", result={"status": "error", "code": None})
            path_params[key] = value
            target = target.replace("{" + key + "}", rendered)
        reads = method in ("GET", "HEAD", "OPTIONS", "DELETE")
        # The declared query pairs win over an argument of the same name.
        params = {**args, **query} if reads else (query or None)
        if sender is not None:
            body = {"params": params} if reads else {"params": params, "json_body": args}
            return sender(method, target, path_params=path_params, extra_headers=dry, timeout_s=timeout,
                          tool_name=name, floor_url=url, **body)
        if reads:
            return send(client, method, target, headers=headers, store=store, extra_headers=dry,
                        params=params, timeout_s=timeout, tool_name=name, floor_url=url)
        return send(client, method, target, headers=headers, store=store, extra_headers=dry,
                    params=params, json_body=args, timeout_s=timeout, tool_name=name, floor_url=url)

    safe = safe_approval(method, name, url)
    manifest = ToolManifest(
        name=name, description=str(decl.get("description") or f"{method} {url}"), command=[],
        input_schema=schema, output_schema={"type": "object"}, timeout_s=timeout,
        approval=_approval_for(decl, name, safe), title=decl.get("title"),
    )
    return [SourceTool(manifest, handler, "http", name, method, url, safe_approval=safe,
                       extra={"query": query, "body_format": body_format})]


def _build_python(decl, *, base_dir, policy, transport, store, dry, broker=None) -> list[SourceTool]:
    name, target = decl["name"], str(decl["callable"])
    module_name, _, attr = target.partition(":")
    if not attr:
        module_name, _, attr = target.rpartition(".")
    fn = getattr(importlib.import_module(module_name), attr)
    if not callable(fn):
        raise ToolSourceError(f"python source {name}: {target} is not callable")

    timeout = float(decl.get("timeout_s") or 30.0)

    def call(args: dict) -> dict:
        result = fn(dict(args or {}))
        if isinstance(result, dict) and "ok" in result:
            return result
        return tool_ok({"status": "ok", "data": result})

    def handler(args: dict) -> dict:
        # A thread cannot be killed, so a call past the timeout keeps running
        # in the background; the agent gets an error and moves on.
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"tool-{name}")
        try:
            # In a copy of the run's context, so a credential lookup inside
            # the callable sees the run's install database.
            return pool.submit(contextvars.copy_context().run, call, args).result(timeout=timeout)
        except FuturesTimeout:
            return tool_error(f"{name}: timed out after {timeout:g}s")
        finally:
            pool.shutdown(wait=False)

    manifest = ToolManifest(
        name=name, description=str(decl.get("description") or (fn.__doc__ or name).strip().splitlines()[0]),
        command=[], input_schema=dict(decl.get("input_schema") or {"type": "object"}),
        output_schema={"type": "object"}, timeout_s=timeout,
        approval=_approval_for(decl, name, "write"), title=decl.get("title"),
    )
    return [SourceTool(manifest, handler, "python", name)]


def _build_mcp(decl, *, base_dir, policy, transport, store, dry, broker=None) -> list[SourceTool]:
    from fabri.tools.mcp_client import build_mcp_tools

    cfg: dict[str, Any] = {"name": decl["name"], "timeout_s": float(decl.get("timeout_s") or 30.0)}
    if decl.get("url") and broker is not None:
        # The broker applies the declared header specs; the run sends only
        # the literal dry-run headers, which the broker checks like any
        # other extra header.
        source, url, extra = decl["name"], str(decl["url"]), dict(dry)

        def poster(*, body: bytes, headers: Mapping[str, str], timeout_s: float = 30.0):
            return broker.post_mcp(source=source, url=url, body=body, headers={**dict(headers or {}), **extra},
                                   timeout_s=timeout_s)

        cfg["url"] = url
        cfg["poster"] = poster
        provider = secret_ref_provider(decl.get("headers"))
        if provider:
            cfg["credential_hint"] = credential_refused_hint(provider)
    elif decl.get("url"):
        cfg["url"] = decl["url"]
        specs = decl.get("headers")
        # Resolved once here so a missing credential fails the build, then
        # again before every request so a refreshed or replaced token is used.
        cfg["headers"] = {**resolve_headers(specs, store), **dry}
        cfg["header_source"] = lambda: {**resolve_headers(specs, store), **dry}
        provider = secret_ref_provider(specs)
        if provider:
            cfg["credential_hint"] = credential_refused_hint(provider)
    else:
        cfg["command"] = list(decl["command"])
        if decl.get("env"):
            cfg["env"] = resolve_headers(decl.get("env"), store)
        # Only an explicit `env_mode: inherit` passes the whole host
        # environment; the default is the allowlist, so a server must declare
        # the keys it needs under `env` (resolved above).
        cfg["inherit_env"] = decl.get("env_mode") == "inherit"
        if cfg["inherit_env"]:
            # The server may inherit only the FABRI_CRED_* names its own
            # declaration references; the run's other tokens are removed.
            # The allowlist mode needs none: `env` already carries the values.
            cfg["credential_env"] = credential_env_names(
                secret_refs(decl), actor=os.environ.get("FABRI_RUN_ACTOR") or None)
    cfg["transport"] = transport
    client, pairs = build_mcp_tools(
        cfg, approval_for=lambda remote, spec: mcp_approval(remote, str(spec.get("description") or ""),
                                                            spec.get("annotations")))
    out = []
    for manifest, handler in pairs:
        safe = manifest.approval or "write"
        manifest.approval = _approval_for(decl, manifest.name, safe)
        out.append(SourceTool(manifest, handler, "mcp", decl["name"], extra={"client": client}, safe_approval=safe))
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
