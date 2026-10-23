"""Add, list, enable, disable and remove tool sources in a layer.

Each added source is one plain YAML file, ``<layer>/tools/sources/<name>.yaml``,
with the same shape as ``tools/tools.yaml``:

    sources:
      - kind: openapi
        name: pets
        spec: specs/pets.yaml          # copied next to it, so the layer is self-contained
        headers: {Authorization: {env: PETS_TOKEN, prefix: "Bearer "}}
        approval: {pet_delete: always_approve}
    allow: [pet_list, pet_get, pet_create, pet_delete]

Relative paths resolve against ``<layer>/tools``, as in tools.yaml. Secrets
are only ever environment variable names or ``provider:handle`` refs.

The flow is plan, then preview, then write: ``openapi_entry``,
``mcp_entry`` and ``connector_tool_entry`` build a ToolEntry without touching
disk; ``preview_entry`` builds its tools under the caller's policy and counts
approval classes; ``add_entry`` does both, refuses anything that does not
build, writes the files and lints the layer stack.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

import httpx
import yaml

from fabri.setups.layers import Layer, SetupError, read_yaml
from fabri.tools.approval import (
    APPROVAL_CLASSES,
    check_approval,
    default_approval_for_method,
    stricter,
)
from fabri.tools.connectors import CONNECTORS, ConnectorError, connector_entry
from fabri.tools.openapi import (
    OpenAPIError,
    filter_operations,
    load_openapi,
    parse_operations,
)
from fabri.tools.policy import ToolPolicy

SOURCES_DIR = Path("tools") / "sources"
SPECS_DIR = Path("tools") / "specs"
ENTRY_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
SECRET_REF = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}:[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_SECRET_QUERY_KEYS = frozenset({"key", "api_key", "apikey", "token", "access_token", "secret", "password",
                                "sig", "signature", "auth"})
_ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
HEADER = "# Added with `fabri setups tools add {how}`. Plain YAML: edit, review and commit it like code.\n" \
         "# Credentials are env var names or secret refs only.\n"
AUDIT_FILE = ".fabri-audit.jsonl"


class EntryError(ValueError):
    """A source that cannot be added as asked. The message holds no secret."""


@dataclass
class ToolEntry:
    name: str
    how: str  # openapi | mcp | connector <kind>
    data: dict[str, Any]  # {sources: [...], allow: [...]}
    spec_rel: str | None = None  # spec path relative to tools/, written with the entry
    spec_text: str | None = None
    spec_doc: dict[str, Any] | None = None
    connect: bool = True  # build MCP sources at preview time (spawns or calls the server)
    approval_floor: str | None = None

    def to_yaml(self) -> str:
        return HEADER.format(how=self.how) + yaml.safe_dump(self.data, sort_keys=False, allow_unicode=True,
                                                            width=100)

    def kinds(self) -> list[str]:
        return sorted({str(s.get("kind")) for s in self.data.get("sources") or []})


@dataclass
class EntryReport:
    name: str
    file: str
    kinds: list[str]
    tools: list[dict[str, Any]] = field(default_factory=list)
    hidden: list[str] = field(default_factory=list)  # built, but left out by allow or the global allowlist
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    lint: list[dict[str, str]] = field(default_factory=list)
    connected: bool = True
    written: bool = False

    def counts(self) -> dict[str, int]:
        out = {c: 0 for c in APPROVAL_CLASSES}
        for tool in self.tools:
            out[tool["approval"]] = out.get(tool["approval"], 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "file": self.file, "kinds": self.kinds, "tool_count": len(self.tools),
                "approvals": self.counts(), "tools": self.tools, "hidden": self.hidden, "errors": self.errors,
                "warnings": self.warnings, "lint": self.lint, "connected": self.connected,
                "written": self.written}


# ---------------------------------------------------------------- credentials


def check_env_name(value: str, what: str) -> str:
    if not isinstance(value, str) or not ENV_NAME.fullmatch(value):
        raise EntryError(f"{what} must be an environment variable name (A-Z, 0-9, _), not a value")
    return value


def credential_spec(*, env: str | None = None, secret_ref: str | None = None,
                    prefix: str | None = None) -> dict[str, Any]:
    """One header or env spec: ``{env: NAME}`` or ``{secret_ref: p:h}``, plus a
    short scheme prefix."""
    if bool(env) == bool(secret_ref):
        raise EntryError("give exactly one of an env var name or a secret ref")
    if env:
        spec: dict[str, Any] = {"env": check_env_name(env, "auth env")}
    else:
        if not isinstance(secret_ref, str) or not SECRET_REF.fullmatch(secret_ref):
            raise EntryError("secret ref must look like provider:handle")
        spec = {"secret_ref": secret_ref}
    if prefix:
        from fabri.tools.registration import looks_like_secret

        if len(prefix) > 16 or looks_like_secret(prefix.strip() or "x"):
            raise EntryError("auth prefix must be a short scheme such as 'Bearer '")
        spec["prefix"] = prefix
    return spec


def auth_headers(*, auth_env: str | None = None, secret_ref: str | None = None,
                 auth_header: str | None = None, auth_prefix: str | None = "Bearer ",
                 header_env: Iterable[str] | Mapping[str, str] | None = None) -> dict[str, Any]:
    """Header specs from CLI-style options. `header_env` is ``Name=ENV_VAR``
    strings or a {Name: ENV_VAR} mapping."""
    out: dict[str, Any] = {}
    if auth_env or secret_ref:
        header = auth_header or "Authorization"
        if not HEADER_NAME.fullmatch(header):
            raise EntryError("auth header must be a header name")
        out[header] = credential_spec(env=auth_env, secret_ref=secret_ref, prefix=auth_prefix)
    pairs = header_env.items() if isinstance(header_env, Mapping) else (
        tuple(item.split("=", 1)) if "=" in item else (item, "") for item in header_env or ())
    for header, var in pairs:
        if not HEADER_NAME.fullmatch(str(header)) or not var:
            raise EntryError("headers take Name=ENV_VAR")
        out[str(header)] = {"env": check_env_name(str(var), f"header {header}")}
    return out


def env_specs(items: Iterable[str] | Mapping[str, str] | None) -> dict[str, Any]:
    """MCP stdio env from ``NAME`` (pass NAME through) or ``NAME=SOURCE_VAR``."""
    out: dict[str, Any] = {}
    pairs = items.items() if isinstance(items, Mapping) else (
        tuple(item.split("=", 1)) if "=" in item else (item, item) for item in items or ())
    for child, source in pairs:
        check_env_name(str(child), "env name")
        out[str(child)] = {"env": check_env_name(str(source), f"env {child} source")}
    return out


def check_url(url: Any, what: str) -> str:
    parsed = urlparse(url) if isinstance(url, str) else None
    if parsed is None or parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise EntryError(f"{what} must be an http or https URL")
    if parsed.username or parsed.password:
        raise EntryError(f"{what} must not embed credentials")
    for key, _ in parse_qsl(parsed.query):
        if key.lower() in _SECRET_QUERY_KEYS:
            raise EntryError(f"{what} must not carry a credential in the query string ({key}); use --auth-env")
    return url


def _secretish_arg(arg: str) -> bool:
    from fabri.tools.registration import looks_like_secret

    value = arg.split("=", 1)[-1] if arg.startswith("-") else arg
    if value.startswith(("/", "./", "../", "~")) or Path(value).exists():
        return False
    return looks_like_secret(value)


def check_name(name: str) -> str:
    if not isinstance(name, str) or not ENTRY_NAME.fullmatch(name):
        raise EntryError(f"name {name!r} must be lowercase letters, digits and _ (start with a letter, max 40)")
    return name


def slug_name(text: str) -> str:
    name = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    name = re.sub(r"^[0-9_]+", "", name)[:40].rstrip("_")
    return name or "api"


def _floor(approval: str | None) -> str | None:
    return check_approval(approval) if approval else None


# ---------------------------------------------------------------- plans


def _read_spec(spec: str, *, spec_text: str | None, transport: httpx.BaseTransport | None,
               cwd: Path | None) -> tuple[str, str | None]:
    """(text, url) for a spec given as a URL, a local path, or text."""
    if spec_text is not None:
        return spec_text, None
    if spec.startswith(("http://", "https://")):
        check_url(spec, "spec URL")
        try:
            with httpx.Client(transport=transport, timeout=30.0) as client:
                resp = client.get(spec)
        except httpx.HTTPError as exc:
            raise EntryError(f"fetching the spec failed: {exc.__class__.__name__}") from None
        if resp.status_code >= 400:
            raise EntryError(f"fetching the spec failed: HTTP {resp.status_code}")
        return resp.text, spec
    path = Path(spec)
    if not path.is_absolute() and cwd is not None:
        path = cwd / path
    try:
        return path.read_text(encoding="utf-8"), None
    except OSError as exc:
        raise EntryError(f"cannot read spec {spec}: {exc.strerror or exc}") from None


def openapi_entry(
    spec: str | None = None,
    *,
    spec_text: str | None = None,
    name: str | None = None,
    include: Sequence[str] | None = None,
    exclude: Sequence[str] | None = None,
    methods: Sequence[str] | None = None,
    allow_delete: bool = False,
    approval: str | None = None,
    base_url: str | None = None,
    headers: Mapping[str, Any] | None = None,
    snapshot: bool | None = None,
    strip_prefixes: Sequence[str] | None = None,
    transport: httpx.BaseTransport | None = None,
    cwd: str | Path | None = None,
) -> ToolEntry:
    """Plan an OpenAPI source. A local file or text is copied into
    ``tools/specs/``; a URL stays a URL unless `snapshot` is true. `approval`
    is a floor: each operation gets at least that class, never less than its
    own default."""
    if not spec and spec_text is None:
        raise EntryError("give a spec file, URL or text")
    text, url = _read_spec(spec or "", spec_text=spec_text, transport=transport,
                           cwd=Path(cwd) if cwd else Path.cwd())
    try:
        doc = load_openapi(yaml.safe_load(text) if text.strip() else {})
    except (OpenAPIError, yaml.YAMLError) as exc:
        raise EntryError(f"spec: {exc}") from None
    if name is None:
        title = str((doc.get("info") or {}).get("title") or "")
        stem = Path(urlparse(spec).path if url else (spec or "")).stem
        name = slug_name(title or stem or "api")
    check_name(name)
    floor = _floor(approval)
    if base_url:
        check_url(base_url, "base_url")
    method_list = [m.upper() for m in methods] if methods else None
    if allow_delete:
        method_list = sorted(set(method_list or _ALL_METHODS) | {"DELETE"}, key=_ALL_METHODS.index)
    decl: dict[str, Any] = {"kind": "openapi", "name": name}
    keep_url = url is not None and not snapshot
    spec_rel = None
    if keep_url:
        decl["spec"] = url
    else:
        suffix = ".json" if text.lstrip().startswith("{") else ".yaml"
        spec_rel = f"specs/{name}{suffix}"
        decl["spec"] = spec_rel
    if base_url:
        decl["base_url"] = base_url
    if method_list:
        decl["methods"] = method_list
    if strip_prefixes:
        decl["strip_prefixes"] = list(strip_prefixes)
    if include:
        decl["allow"] = list(include)
    if exclude:
        decl["deny"] = list(exclude)
    if headers:
        decl["headers"] = dict(headers)
    ops = parse_operations(doc, strip_prefixes=tuple(strip_prefixes) if strip_prefixes else ("api",))
    ops = filter_operations(ops, allow=include, deny=exclude, methods=method_list)
    if not ops:
        raise EntryError("no operations match; check --include, --exclude and --methods")
    approvals = {}
    for op in ops:
        final = stricter(op.approval, floor) if floor else op.approval
        if final != default_approval_for_method(op.method):
            approvals[op.name] = final
    if approvals:
        decl["approval"] = approvals
    return ToolEntry(name=name, how="openapi", data={"sources": [decl], "allow": [op.name for op in ops]},
                     spec_rel=spec_rel, spec_text=None if keep_url else text, spec_doc=doc,
                     approval_floor=floor)


def mcp_entry(
    name: str,
    *,
    command: str | Sequence[str] | None = None,
    url: str | None = None,
    env: Iterable[str] | Mapping[str, str] | None = None,
    headers: Mapping[str, Any] | None = None,
    include: Sequence[str] | None = None,
    approval: str | None = None,
    connect: bool = True,
) -> ToolEntry:
    """Plan an MCP source: a stdio `command` or a Streamable HTTP `url`.
    `include` narrows the server's tools by remote name (fnmatch)."""
    check_name(name)
    if bool(command) == bool(url):
        raise EntryError("give exactly one of --command or --url")
    decl: dict[str, Any] = {"kind": "mcp", "name": name}
    if command:
        argv = shlex.split(command) if isinstance(command, str) else [str(c) for c in command]
        if not argv:
            raise EntryError("--command is empty")
        if any(_secretish_arg(arg) for arg in argv):
            raise EntryError("--command holds what looks like a credential; pass it with --env NAME instead")
        decl["command"] = argv
        if env:
            decl["env"] = env_specs(env)
        if headers:
            raise EntryError("headers apply to --url servers; a stdio server takes --env")
    else:
        decl["url"] = check_url(url, "--url")
        if env:
            raise EntryError("--env applies to --command servers; an HTTP server takes --auth-env or --header")
        if headers:
            decl["headers"] = dict(headers)
    floor = _floor(approval)
    if floor == "always_approve":
        decl["approval"] = floor
    allow = [f"mcp_{name}_{p}" for p in include] if include else [f"mcp_{name}_*"]
    return ToolEntry(name=name, how="mcp", data={"sources": [decl], "allow": allow}, connect=connect,
                     approval_floor=floor)


def connector_tool_entry(kind: str, *, name: str | None = None, token_env: str | None = None,
                         workspace: str | None = None, remote: bool = False, approval: str | None = None,
                         connect: bool = False) -> ToolEntry:
    if token_env:
        check_env_name(token_env, "--token-env")
    if workspace and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", workspace):
        raise EntryError("--workspace must be a handle such as acme or T0123")
    if kind not in CONNECTORS:
        raise EntryError(f"unknown connector {kind!r}; known: {', '.join(sorted(CONNECTORS))}")
    name = check_name(name or kind)
    try:
        data = connector_entry(kind, name=name, token_env=token_env, workspace=workspace, remote=remote)
    except ConnectorError as exc:
        raise EntryError(str(exc)) from None
    floor = _floor(approval)
    if floor == "always_approve":
        for source in data["sources"]:
            source["approval"] = "always_approve"
    return ToolEntry(name=name, how=f"connector {kind}", data=data, connect=connect, approval_floor=floor)


# ---------------------------------------------------------------- preview and write


def entry_path(layer_dir: str | Path, name: str) -> Path:
    return Path(layer_dir) / SOURCES_DIR / f"{check_name(name)}.yaml"


def _allowed(names: Sequence[str], patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(n, p) for n in names for p in patterns)


def preview_entry(
    entry: ToolEntry,
    layer_dir: str | Path,
    *,
    policy: ToolPolicy | None = None,
    transport: httpx.BaseTransport | None = None,
    store: Any = None,
) -> EntryReport:
    """Build the entry's tools under `policy` without writing anything."""
    from fabri.setups.store import validate_tools_yaml
    from fabri.tools.sources import build_source_tools

    policy = policy or ToolPolicy.permissive()
    rel = (SOURCES_DIR / f"{entry.name}.yaml").as_posix()
    report = EntryReport(name=entry.name, file=rel, kinds=entry.kinds(), connected=entry.connect)
    try:
        report.warnings += validate_tools_yaml(entry.to_yaml(), policy, rel)
    except SetupError as exc:
        report.errors.append(str(exc))
        return report
    allow = [str(a) for a in entry.data.get("allow") or []]
    for decl in entry.data.get("sources") or []:
        if decl.get("kind") == "mcp" and not entry.connect:
            continue
        if "DELETE" in [str(m).upper() for m in decl.get("methods") or []] and not policy.allow_delete:
            report.errors.append(f"{decl.get('name')}: the policy does not allow DELETE (allow_delete: false); "
                                 "drop --allow-delete or widen the policy")
            continue
        built_decl = dict(decl)
        if entry.spec_doc is not None and decl.get("kind") == "openapi":
            built_decl["spec"] = entry.spec_doc
        try:
            built = build_source_tools(built_decl, base_dir=Path(layer_dir) / "tools", policy=policy,
                                       transport=transport, store=store)
        except Exception as exc:  # noqa: BLE001 -- reported to the caller, nothing is written
            report.errors.append(f"{decl.get('name')}: {exc}")
            continue
        for tool in built:
            names = [tool.manifest.name, *(tool.manifest.aliases or [])]
            if not _allowed(names, allow) or not policy.name_allowed(tool.manifest.name, tool.manifest.aliases or ()):
                report.hidden.append(tool.manifest.name)
                continue
            approval = tool.manifest.approval or "write"
            if entry.approval_floor:
                approval = stricter(approval, entry.approval_floor)
            floor = policy.floor_for(tool.manifest.name, tool.manifest.aliases or ())
            if floor:
                approval = stricter(approval, floor)
            report.tools.append({"name": tool.manifest.name, "approval": approval, "kind": tool.kind,
                                 "method": tool.method, "path": tool.path, "source": tool.source})
        client = built[0].extra.get("client") if built else None
        if client is not None and hasattr(client, "close"):
            client.close()
    built_names = {t["name"] for t in report.tools} | set(report.hidden)
    missing = [a for a in allow if not any(ch in a for ch in "*?[") and a not in built_names]
    if missing and not report.errors and (entry.connect or "mcp" not in report.kinds):
        report.warnings.append(f"allowed but not built: {', '.join(missing)}")
    if entry.connect and not report.errors:
        _pin_mcp(entry, report)
    return report


def _pin_mcp(entry: ToolEntry, report: EntryReport) -> None:
    """For a server reached at preview time, write the tool names it listed
    into `allow` (a new server tool then needs review) and its always_approve
    tools into `approval`, so the file shows both."""
    mcp = [d for d in entry.data.get("sources") or [] if d.get("kind") == "mcp"]
    if not mcp:
        return
    names = {d["name"] for d in mcp}
    entry.data["allow"] = [a for a in entry.data.get("allow") or []
                           if not any(a.startswith(f"mcp_{n}_") for n in names)]
    entry.data["allow"] += [t["name"] for t in report.tools if t["source"] in names]
    for decl in mcp:
        if isinstance(decl.get("approval"), str):
            continue
        raised = {t["name"]: t["approval"] for t in report.tools
                  if t["source"] == decl["name"] and t["approval"] != "write"}
        if raised:
            decl["approval"] = raised


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def audit(layer_dir: str | Path, actor: str, action: str, scope: str, name: str, payload: Any) -> None:
    """Append one line to ``<layer>/.fabri-audit.jsonl``, the same file and
    shape the Setups API writes."""
    import hashlib

    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "actor": actor or "cli",
                       "action": action, "scope": scope, "kind": "tools", "name": name, "content_hash": digest})
    with open(Path(layer_dir) / AUDIT_FILE, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _clash(entry: ToolEntry, layer_dir: Path, chain: Sequence[Layer]) -> tuple[list[str], list[str]]:
    """(errors, warnings) for source names already used in the stack."""
    errors, warnings = [], []
    names = {str(s.get("name")) for s in entry.data.get("sources") or []}
    tools_yaml = read_yaml(layer_dir / "tools" / "tools.yaml")
    here = {str(s.get("name")) for s in tools_yaml.get("sources") or [] if isinstance(s, Mapping)}
    for name in sorted(names & here):
        errors.append(f"source {name} is already declared in this layer's tools/tools.yaml")
    for other in sorted((layer_dir / SOURCES_DIR).glob("*.yaml")) if (layer_dir / SOURCES_DIR).is_dir() else []:
        if other.stem == entry.name or other.name.startswith("."):
            continue
        taken = {str(s.get("name")) for s in read_yaml(other).get("sources") or [] if isinstance(s, Mapping)}
        for name in sorted(names & taken):
            errors.append(f"source {name} is already declared in tools/sources/{other.name}")
    resolved_here = layer_dir.resolve()
    for layer in chain:
        if layer.path.resolve() == resolved_here:
            continue
        shallow = {str(s.get("name")) for s in read_yaml(layer.path / "tools" / "tools.yaml").get("sources") or []
                   if isinstance(s, Mapping)}
        shallow |= {p.stem for p in (layer.path / SOURCES_DIR).glob("*.yaml")} \
            if (layer.path / SOURCES_DIR).is_dir() else set()
        for name in sorted(names & shallow):
            warnings.append(f"source {name} also exists in layer {layer.name}; the deeper layer's copy wins")
    return errors, warnings


def _tool_clashes(entry: ToolEntry, report: EntryReport, chain: Sequence[Layer], policy: ToolPolicy | None,
                  transport: Any, store: Any) -> list[str]:
    """Tool names the entry would build that another source in the stack
    already builds."""
    from fabri.setups import resolve_setup, resolve_tools

    own = {str(s.get("name")) for s in entry.data.get("sources") or []}
    existing = resolve_tools(resolve_setup(chain, use_cache=False), policy=policy or ToolPolicy.permissive(),
                             transport=transport, store=store, catalog=False)
    taken = {}
    for tool in existing.tools.values():
        if tool.source not in own:
            for n in (tool.name, *tool.aliases):
                taken[n] = f"{tool.source} in {tool.layer}"
    return [f"tool {t['name']} is already built by source {taken[t['name']]}; rename or --exclude it"
            for t in report.tools if t["name"] in taken]


def add_entry(
    layer_dir: str | Path,
    entry: ToolEntry,
    *,
    chain: Sequence[Layer] | None = None,
    policy: ToolPolicy | None = None,
    replace: bool = False,
    dry_run: bool = False,
    actor: str = "cli",
    scope: str = "",
    transport: httpx.BaseTransport | None = None,
    store: Any = None,
) -> EntryReport:
    """Preview, then write the entry and its spec and lint the stack. Nothing
    is written when the preview has errors or `dry_run` is set."""
    layer_dir = Path(layer_dir)
    if not layer_dir.is_dir():
        raise EntryError(f"layer directory {layer_dir} does not exist")
    chain = list(chain) if chain else [Layer(scope or layer_dir.name, layer_dir)]
    path = entry_path(layer_dir, entry.name)
    report = preview_entry(entry, layer_dir, policy=policy, transport=transport, store=store)
    errors, warnings = _clash(entry, layer_dir, chain)
    report.errors = errors + report.errors
    report.warnings += warnings
    if path.exists() and not replace:
        report.errors.insert(0, f"{report.file} already exists; pass replace to overwrite it")
    if report.tools and not report.errors:
        report.errors += _tool_clashes(entry, report, chain, policy, transport, store)
    if report.errors or dry_run:
        return report
    if entry.spec_rel and entry.spec_text is not None:
        _atomic_write(layer_dir / "tools" / entry.spec_rel, entry.spec_text)
    _atomic_write(path, entry.to_yaml())
    report.written = True
    audit(layer_dir, actor, "add-tool-source", scope, entry.name, entry.data)
    soft = set() if entry.connect else {str(d["name"]) for d in entry.data["sources"] if d.get("kind") == "mcp"}
    report.lint = lint_entry(chain, policy, entry.name, [t["name"] for t in report.tools], soft_sources=soft)
    return report


def lint_entry(chain: Sequence[Layer], policy: ToolPolicy | None, name: str,
               tool_names: Sequence[str], soft_sources: Iterable[str] = ()) -> list[dict[str, str]]:
    """Lint findings and tool build errors in the stack that concern one
    entry. Build errors of `soft_sources` (servers the caller chose not to
    reach) are warnings."""
    soft = set(soft_sources)
    from fabri.setups import lint_setup, resolve_setup, resolve_tools

    resolved = resolve_setup(chain, use_cache=False)
    tools = resolve_tools(resolved, policy=policy or ToolPolicy.permissive(), use_cache=False)
    findings = lint_setup(resolved, tool_names=[n for t in tools.tools.values() for n in [t.name, *t.aliases]],
                          policy=policy, tool_set=tools)
    mine = set(tool_names)
    sources = {str(s["decl"].get("name")) for s in resolved.tools["sources"] if s.get("file") == f"tools/sources/{name}.yaml"}
    out = []
    for f in findings:
        if f.item in (f"tools/sources/{name}.yaml", *(f"tools/{s}" for s in sources)) \
                or any(t in f.message for t in mine):
            out.append({"severity": f.level, "item": f.item, "layer": f.layer, "message": f.message})
    for error in tools.errors:
        if any(error.startswith(f"{layer.name}:{s}:") or f":{s}:" in error for layer in chain for s in sources):
            # A credential missing here is normal (it lives where the host runs); the file is still right.
            missing = "is not set" in error or "credential unavailable" in error \
                or any(f":{src}:" in error for src in soft)
            out.append({"severity": "warning" if missing else "error", "item": f"tools/sources/{name}.yaml",
                        "layer": "", "message": error})
    return out


# ---------------------------------------------------------------- list, toggle, remove


def list_entries(layer_dir: str | Path) -> list[dict[str, Any]]:
    """Every source declared in one layer: tools/sources files, tools.yaml
    and registered tools, each with where it lives and whether it is on."""
    layer_dir = Path(layer_dir)
    out: list[dict[str, Any]] = []
    directory = layer_dir / SOURCES_DIR
    for path in sorted(directory.glob("*.yaml")) if directory.is_dir() else []:
        if path.name.startswith("."):
            continue
        try:
            data = read_yaml(path)
        except SetupError as exc:
            out.append({"name": path.stem, "file": f"tools/sources/{path.name}", "origin": "sources",
                        "kinds": [], "sources": [], "enabled": False, "allow": [], "error": str(exc)})
            continue
        sources = [s for s in data.get("sources") or [] if isinstance(s, Mapping)]
        out.append({"name": path.stem, "file": f"tools/sources/{path.name}", "origin": "sources",
                    "kinds": sorted({str(s.get("kind")) for s in sources}),
                    "sources": [str(s.get("name")) for s in sources],
                    "enabled": any(s.get("enabled") is not False for s in sources),
                    "allow": [str(a) for a in data.get("allow") or []]})
    tools_yaml = read_yaml(layer_dir / "tools" / "tools.yaml")
    for decl in tools_yaml.get("sources") or []:
        if isinstance(decl, Mapping) and decl.get("name"):
            out.append({"name": str(decl["name"]), "file": "tools/tools.yaml", "origin": "tools.yaml",
                        "kinds": [str(decl.get("kind"))], "sources": [str(decl["name"])],
                        "enabled": decl.get("enabled") is not False, "allow": []})
    from fabri.tools.registration import list_registered

    for tool in list_registered(layer_dir):
        out.append({"name": tool.name, "file": f"tools/registered/{tool.name}.json", "origin": "registered",
                    "kinds": [tool.kind], "sources": [tool.name], "enabled": tool.enabled, "allow": []})
    return out


def _find(layer_dir: Path, name: str) -> dict[str, Any]:
    for item in list_entries(layer_dir):
        if item["name"] == name and item["origin"] in ("sources", "registered"):
            return item
    for item in list_entries(layer_dir):
        if item["name"] == name:
            raise EntryError(f"{name} is declared in {item['file']}; edit that file to change it")
    raise EntryError(f"no tool source named {name!r} in this layer")


def set_entry_enabled(layer_dir: str | Path, name: str, enabled: bool, *, actor: str = "cli",
                      scope: str = "") -> bool:
    """Turn an entry on or off. Returns True when the file changed."""
    layer_dir = Path(layer_dir)
    item = _find(layer_dir, name)
    if item["origin"] == "registered":
        from fabri.tools.registration import set_enabled

        changed = set_enabled(layer_dir, name, enabled, actor=actor).changed
    else:
        path = layer_dir / item["file"]
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}
        changed = False
        for decl in data.get("sources") or []:
            if isinstance(decl, dict) and (decl.get("enabled") is not False) != enabled:
                if enabled:
                    decl.pop("enabled", None)
                else:
                    decl["enabled"] = False
                changed = True
        if changed:
            lines = text.splitlines()
            head = next((i for i, line in enumerate(lines) if not line.startswith("#")), len(lines))
            comments = "".join(line + "\n" for line in lines[:head])
            _atomic_write(path, comments + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100))
    if changed:
        audit(layer_dir, actor, "enable-tool-source" if enabled else "disable-tool-source", scope, name,
              {"enabled": enabled})
    return changed


def remove_entry(layer_dir: str | Path, name: str, *, actor: str = "cli", scope: str = "") -> list[str]:
    """Delete an entry and the spec copy it owns. Returns the removed paths."""
    layer_dir = Path(layer_dir)
    item = _find(layer_dir, name)
    if item["origin"] == "registered":
        from fabri.tools.registration import unregister_tool

        unregister_tool(layer_dir, name, actor=actor)
        removed = [item["file"]]
    else:
        path = layer_dir / item["file"]
        data = read_yaml(path)
        removed = [item["file"]]
        for decl in data.get("sources") or []:
            spec = decl.get("spec") if isinstance(decl, Mapping) else None
            if isinstance(spec, str) and re.fullmatch(rf"specs/{re.escape(name)}\.(json|yaml)", spec):
                spec_path = layer_dir / "tools" / spec
                if spec_path.is_file():
                    spec_path.unlink()
                    removed.append(f"tools/{spec}")
        path.unlink()
    audit(layer_dir, actor, "remove-tool-source", scope, name, {"removed": removed})
    return removed


def stack_summary(tool_set: Any, layer_name: str | None = None) -> dict[str, dict[str, Any]]:
    """{source name: {tools, approvals}} over a ResolvedToolSet, optionally
    for one layer only."""
    out: dict[str, dict[str, Any]] = {}
    for tool in tool_set.tools.values():
        if layer_name is not None and tool.layer != layer_name:
            continue
        row = out.setdefault(tool.source, {"tools": 0, "approvals": {c: 0 for c in APPROVAL_CLASSES}})
        row["tools"] += 1
        row["approvals"][tool.approval] = row["approvals"].get(tool.approval, 0) + 1
    return out



__all__ = [
    "EntryError", "EntryReport", "ToolEntry", "add_entry", "auth_headers", "connector_tool_entry", "env_specs",
    "list_entries", "mcp_entry", "openapi_entry", "preview_entry", "remove_entry", "set_entry_enabled",
    "stack_summary",
]
