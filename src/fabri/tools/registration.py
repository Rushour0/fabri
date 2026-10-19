"""Register dynamic tools into a setup layer at runtime.

    from fabri.tools.registration import register_tool
    from fabri.tools.policy import ToolPolicy

    policy = ToolPolicy(allow=("crm_*",), allowed_kinds=("http",), allowed_hosts=("api.example.com",))
    register_tool(project_layer, {
        "name": "crm_lookup",
        "description": "Look up a contact by id.",
        "kind": "http",
        "endpoint": "https://api.example.com/contacts/{contact_id}",
        "method": "GET",
        "input_schema": {"type": "object", "properties": {"contact_id": {"type": "string"}},
                         "required": ["contact_id"]},
        "auth": {"secret_ref": "crm:prod", "header": "Authorization", "prefix": "Bearer "},
        "approval": "read",
        "version": 1,
    }, actor="ops@example.com", policy=policy)

Each tool is one file, ``<layer>/tools/registered/<name>.json``, written
atomically. The setup resolver merges these with ``tools/tools.yaml``, so the
caller's global allowlist and hot reload apply the same way. Every register,
unregister, enable and disable appends a line to
``tools/registered/_audit.jsonl`` (actor, action, name, version, time,
content hash). Neither file ever holds a secret value: `auth` only accepts a
secret reference or an environment variable name.

Versions: registering a name again with a higher version replaces it; the
same version with different content is rejected; the same content is a
no-op.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

import httpx

from fabri.tools.approval import check_approval, default_approval_for_method
from fabri.tools.openapi import OpenAPIError, load_openapi, parse_operations
from fabri.tools.policy import SOURCE_KINDS, PolicyError, ToolPolicy
from fabri.tools.sources import TOOL_NAME

REGISTERED_DIR = Path("tools") / "registered"
AUDIT_FILE = "_audit.jsonl"
DECL_KEYS = frozenset({
    "name", "description", "kind", "input_schema", "endpoint", "method", "source", "server",
    "callable", "auth", "approval", "version", "title", "enabled", "timeout_s",
})
AUTH_KEYS = frozenset({"secret_ref", "header_env", "header", "prefix"})
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_SECRET_REF = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}:[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_SECRETISH_PREFIXES = ("sk-", "sk_", "pk_", "rk_", "ghp_", "gho_", "github_pat_", "xox", "akia", "bearer ",
                       "basic ", "eyj", "glpat-", "ya29.")
_SECRET_QUERY_KEYS = frozenset({"key", "api_key", "apikey", "token", "access_token", "secret", "password",
                                "sig", "signature", "auth"})
_JSON_TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})


class RegistrationError(ValueError):
    """A registration was refused. The message never holds a secret value."""


@dataclass
class RegisteredTool:
    name: str
    version: str
    kind: str
    approval: str
    enabled: bool
    content_hash: str
    path: Path
    decl: dict[str, Any]
    registered_by: str = ""
    registered_at: str = ""
    changed: bool = True


# ---------------------------------------------------------------- helpers


def looks_like_secret(value: str) -> bool:
    """Heuristic for a pasted credential: a known token prefix, or a long
    run of mixed letters and digits with no separators."""
    text = value.strip()
    lower = text.lower()
    if lower.startswith(_SECRETISH_PREFIXES):
        return True
    if len(text) >= 24 and re.fullmatch(r"[A-Za-z0-9+/=_\-.]+", text) and re.search(r"\d", text) \
            and re.search(r"[a-z]", text) and re.search(r"[A-Z]", text):
        return True
    return False


def _version_key(version: str) -> tuple:
    parts = re.findall(r"\d+", str(version))
    if not parts:
        raise RegistrationError(f"version {version!r} must contain a number, for example 1 or 1.2.0")
    return tuple(int(p) for p in parts)


def _content_hash(decl: Mapping[str, Any]) -> str:
    body = {k: v for k, v in decl.items() if k != "enabled"}
    return "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _dir(layer_dir: str | Path) -> Path:
    return Path(layer_dir) / REGISTERED_DIR


def _atomic_write(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _audit(layer_dir: str | Path, actor: str, action: str, name: str, version: str, content_hash: str) -> None:
    directory = _dir(layer_dir)
    directory.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": _now(), "actor": actor, "action": action, "name": name,
                       "version": version, "content_hash": content_hash}, sort_keys=True)
    fd = os.open(directory / AUDIT_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (line + "\n").encode())
    finally:
        os.close(fd)


def check_input_schema(schema: Any, where: str = "input_schema") -> None:
    """Structural JSON Schema check: an object at the root, mappings for
    properties, known types, `required` naming declared properties."""
    if not isinstance(schema, Mapping):
        raise RegistrationError(f"{where} must be a JSON Schema object")
    if where == "input_schema" and schema.get("type") != "object":
        raise RegistrationError("input_schema must have type: object at the root")
    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind] if kind is not None else []
    for k in kinds:
        if k not in _JSON_TYPES:
            raise RegistrationError(f"{where}: unknown type {k!r}")
    props = schema.get("properties", {})
    if not isinstance(props, Mapping):
        raise RegistrationError(f"{where}.properties must be a mapping")
    for name, sub in props.items():
        check_input_schema(sub, f"{where}.properties.{name}")
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
        raise RegistrationError(f"{where}.required must be a list of names")
    missing = [r for r in required if r not in props]
    if missing:
        raise RegistrationError(f"{where}.required names undeclared properties {missing}")
    if "items" in schema:
        check_input_schema(schema["items"], f"{where}.items")
    for key in ("oneOf", "anyOf", "allOf"):
        if key in schema:
            if not isinstance(schema[key], list):
                raise RegistrationError(f"{where}.{key} must be a list")
            for i, sub in enumerate(schema[key]):
                check_input_schema(sub, f"{where}.{key}[{i}]")
    if "enum" in schema and not isinstance(schema["enum"], list):
        raise RegistrationError(f"{where}.enum must be a list")


def _check_auth(auth: Any) -> None:
    if auth is None:
        return
    if not isinstance(auth, Mapping):
        raise RegistrationError("auth must be a mapping with secret_ref or header_env")
    unknown = set(auth) - AUTH_KEYS
    if unknown:
        raise RegistrationError(f"auth accepts only {sorted(AUTH_KEYS)}; raw values are not stored "
                                f"(unexpected keys {sorted(unknown)})")
    if ("secret_ref" in auth) == ("header_env" in auth):
        raise RegistrationError("auth needs exactly one of secret_ref or header_env")
    if "secret_ref" in auth:
        ref = str(auth["secret_ref"])
        if not _SECRET_REF.fullmatch(ref) or looks_like_secret(ref.split(":", 1)[-1]):
            raise RegistrationError("auth.secret_ref must be a reference like provider:handle, not a secret value")
    if "header_env" in auth:
        env = str(auth["header_env"])
        if not _ENV_NAME.fullmatch(env) or looks_like_secret(env):
            raise RegistrationError("auth.header_env must be an environment variable name (A-Z, 0-9, _), "
                                    "not a secret value")
    header = auth.get("header", "Authorization")
    if not isinstance(header, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", header):
        raise RegistrationError("auth.header must be a header name")
    prefix = auth.get("prefix", "")
    if not isinstance(prefix, str) or len(prefix) > 16 or looks_like_secret(prefix.strip() or "x"):
        raise RegistrationError("auth.prefix must be a short scheme such as 'Bearer '")


def _check_url(url: Any, what: str, policy: ToolPolicy) -> str:
    if not isinstance(url, str) or urlparse(url).scheme not in ("http", "https") or not urlparse(url).hostname:
        raise RegistrationError(f"{what} must be an http or https URL")
    if urlparse(url).username or urlparse(url).password:
        raise RegistrationError(f"{what} must not embed credentials")
    for key, _ in parse_qsl(urlparse(url).query):
        if key.lower() in _SECRET_QUERY_KEYS:
            raise RegistrationError(f"{what} must not carry a credential in the query string ({key})")
    policy.check_host(url, what)
    return url


def _headers(auth: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not auth:
        return None
    header = str(auth.get("header") or "Authorization")
    spec: dict[str, Any] = {"secret_ref": auth["secret_ref"]} if "secret_ref" in auth else {"env": auth["header_env"]}
    if auth.get("prefix"):
        spec["prefix"] = auth["prefix"]
    return {header: spec}


def _openapi_source(decl: Mapping[str, Any]) -> dict[str, Any]:
    source = decl.get("source")
    if isinstance(source, str):
        source = {"spec": source}
    if not isinstance(source, Mapping) or not source.get("spec"):
        raise RegistrationError("openapi tools need source: {spec: <URL or file>, operation_ids: [...]}")
    ids = source.get("operation_ids") or ([source["operation_id"]] if source.get("operation_id") else [])
    if not ids or not all(isinstance(i, str) for i in ids):
        raise RegistrationError("openapi tools need source.operation_ids (one or more operation ids)")
    return {**source, "operation_ids": list(ids)}


# ---------------------------------------------------------------- validate


def validate_registration(
    decl: Any,
    policy: ToolPolicy,
    *,
    layer_dir: str | Path | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Check a declaration against the rules and the caller's policy.
    Returns the normalized declaration; raises RegistrationError or
    PolicyError."""
    if not isinstance(decl, Mapping):
        raise RegistrationError("a tool declaration must be a mapping")
    unknown = set(decl) - DECL_KEYS
    if unknown:
        raise RegistrationError(f"unknown keys {sorted(unknown)}")
    name = decl.get("name")
    if not isinstance(name, str) or not TOOL_NAME.fullmatch(name) or name.startswith("_"):
        raise RegistrationError(f"name {name!r} must match {TOOL_NAME.pattern} and not start with _")
    kind = decl.get("kind")
    if kind not in SOURCE_KINDS:
        raise RegistrationError(f"kind must be one of {', '.join(SOURCE_KINDS)}")
    policy.check_kind(kind)
    if not isinstance(decl.get("description"), str) or not decl["description"].strip():
        raise RegistrationError("description is required")
    _check_auth(decl.get("auth"))
    version = str(decl.get("version", 1))
    _version_key(version)
    out = {**decl, "version": version, "enabled": decl.get("enabled", True) is not False}
    if "input_schema" in decl or kind in ("http", "python"):
        check_input_schema(decl.get("input_schema", {"type": "object"}))

    if kind == "http":
        endpoint = decl.get("endpoint")
        method = decl.get("method") or "GET"
        if isinstance(endpoint, Mapping):
            method = endpoint.get("method") or method
            endpoint = endpoint.get("url")
        _check_url(endpoint, "endpoint", policy)
        method = str(method).upper()
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            raise RegistrationError(f"method {method!r} is not an HTTP method")
        if method == "DELETE" and not policy.allow_delete:
            raise PolicyError("DELETE tools are not allowed by the policy")
        out.update(endpoint=endpoint, method=method)
        if not policy.name_allowed(name):
            raise PolicyError(f"tool name {name!r} is not in the policy's allowlist")
        default_approval = default_approval_for_method(method)
    elif kind == "openapi":
        source = _openapi_source(decl)
        spec = source["spec"]
        if isinstance(spec, str) and spec.startswith(("http://", "https://")):
            _check_url(spec, "source.spec", policy)
        if source.get("base_url"):
            _check_url(source["base_url"], "source.base_url", policy)
        base_dir = Path(layer_dir) / "tools" if layer_dir else None
        try:
            doc = load_openapi(spec, base_dir=base_dir, transport=transport)
        except OpenAPIError as exc:
            raise RegistrationError(f"source.spec: {exc}") from None
        ops = {op.operation_id: op for op in parse_operations(doc)}
        missing = [i for i in source["operation_ids"] if i not in ops]
        if missing:
            raise RegistrationError(f"source.operation_ids not in the document: {missing}")
        if not source.get("base_url"):
            from fabri.tools.openapi import default_base_url

            base = default_base_url(doc)
            if base is None:
                raise RegistrationError("source.base_url is required when the document has no absolute server URL")
            policy.check_host(base, "the document's server URL")
        for op_id in source["operation_ids"]:
            op = ops[op_id]
            if op.method == "DELETE" and not policy.allow_delete:
                raise PolicyError(f"operation {op_id} is a DELETE, which the policy does not allow")
            if not policy.name_allowed(op.name, op.aliases):
                raise PolicyError(f"operation {op_id} ({op.name}) is not in the policy's allowlist")
        out["source"] = source
        default_approval = "read" if all(ops[i].method == "GET" for i in source["operation_ids"]) else "write"
    elif kind == "mcp":
        server = decl.get("server")
        if not isinstance(server, Mapping) or bool(server.get("command")) == bool(server.get("url")):
            raise RegistrationError("mcp tools need server: {command: [...]} or server: {url: ...}")
        if server.get("url"):
            _check_url(server["url"], "server.url", policy)
        elif not isinstance(server["command"], list) or not all(isinstance(c, str) for c in server["command"]):
            raise RegistrationError("server.command must be a list of strings")
        if not policy.name_allowed(f"mcp_{name}_x") and not policy.name_allowed(name):
            raise PolicyError(f"mcp tools named mcp_{name}_* are not in the policy's allowlist")
        default_approval = "write"
    else:  # python
        target = decl.get("callable")
        if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z_][\w.]*(:[A-Za-z_]\w*)?", target):
            raise RegistrationError("callable must be a dotted path such as package.module:function")
        if not policy.module_allowed(target):
            raise PolicyError("callable is outside the policy's python_modules")
        if not policy.name_allowed(name):
            raise PolicyError(f"tool name {name!r} is not in the policy's allowlist")
        default_approval = "write"
    out["approval"] = check_approval(decl.get("approval") or default_approval)
    return out


# ---------------------------------------------------------------- API


def _load(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _record(path: Path, data: Mapping[str, Any], changed: bool = True) -> RegisteredTool:
    decl = dict(data.get("decl") or {})
    return RegisteredTool(
        name=str(data["name"]), version=str(data.get("version")), kind=str(decl.get("kind")),
        approval=str(decl.get("approval")), enabled=bool(data.get("enabled", True)),
        content_hash=str(data.get("content_hash")), path=path, decl=decl,
        registered_by=str(data.get("registered_by") or ""), registered_at=str(data.get("registered_at") or ""),
        changed=changed,
    )


def register_tool(
    layer_dir: str | Path,
    decl: Mapping[str, Any],
    *,
    actor: str,
    policy: ToolPolicy | None = None,
    transport: httpx.BaseTransport | None = None,
) -> RegisteredTool:
    """Validate and store one tool in `layer_dir`. `policy` defaults to the
    strict ToolPolicy() (http and openapi only, no DELETE, no python)."""
    if not actor:
        raise RegistrationError("actor is required for the audit log")
    policy = policy or ToolPolicy()
    normalized = validate_registration(decl, policy, layer_dir=layer_dir, transport=transport)
    name, version = normalized["name"], normalized["version"]
    digest = _content_hash(normalized)
    path = _dir(layer_dir) / f"{name}.json"
    existing = _load(path) if path.exists() else None
    if existing is not None:
        old_version = str(existing.get("version"))
        if _version_key(version) < _version_key(old_version):
            raise RegistrationError(f"{name} is already registered at version {old_version}; "
                                    f"register a higher version to replace it")
        if _version_key(version) == _version_key(old_version):
            if existing.get("content_hash") == digest:
                return _record(path, existing, changed=False)
            raise RegistrationError(f"{name} version {version} is already registered with different content; "
                                    f"bump the version")
    data = {"name": name, "version": version, "enabled": normalized["enabled"], "content_hash": digest,
            "registered_by": actor, "registered_at": _now(), "decl": normalized}
    _atomic_write(path, data)
    _audit(layer_dir, actor, "register", name, version, digest)
    return _record(path, data)


def unregister_tool(layer_dir: str | Path, name: str, *, actor: str) -> bool:
    if not actor:
        raise RegistrationError("actor is required for the audit log")
    path = _dir(layer_dir) / f"{name}.json"
    existing = _load(path) if path.exists() else None
    if existing is None:
        return False
    path.unlink()
    _audit(layer_dir, actor, "unregister", name, str(existing.get("version")), str(existing.get("content_hash")))
    return True


def set_enabled(layer_dir: str | Path, name: str, enabled: bool, *, actor: str) -> RegisteredTool:
    if not actor:
        raise RegistrationError("actor is required for the audit log")
    path = _dir(layer_dir) / f"{name}.json"
    existing = _load(path) if path.exists() else None
    if existing is None:
        raise RegistrationError(f"no registered tool named {name!r}")
    if bool(existing.get("enabled", True)) == enabled:
        return _record(path, existing, changed=False)
    existing["enabled"] = enabled
    existing["decl"] = {**(existing.get("decl") or {}), "enabled": enabled}
    _atomic_write(path, existing)
    _audit(layer_dir, actor, "enable" if enabled else "disable", name, str(existing.get("version")),
           str(existing.get("content_hash")))
    return _record(path, existing)


def list_registered(layer_dir: str | Path) -> list[RegisteredTool]:
    directory = _dir(layer_dir)
    out = []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        if path.name.startswith(("_", ".")):
            continue
        data = _load(path)
        if data and data.get("name") == path.stem:
            out.append(_record(path, data, changed=False))
    return out


def read_audit(layer_dir: str | Path) -> list[dict[str, Any]]:
    path = _dir(layer_dir) / AUDIT_FILE
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------- resolver


def to_source_decl(decl: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """A registered declaration as a fabri.tools.sources declaration, plus
    the allow entries it implies."""
    kind, name = decl["kind"], decl["name"]
    common = {k: decl[k] for k in ("description", "title", "timeout_s") if decl.get(k) is not None}
    headers = _headers(decl.get("auth"))
    if kind == "http":
        source = {"kind": "http", "name": name, "url": decl["endpoint"], "method": decl.get("method", "GET"),
                  "input_schema": decl.get("input_schema") or {"type": "object"}, "approval": decl["approval"],
                  **common}
        allow = [name]
    elif kind == "openapi":
        src = decl["source"]
        source = {"kind": "openapi", "name": name, "spec": src["spec"], "allow": list(src["operation_ids"]),
                  "approval": decl["approval"]}
        if "DELETE" in [str(m).upper() for m in src.get("methods") or []]:
            source["methods"] = src["methods"]
        for key in ("base_url", "naming", "names", "strip_prefixes", "dry_run_header"):
            if src.get(key) is not None:
                source[key] = src[key]
        if decl.get("timeout_s"):
            source["timeout_s"] = decl["timeout_s"]
        allow = list(src["operation_ids"])
    elif kind == "mcp":
        server = decl["server"]
        source = {"kind": "mcp", "name": name, "approval": decl["approval"],
                  **({"url": server["url"]} if server.get("url") else {"command": list(server["command"])})}
        allow = [f"mcp_{name}_*"]
    else:
        source = {"kind": "python", "name": name, "callable": decl["callable"], "approval": decl["approval"],
                  "input_schema": decl.get("input_schema") or {"type": "object"}, **common}
        allow = [name]
    if headers and kind in ("http", "openapi", "mcp"):
        source["headers"] = headers
    return source, allow


def registered_sources(directory: Path) -> list[dict[str, Any]]:
    """Read ``tools/registered`` for the resolver."""
    out = []
    for tool in list_registered(directory.parent.parent):
        try:
            source, allow = to_source_decl(tool.decl)
        except (KeyError, TypeError):
            continue
        out.append({"name": tool.name, "enabled": tool.enabled, "version": tool.version,
                    "decl": source, "allow": allow})
    return out
