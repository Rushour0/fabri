"""Turn an OpenAPI 3 document into fabri tools.

    from fabri.tools.openapi import openapi_tools
    pairs = openapi_tools("https://api.example.com/openapi.json",
                          base_url="https://api.example.com",
                          headers={"Authorization": {"secret_ref": "svc:prod", "prefix": "Bearer "}},
                          methods=["GET", "POST"])
    for manifest, handler in pairs:
        registry.register_callable(manifest, handler)

One operation becomes one tool:

- ``name``: a readable ``<resource>_<action>`` derived from the path and method
  (``GET /pets/{id}`` -> ``pet_get``), or the ``operationId`` with
  ``naming="operation_id"``. The other name is kept as an alias, so the
  registry resolves both.
- ``description``: summary, description, and one ``Responses:`` line with the
  status codes and any documented reason enums. Capped at 1,500 characters.
- ``input_schema``: path and query parameters merged with the JSON request
  body. ``$ref``, ``allOf``, ``oneOf``/``anyOf`` and ``nullable`` are resolved.
- ``approval``: ``read`` for GET, ``write`` otherwise, unless overridden by the
  ``approval`` map or an ``x-fabri-approval`` extension on the operation.

DELETE operations are left out unless ``methods`` names DELETE or ``allow``
names the operation exactly.
"""
from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import yaml

from fabri.tools.approval import check_approval, default_approval_for_method
from fabri.tools.credential_store import CredentialStore
from fabri.tools.http_call import dry_run_headers, send
from fabri.tools.manifest_schema import ToolManifest
from fabri.tools.result import tool_error

HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")
DESCRIPTION_CAP = 1500
NAME_MAX = 64
DEFAULT_STRIP_PREFIXES = ("api",)
# Response-schema properties whose enum values describe why a call failed.
REASON_KEYS = frozenset({"reason", "code", "error", "error_code", "reason_code"})
_DROP_SCHEMA_KEYS = frozenset({"example", "examples", "xml", "externalDocs", "discriminator", "deprecated"})
_VERSION_SEG = re.compile(r"^v\d+(\.\d+)*$", re.IGNORECASE)
_JSON_CT = re.compile(r"^application/([a-z0-9.+-]*\+)?json", re.IGNORECASE)


class OpenAPIError(ValueError):
    """The document could not be loaded or a reference does not resolve."""


# ---------------------------------------------------------------- loading


def load_openapi(
    source: str | Path | Mapping[str, Any],
    *,
    base_dir: str | Path | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout_s: float = 30.0,
) -> dict:
    """Load an OpenAPI 3 document from a URL, a file path (JSON or YAML) or a
    dict. Relative paths resolve against `base_dir` when given."""
    if isinstance(source, Mapping):
        doc: Any = copy.deepcopy(dict(source))
    else:
        text_source = str(source)
        if text_source.startswith(("http://", "https://")):
            with httpx.Client(transport=transport, timeout=timeout_s) as client:
                resp = client.get(text_source)
            if resp.status_code >= 400:
                raise OpenAPIError(f"fetching the OpenAPI document failed: HTTP {resp.status_code}")
            text = resp.text
        else:
            path = Path(text_source)
            if not path.is_absolute() and base_dir is not None:
                path = Path(base_dir) / path
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise OpenAPIError(f"cannot read OpenAPI document {path}: {exc}") from exc
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise OpenAPIError(f"OpenAPI document is not valid JSON or YAML: {exc}") from exc
    if not isinstance(doc, dict) or not str(doc.get("openapi", "")).startswith("3"):
        raise OpenAPIError("not an OpenAPI 3 document (missing `openapi: 3.x`)")
    return doc


# ---------------------------------------------------------------- schemas


class SchemaResolver:
    """Inline local ``$ref``s and flatten ``allOf``. Recursive references
    stop at the first repeat and become a plain object with a note."""

    def __init__(self, doc: Mapping[str, Any]):
        self.doc = doc

    def lookup(self, ref: str) -> Any:
        if not ref.startswith("#/"):
            raise OpenAPIError(f"only local $ref values are supported (got {ref!r})")
        node: Any = self.doc
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, Mapping) or part not in node:
                raise OpenAPIError(f"$ref does not resolve: {ref}")
            node = node[part]
        return node

    def deref(self, node: Any, stack: tuple[str, ...] = ()) -> Any:
        """Follow a top-level $ref chain (parameters, request bodies, responses)."""
        seen = set(stack)
        while isinstance(node, Mapping) and "$ref" in node:
            ref = node["$ref"]
            if ref in seen:
                raise OpenAPIError(f"circular $ref: {ref}")
            seen.add(ref)
            node = self.lookup(ref)
        return node

    def schema(self, node: Any, stack: tuple[str, ...] = ()) -> Any:
        if isinstance(node, list):
            return [self.schema(item, stack) for item in node]
        if not isinstance(node, Mapping):
            return node
        if "$ref" in node:
            ref = str(node["$ref"])
            if ref in stack:
                return {"type": "object", "description": f"recursive reference to {ref.rsplit('/', 1)[-1]}"}
            target = self.schema(self.lookup(ref), stack + (ref,))
            siblings = {k: v for k, v in node.items() if k != "$ref"}
            if siblings and isinstance(target, dict):
                return {**target, **self.schema(siblings, stack)}
            return target
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in _DROP_SCHEMA_KEYS or key.startswith("x-"):
                continue
            if key in ("properties", "patternProperties") and isinstance(value, Mapping):
                out[key] = {name: self.schema(sub, stack) for name, sub in value.items()}
            elif key in ("items", "additionalProperties", "not") and isinstance(value, Mapping):
                out[key] = self.schema(value, stack)
            elif key in ("allOf", "oneOf", "anyOf") and isinstance(value, list):
                out[key] = [self.schema(sub, stack) for sub in value]
            else:
                out[key] = copy.deepcopy(value)
        if "allOf" in out:
            out = _merge_all_of(out)
        if out.pop("nullable", False):
            out = _make_nullable(out)
        return out


def _merge_all_of(node: dict[str, Any]) -> dict[str, Any]:
    parts = node.pop("allOf")
    merged: dict[str, Any] = {}
    for part in [*parts, node]:
        if not isinstance(part, dict):
            continue
        for key, value in part.items():
            if key == "properties" and isinstance(value, dict):
                merged.setdefault("properties", {}).update(value)
            elif key == "required" and isinstance(value, list):
                merged["required"] = list(dict.fromkeys([*merged.get("required", []), *value]))
            elif key == "description" and "description" in merged:
                continue
            else:
                merged[key] = value
    if "properties" in merged:
        merged.setdefault("type", "object")
    return merged


def _make_nullable(node: dict[str, Any]) -> dict[str, Any]:
    kind = node.get("type")
    if isinstance(kind, str):
        node["type"] = [kind, "null"]
    elif isinstance(kind, list):
        if "null" not in kind:
            node["type"] = [*kind, "null"]
    else:
        return {"anyOf": [node, {"type": "null"}]}
    if isinstance(node.get("enum"), list) and None not in node["enum"]:
        node["enum"] = [*node["enum"], None]
    return node


def _type_label(schema: Any) -> str:
    if not isinstance(schema, Mapping):
        return "any"
    kind = schema.get("type")
    if isinstance(kind, list):
        return "|".join(str(k) for k in kind)
    if kind:
        return str(kind)
    for key in ("oneOf", "anyOf"):
        if isinstance(schema.get(key), list):
            return "|".join(_type_label(s) for s in schema[key])
    return "object" if "properties" in schema else "any"


def _reason_enums(schema: Any, depth: int = 0) -> list[str]:
    if depth > 3 or not isinstance(schema, Mapping):
        return []
    found: list[str] = []
    for name, sub in (schema.get("properties") or {}).items():
        if not isinstance(sub, Mapping):
            continue
        if name in REASON_KEYS and isinstance(sub.get("enum"), list):
            found.extend(str(v) for v in sub["enum"] if v is not None)
        found.extend(_reason_enums(sub, depth + 1))
    for key in ("oneOf", "anyOf"):
        for sub in schema.get(key) or []:
            found.extend(_reason_enums(sub, depth + 1))
    if isinstance(schema.get("items"), Mapping):
        found.extend(_reason_enums(schema["items"], depth + 1))
    return list(dict.fromkeys(found))


# ---------------------------------------------------------------- naming


def snake_case(text: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", text)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_").lower()


def singular(word: str) -> str:
    """Small English singularizer for path segments (pets -> pet)."""
    lower = word.lower()
    if lower.endswith("ies") and len(lower) > 3:
        return word[:-3] + "y"
    if re.search(r"(ss|x|z|ch|sh)es$", lower):
        return word[:-2]
    if lower.endswith(("ss", "us", "is")) or not lower.endswith("s") or len(lower) <= 3:
        return word
    return word[:-1]


def _is_param(seg: str) -> bool:
    return seg.startswith("{") and seg.endswith("}")


_COLLECTION_ACTIONS = {"get": "list", "post": "create", "put": "replace", "patch": "update",
                       "delete": "delete", "head": "head", "options": "options"}
_ITEM_ACTIONS = {"get": "get", "post": "update", "put": "replace", "patch": "update",
                 "delete": "delete", "head": "head", "options": "options"}


def readable_parts(
    method: str, path: str, tags: Iterable[str] = (), strip_prefixes: Iterable[str] = DEFAULT_STRIP_PREFIXES
) -> tuple[str, str]:
    """Return (resource, action) for one operation.

    ``GET /pets`` -> (pet, list), ``GET /pets/{id}`` -> (pet, get),
    ``POST /pets`` -> (pet, create), ``PATCH /pets/{id}`` -> (pet, update),
    ``POST /pets/{id}/adopt`` -> (pet, adopt), ``GET /pets/{id}/photos`` ->
    (pet_photo, list). Version segments (v1, v2.1) and `strip_prefixes` are
    dropped first.
    """
    method = method.lower()
    strip = {s.lower().strip("/") for s in strip_prefixes}
    segs = [s for s in path.strip("/").split("/") if s]
    segs = [s for s in segs if not (s.lower() in strip or _VERSION_SEG.match(s))]
    if not segs:
        resource = snake_case(next(iter(tags), "") or "root")
        return singular(resource) or "root", _COLLECTION_ACTIONS.get(method, method)
    last = segs[-1]
    statics = [i for i, s in enumerate(segs) if not _is_param(s)]
    if _is_param(last):
        owner = segs[statics[-1]] if statics else "item"
        return singular(snake_case(owner)), _ITEM_ACTIONS.get(method, method)
    prev_is_param = len(segs) >= 2 and _is_param(segs[-2])
    word = snake_case(last)
    if prev_is_param:
        owner_idx = [i for i in statics if i < len(segs) - 2]
        owner = singular(snake_case(segs[owner_idx[-1]])) if owner_idx else "item"
        if singular(word) != word:  # plural: a sub-collection
            return f"{owner}_{singular(word)}", _COLLECTION_ACTIONS.get(method, method)
        return owner, word  # a verb such as adopt, cancel, publish
    if len(segs) >= 2 and not _is_param(segs[-2]) and singular(word) == word and method == "post":
        # POST /pets/bulk-import -> pet_bulk_import
        return singular(snake_case(segs[-2])), word
    return singular(word), _COLLECTION_ACTIONS.get(method, method)


def _clip_name(name: str) -> str:
    name = snake_case(name) or "op"
    return name[:NAME_MAX].rstrip("_")


def _fallback_operation_id(method: str, path: str) -> str:
    return _clip_name(f"{method}_{path}")


# ---------------------------------------------------------------- operations


@dataclass
class OpenAPIParam:
    name: str
    location: str  # path | query
    required: bool
    schema: dict
    description: str = ""

    @property
    def enum(self) -> list | None:
        values = self.schema.get("enum") if isinstance(self.schema, dict) else None
        return list(values) if isinstance(values, list) else None


@dataclass
class OpenAPIOperation:
    """One operation, ready to become a tool."""

    name: str
    operation_id: str
    method: str
    path: str
    resource: str
    action: str
    summary: str
    text: str  # the operation's own description
    description: str  # summary + description + Responses line, capped
    input_schema: dict
    approval: str
    tags: list[str] = field(default_factory=list)
    params: list[OpenAPIParam] = field(default_factory=list)
    body_mode: str | None = None  # "merge" | "wrap" | None
    body_keys: list[str] = field(default_factory=list)
    body_required: bool = False
    body_schema: dict | None = None
    body_content_type: str = "application/json"
    responses: dict[str, dict] = field(default_factory=dict)  # code -> {description, reasons}
    probe_safe: bool = False
    aliases: list[str] = field(default_factory=list)

    def manifest(self, timeout_s: float = 30.0) -> ToolManifest:
        return ToolManifest(
            name=self.name,
            description=self.description,
            command=[],
            input_schema=self.input_schema,
            output_schema={"type": "object"},
            timeout_s=timeout_s,
            approval=self.approval,
            aliases=list(self.aliases),
            title=self.summary or None,
        )

    def schema_hash(self) -> str:
        payload = json.dumps(
            {"name": self.name, "method": self.method, "path": self.path, "summary": self.summary,
             "text": self.text, "input": self.input_schema, "responses": self.responses},
            sort_keys=True, default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def responses_line(responses: Mapping[str, Mapping[str, Any]]) -> str:
    if not responses:
        return ""
    parts = []
    for code in sorted(responses, key=lambda c: (not c[:1].isdigit(), c)):
        reasons = responses[code].get("reasons") or []
        parts.append(f"{code} (reason: {'|'.join(reasons)})" if reasons else code)
    return "Responses: " + ", ".join(parts) + "."


def compose_description(summary: str, text: str, responses: Mapping[str, Mapping[str, Any]],
                        fallback: str, cap: int = DESCRIPTION_CAP) -> str:
    summary, text = (summary or "").strip(), (text or "").strip()
    pieces = [summary] if summary else []
    if text and not text.startswith(summary or "\0"):
        pieces.append(text)
    elif text and not summary:
        pieces.append(text)
    body = "\n\n".join(pieces) or fallback
    tail = responses_line(responses)
    if not tail:
        return _cut(body, cap)
    room = cap - len(tail) - 1
    if room < 40:
        return _cut(body + "\n" + tail, cap)
    return _cut(body, room) + "\n" + tail


def _cut(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[: max(0, cap - 3)].rstrip() + "..."


def _pick_content(content: Mapping[str, Any] | None) -> tuple[str, Any] | None:
    if not content:
        return None
    for ctype, media in content.items():
        if _JSON_CT.match(ctype):
            return ctype, media
    for ctype, media in content.items():
        if ctype.startswith("application/x-www-form-urlencoded"):
            return ctype, media
    return None


def _matches(name: str, patterns: Iterable[str] | None) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in (patterns or ()))


def parse_operations(
    doc: Mapping[str, Any],
    *,
    naming: str = "readable",
    names: Mapping[str, str] | None = None,
    strip_prefixes: Iterable[str] = DEFAULT_STRIP_PREFIXES,
    approval: Mapping[str, str] | None = None,
    description_cap: int = DESCRIPTION_CAP,
) -> list[OpenAPIOperation]:
    """Every operation in the document, unfiltered, with stable names.

    `names` maps an operation_id (or a readable name) to a chosen tool name.
    Readable names that collide get a stable 4-hex suffix from method and
    path; the first operation in path order keeps the plain name.
    """
    if naming not in ("readable", "operation_id"):
        raise OpenAPIError("naming must be 'readable' or 'operation_id'")
    resolver = SchemaResolver(doc)
    raw: list[tuple[str, str, dict, list]] = []
    for path, item in sorted((doc.get("paths") or {}).items()):
        item = resolver.deref(item)
        if not isinstance(item, Mapping):
            continue
        shared = item.get("parameters") or []
        for method in HTTP_METHODS:
            op = item.get(method)
            if isinstance(op, Mapping):
                raw.append((path, method, dict(op), list(shared)))

    # Names first, over the whole document, so a filter change never renames a tool.
    readable: list[str] = []
    for path, method, op, _ in raw:
        resource, action = readable_parts(method, path, op.get("tags") or (), strip_prefixes)
        readable.append(_clip_name(f"{resource}_{action}"))
    seen: dict[str, int] = {}
    unique: list[str] = []
    for (path, method, _, _), base in zip(raw, readable):
        if base in seen:
            suffix = hashlib.sha1(f"{method.upper()} {path}".encode()).hexdigest()[:4]
            unique.append(f"{base[: NAME_MAX - 5]}_{suffix}")
        else:
            unique.append(base)
        seen[base] = seen.get(base, 0) + 1

    ops: list[OpenAPIOperation] = []
    used: set[str] = set()
    for (path, method, op, shared), pretty in zip(raw, unique):
        op_id = str(op.get("operationId") or _fallback_operation_id(method, path))
        primary, alias = (pretty, op_id) if naming == "readable" else (op_id, pretty)
        override = (names or {}).get(op_id) or (names or {}).get(pretty)
        if override:
            primary = str(override)
        if primary in used:
            primary = f"{primary[: NAME_MAX - 5]}_{hashlib.sha1(f'{method} {path}'.encode()).hexdigest()[:4]}"
        used.add(primary)
        aliases = [a for a in dict.fromkeys([op_id, pretty]) if a != primary]
        resource, action = readable_parts(method, path, op.get("tags") or (), strip_prefixes)
        ops.append(_build_operation(resolver, path, method, op, shared, primary, op_id, aliases,
                                    resource, action, approval, description_cap))
    return ops


def _build_operation(resolver, path, method, op, shared, name, op_id, aliases, resource, action,
                     approval_map, cap) -> OpenAPIOperation:
    merged: dict[tuple[str, str], dict] = {}
    for p in [*shared, *(op.get("parameters") or [])]:
        p = resolver.deref(p)
        if isinstance(p, Mapping) and p.get("name") and p.get("in"):
            merged[(str(p["name"]), str(p["in"]))] = dict(p)
    params: list[OpenAPIParam] = []
    for (pname, loc), p in merged.items():
        if loc not in ("path", "query"):
            continue  # header and cookie params are set by the host, not the model
        schema = p.get("schema")
        if schema is None and isinstance(p.get("content"), Mapping):
            first = next(iter(p["content"].values()), {})
            schema = first.get("schema") if isinstance(first, Mapping) else None
        schema = resolver.schema(schema or {"type": "string"})
        params.append(OpenAPIParam(pname, loc, bool(p.get("required")) or loc == "path",
                                   schema if isinstance(schema, dict) else {}, str(p.get("description") or "")))

    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in params:
        prop = dict(param.schema)
        if param.description and "description" not in prop:
            prop["description"] = param.description
        properties[param.name] = prop
        if param.required:
            required.append(param.name)

    body_mode, body_keys, body_required, body_schema, body_ct = None, [], False, None, "application/json"
    request_body = resolver.deref(op.get("requestBody")) if op.get("requestBody") else None
    if isinstance(request_body, Mapping):
        picked = _pick_content(request_body.get("content"))
        if picked:
            body_ct, media = picked
            schema = resolver.schema((media or {}).get("schema") or {"type": "object"})
            body_required = bool(request_body.get("required"))
            if isinstance(schema, dict):
                body_schema = schema
                props = {k: v for k, v in (schema.get("properties") or {}).items()
                         if not (isinstance(v, Mapping) and v.get("readOnly"))}
                is_object = schema.get("type") in (None, "object") and props
                if is_object and not set(props) & set(properties):
                    body_mode, body_keys = "merge", list(props)
                    for key, sub in props.items():
                        if isinstance(sub, dict):
                            sub = {k: v for k, v in sub.items() if k != "readOnly"}
                        properties[key] = sub
                    if body_required:
                        required.extend(r for r in schema.get("required") or [] if r in props)
                else:
                    body_mode = "wrap"
                    properties["body"] = schema
                    if body_required:
                        required.append("body")

    input_schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        input_schema["required"] = list(dict.fromkeys(required))

    responses: dict[str, dict] = {}
    for code, resp in (op.get("responses") or {}).items():
        resp = resolver.deref(resp)
        if not isinstance(resp, Mapping):
            continue
        reasons: list[str] = []
        picked = _pick_content(resp.get("content"))
        if picked:
            reasons = _reason_enums(resolver.schema((picked[1] or {}).get("schema") or {}))
        responses[str(code)] = {"description": str(resp.get("description") or ""), "reasons": reasons}

    summary = str(op.get("summary") or "").strip()
    text = str(op.get("description") or "").strip()
    desc = compose_description(summary, text, responses, f"{method.upper()} {path}", cap)
    approval = (approval_map or {}).get(name) or (approval_map or {}).get(op_id) \
        or op.get("x-fabri-approval") or default_approval_for_method(method)
    return OpenAPIOperation(
        name=name, operation_id=op_id, method=method.upper(), path=path, resource=resource,
        action=action, summary=summary, text=text, description=desc, input_schema=input_schema,
        approval=check_approval(approval), tags=[str(t) for t in op.get("tags") or []],
        params=params, body_mode=body_mode, body_keys=body_keys, body_required=body_required,
        body_schema=body_schema, body_content_type=body_ct, responses=responses,
        probe_safe=bool(op.get("x-fabri-probe-safe")), aliases=aliases,
    )


def filter_operations(
    ops: Iterable[OpenAPIOperation],
    *,
    allow: Iterable[str] | None = None,
    deny: Iterable[str] | None = None,
    methods: Iterable[str] | None = None,
    tags: Iterable[str] | None = None,
    allow_delete: bool = True,
) -> list[OpenAPIOperation]:
    """Apply allow and deny lists (names or fnmatch patterns, matched against
    the tool name and the operation_id), a method list and a tag list.

    DELETE passes only when `methods` names it or `allow` names the operation
    exactly (no pattern), and never when `allow_delete` is False (a policy
    can switch DELETE off for everyone)."""
    allow_list = list(allow) if allow is not None else None
    method_set = {m.upper() for m in methods} if methods is not None else None
    tag_set = set(tags) if tags is not None else None
    out = []
    for op in ops:
        names = [op.name, op.operation_id]
        if op.method == "DELETE":
            explicit = (method_set is not None and "DELETE" in method_set) or \
                (allow_list is not None and any(n in allow_list for n in names))
            if not explicit or not allow_delete:
                continue
        if method_set is not None and op.method not in method_set:
            continue
        if tag_set is not None and not tag_set & set(op.tags):
            continue
        if allow_list is not None and not any(_matches(n, allow_list) for n in names):
            continue
        if deny and any(_matches(n, deny) for n in names):
            continue
        out.append(op)
    return out


def openapi_tool_defs(doc: Mapping[str, Any], **options: Any) -> list[OpenAPIOperation]:
    """parse_operations + filter_operations in one call. Accepts the keyword
    arguments of both."""
    parse_keys = {"naming", "names", "strip_prefixes", "approval", "description_cap"}
    parse_kw = {k: v for k, v in options.items() if k in parse_keys}
    filter_kw = {k: v for k, v in options.items() if k not in parse_keys}
    return filter_operations(parse_operations(doc, **parse_kw), **filter_kw)


# ---------------------------------------------------------------- calling


def default_base_url(doc: Mapping[str, Any]) -> str | None:
    for server in doc.get("servers") or []:
        url = str((server or {}).get("url") or "")
        if urlparse(url).scheme in ("http", "https"):
            return url
    return None


def operation_handler(
    op: OpenAPIOperation,
    *,
    base_url: str,
    client: httpx.Client,
    headers: Mapping[str, Any] | None = None,
    store: CredentialStore | None = None,
    extra_headers: Mapping[str, str] | None = None,
    timeout_s: float = 30.0,
) -> Callable[[dict], dict]:
    known = {p.name for p in op.params} | set(op.body_keys) | ({"body"} if op.body_mode == "wrap" else set())

    def handler(args: dict) -> dict:
        args = dict(args or {})
        unknown = sorted(set(args) - known)
        if unknown:
            return tool_error(f"{op.name}: unknown arguments {unknown}",
                              result={"status": "error", "code": None})
        path = op.path
        query: dict[str, Any] = {}
        for param in op.params:
            if param.location == "path":
                if param.name not in args or args[param.name] in (None, ""):
                    return tool_error(f"{op.name}: missing path parameter {param.name!r}",
                                      result={"status": "error", "code": None})
                path = path.replace("{" + param.name + "}", quote(str(args[param.name]), safe=""))
            elif param.name in args:
                value = args[param.name]
                query[param.name] = ("true" if value else "false") if isinstance(value, bool) else value
        json_body = form_body = None
        if op.body_mode == "merge":
            body = {k: args[k] for k in op.body_keys if k in args}
            if body or op.body_required:
                if op.body_content_type.startswith("application/x-www-form-urlencoded"):
                    form_body = body
                else:
                    json_body = body
        elif op.body_mode == "wrap" and "body" in args:
            json_body = args["body"]
        return send(client, op.method, base_url.rstrip("/") + path, headers=headers, store=store,
                    extra_headers=extra_headers, params=query, json_body=json_body,
                    form_body=form_body, timeout_s=timeout_s, tool_name=op.name)

    return handler


def openapi_tools(
    source: str | Path | Mapping[str, Any],
    *,
    base_url: str | None = None,
    headers: Mapping[str, Any] | None = None,
    dry_run_header: Mapping[str, Any] | str | None = None,
    dry_run: bool = False,
    store: CredentialStore | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout_s: float = 30.0,
    base_dir: str | Path | None = None,
    **options: Any,
) -> list[tuple[ToolManifest, Callable[[dict], dict]]]:
    """Load a document and return (manifest, handler) pairs. `options` are
    the keyword arguments of openapi_tool_defs."""
    doc = load_openapi(source, base_dir=base_dir, transport=transport, timeout_s=timeout_s)
    base = base_url or default_base_url(doc)
    if not base:
        raise OpenAPIError("no base_url given and the document has no absolute servers[].url")
    client = httpx.Client(transport=transport, timeout=timeout_s)
    extra = dry_run_headers(dry_run_header) if dry_run else {}
    return [
        (op.manifest(timeout_s), operation_handler(op, base_url=base, client=client, headers=headers,
                                                   store=store, extra_headers=extra, timeout_s=timeout_s))
        for op in openapi_tool_defs(doc, **options)
    ]
