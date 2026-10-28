"""Setups API for ``fabri serve``: manage layered setups and dynamic tools over
HTTP. Contract: docs/setups-api.md.

This module only maps HTTP onto :mod:`fabri.setups` and :mod:`fabri.tools`.
It owns four things those packages do not: the scope-to-directory mapping,
the auth gate, the audit line on every write, and the secret scrub on every
response.

Auth (docs/setups-api.md, "Every route requires the service's existing auth"):

- Reads need a Studio session (when ``auth.enabled``), or a bearer token equal
  to ``FABRI_SERVICE_TOKEN`` / ``FABRI_ADMIN_TOKEN``. With none of those
  configured the API is open, like the rest of an un-authed ``fabri serve``.
- Writes need the admin role: ``Authorization: Bearer $FABRI_ADMIN_TOKEN``, a
  session whose email is listed under ``auth.admins`` in the service config,
  or ``FABRI_SERVICE_TOKEN`` when neither an admin token nor session auth is
  configured (the one shared secret is then the admin). With nothing
  configured at all the gate is open and the actor is ``anonymous``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import string
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlsplit

import yaml

from fabri.setups import (
    KINDS as _STORE_KINDS,  # noqa: F401  (documented dependency)
    Layer,
    SetupError,
    Setups,
    item_history,
    item_revision,
    read_item,
    resolve_setup,
    validate_item,
)
from fabri.setups.layers import SLUG, check_slug, read_yaml, split_frontmatter
from fabri.setups.store import allowed_file
from fabri.setups.tool_entries import (
    EntryError,
    add_entry,
    auth_headers,
    connector_tool_entry,
    list_entries,
    mcp_entry,
    openapi_entry,
    remove_entry,
    set_entry_enabled,
    stack_summary,
)
from fabri.setups.toolset import default_state_dir, diff_snapshots
from fabri.tools.approval import ApprovalError
from fabri.tools.catalog import read_state, save_catalog, write_state
from fabri.tools.policy import PolicyError, ToolPolicy
from fabri.tools.probe import denied_key, probe_tools
from fabri.tools.registration import (
    RegisteredTool,
    RegistrationError,
    list_registered,
    looks_like_secret,
    register_tool,
    set_enabled,
    unregister_tool,
)
from fabri.tools.sources import TOOL_NAME, ToolSourceError

API_KINDS = ("agents", "skills", "context", "data", "tools", "plays", "setup")
SCOPE_KINDS = ("baseline", "pack", "org", "project")
AUDIT_FILE = ".fabri-audit.jsonl"
MAX_BODY_BYTES = 8 * 1024 * 1024
REDACTED = "[redacted]"
DEFAULT_LAYERS = (
    {"kind": "baseline", "path": "baseline"},
    {"kind": "pack", "path": "packs/{pack}"},
    {"kind": "org", "path": "companies/{org}"},
    {"kind": "project", "path": "companies/{org}/projects/{project}"},
)
_FIELDS = {"baseline": set(), "pack": {"pack"}, "org": {"org"}, "project": {"org", "project"}}
_SCOPE_RE = re.compile(
    r"(?P<baseline>baseline)"
    r"|pack:(?P<pack>[a-z0-9][a-z0-9-]*)"
    r"|org:(?P<org>[a-z0-9][a-z0-9-]*)(?:/project:(?P<project>[a-z0-9][a-z0-9-]*))?"
)
# Keys whose string values are never returned, unless the value is a reference.
_SECRET_KEY_PARTS = ("secret", "token", "password", "passwd", "apikey", "authorization", "cookie",
                     "credential", "privatekey")
_REFERENCE_KEYS = frozenset({"secret_ref", "header_env", "env", "prefix", "header", "secret_env",
                             "api_key_env", "session_secret_env"})


class ApiError(Exception):
    """A request the API refuses. `reasons` carries policy/validation detail."""

    def __init__(self, status: int, message: str, reasons: list[str] | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.reasons = reasons


# ---------------------------------------------------------------- ui config

# What a setups config's `ui:` block may set. Studio keeps its own defaults
# and applies only what is given here.
UI_LABELS = frozenset({
    "agent_type", "agent_types", "company", "companies", "project", "projects",
    "global_skills", "skills", "context", "data", "tools", "merged", "preview", "worker", "workers",
})
UI_TABS = ("agents", "skills", "companies", "projects", "tools", "merged", "preview")


def check_ui_config(ui: Any) -> dict[str, Any]:
    """Validate a `ui:` block: `title`, `intro`, `labels` (UI_LABELS keys to
    strings), `tabs` (an ordered subset of UI_TABS) and `default_tab`."""
    if ui is None:
        return {}
    if not isinstance(ui, Mapping):
        raise SetupError("ui must be a mapping")
    unknown = set(ui) - {"title", "intro", "labels", "tabs", "default_tab"}
    if unknown:
        raise SetupError(f"ui: unknown keys {sorted(unknown)}")
    out: dict[str, Any] = {}
    for key in ("title", "intro"):
        if key in ui:
            if not isinstance(ui[key], str) or not ui[key].strip() or len(ui[key]) > 600:
                raise SetupError(f"ui.{key} must be a string of 1 to 600 characters")
            out[key] = ui[key]
    labels = ui.get("labels") or {}
    if not isinstance(labels, Mapping) or not all(isinstance(v, str) and 0 < len(v) <= 40 for v in labels.values()):
        raise SetupError("ui.labels must map label keys to strings of 1 to 40 characters")
    bad = set(labels) - UI_LABELS
    if bad:
        raise SetupError(f"ui.labels: unknown keys {sorted(bad)} (known: {sorted(UI_LABELS)})")
    if labels:
        out["labels"] = dict(labels)
    if "tabs" in ui:
        tabs = ui["tabs"]
        if not isinstance(tabs, list) or not tabs or any(t not in UI_TABS for t in tabs) or len(set(tabs)) != len(tabs):
            raise SetupError(f"ui.tabs must be a non-empty list of distinct tabs from {list(UI_TABS)}")
        out["tabs"] = list(tabs)
    if "default_tab" in ui:
        if ui["default_tab"] not in out.get("tabs", UI_TABS):
            raise SetupError("ui.default_tab must be one of the shown tabs")
        out["default_tab"] = ui["default_tab"]
    return out


# ---------------------------------------------------------------- scopes


@dataclass(frozen=True)
class Scope:
    kind: str  # baseline | pack | org | project
    pack: str | None = None
    org: str | None = None
    project: str | None = None

    @property
    def text(self) -> str:
        if self.kind == "baseline":
            return "baseline"
        if self.kind == "pack":
            return f"pack:{self.pack}"
        if self.kind == "org":
            return f"org:{self.org}"
        return f"org:{self.org}/project:{self.project}"


def parse_scope(value: Any) -> Scope:
    if not isinstance(value, str) or not _SCOPE_RE.fullmatch(value):
        raise ApiError(400, "scope must be baseline, pack:<name>, org:<org> or org:<org>/project:<project> "
                            "(names are lowercase slugs)")
    m = _SCOPE_RE.fullmatch(value)
    assert m is not None
    if m.group("baseline"):
        return Scope("baseline")
    if m.group("pack"):
        return Scope("pack", pack=m.group("pack"))
    if m.group("project"):
        return Scope("project", org=m.group("org"), project=m.group("project"))
    return Scope("org", org=m.group("org"))


# ---------------------------------------------------------------- scrub


def _is_secret_key(key: str) -> bool:
    norm = re.sub(r"[^a-z0-9]", "", key.lower())
    return any(part in norm for part in _SECRET_KEY_PARTS)


def scrub(value: Any, key: str = "") -> Any:
    """A copy of a JSON value with credential-looking data replaced. Applied
    to every response. References (secret_ref, env var names) pass through."""
    if isinstance(value, Mapping):
        return {str(k): scrub(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, key) for v in value]
    if isinstance(value, str):
        if key in _REFERENCE_KEYS:
            return value
        if _is_secret_key(key) and value:
            return REDACTED
        if looks_like_secret(value):
            return REDACTED
    return value


def _literal_header_secret(header: str, spec: Any) -> bool:
    """True for a tools.yaml header or env spec that holds a raw secret value."""
    literal = spec if isinstance(spec, str) else (spec.get("value") if isinstance(spec, Mapping) else None)
    if not isinstance(literal, str) or not literal:
        return False
    return denied_key(header) or looks_like_secret(literal)


def _tools_yaml_literals(data: Any) -> list[str]:
    found = []
    sources = data.get("sources") if isinstance(data, Mapping) else None
    for decl in sources if isinstance(sources, list) else []:
        if not isinstance(decl, Mapping):
            continue
        for field_name in ("headers", "env"):
            specs = decl.get(field_name)
            if isinstance(specs, Mapping):
                found += [f"{decl.get('name')}.{field_name}.{h}" for h, s in specs.items()
                          if _literal_header_secret(str(h), s)]
    return found


def _redact_tools_yaml(text: str) -> tuple[str, bool]:
    """tools.yaml text with literal header/env secrets replaced. Text with
    none comes back byte for byte."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return text, False
    if not _tools_yaml_literals(data):
        return text, False
    for decl in data["sources"]:
        for field_name in ("headers", "env"):
            specs = decl.get(field_name) if isinstance(decl, dict) else None
            if isinstance(specs, dict):
                for h, s in list(specs.items()):
                    if _literal_header_secret(str(h), s):
                        specs[h] = REDACTED
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True), True


# ---------------------------------------------------------------- config


def _check_template(kind: str, template: Any) -> str:
    if not isinstance(template, str) or not template or template.startswith(("/", "~")) or "\\" in template:
        raise SetupError(f"layers: {kind} path must be a relative template")
    parts = template.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise SetupError(f"layers: {kind} path {template!r} has an empty or relative segment")
        fields = [f for _, f, spec, conv in string.Formatter().parse(part) if f is not None]
        for name in fields:
            if name not in _FIELDS[kind] and not (kind == "project" and name == "org"):
                raise SetupError(f"layers: {kind} path may only use {sorted(_FIELDS[kind])}, not {{{name}}}")
        if fields and part != "{" + fields[0] + "}":
            raise SetupError(f"layers: {kind} path placeholders must fill a whole segment ({part!r})")
    needed = _FIELDS[kind]
    used = {f for part in parts for _, f, _, _ in string.Formatter().parse(part) if f}
    if used != needed:
        raise SetupError(f"layers: {kind} path must use {sorted(needed)} (uses {sorted(used)})")
    return template


class SetupsApi:
    """Everything the Setups API needs: where the layers live, the tool
    policy, and the request handlers."""

    def __init__(
        self,
        root: str | Path,
        *,
        layers: list[Mapping[str, Any]] | None = None,
        global_allow: str | Path | None = None,
        policy: str = "strict",
        transport: Any = None,
        store: Any = None,
        ui: Mapping[str, Any] | None = None,
    ):
        self.ui = check_ui_config(ui)
        self.root = Path(root).resolve()
        templates: dict[str, str] = {}
        for item in layers if layers is not None else DEFAULT_LAYERS:
            kind = item.get("kind") if isinstance(item, Mapping) else None
            if kind not in SCOPE_KINDS or kind in templates:
                raise SetupError(f"layers: kind must be one of {SCOPE_KINDS}, each at most once (got {kind!r})")
            templates[kind] = _check_template(kind, item.get("path"))
        self.templates = templates
        if policy not in ("strict", "permissive"):
            raise SetupError("policy must be strict or permissive")
        self.policy_name = policy
        base = ToolPolicy() if policy == "strict" else ToolPolicy.permissive()
        if global_allow is not None:
            allow_path = Path(global_allow)
            if not allow_path.is_absolute():
                allow_path = self.root / allow_path
            self.policy = ToolPolicy.from_file(allow_path, base=base)
        else:
            self.policy = base
        self.transport = transport
        self.store = store
        self._write_lock = threading.RLock()
        self._routes = self._build_routes()
        self._dyn_routes = [
            ("GET", re.compile(r"/setups/agent-types/(?P<type>[^/]+)"), self.get_agent_type, False),
            ("PUT", re.compile(r"/setups/agent-types/(?P<type>[^/]+)"), self.put_agent_type, True),
            ("GET", re.compile(r"/setups/orgs/(?P<org>[^/]+)/agent-types"), self.get_org_agent_types, False),
            ("PUT", re.compile(r"/setups/orgs/(?P<org>[^/]+)/agent-types"), self.put_org_agent_types, True),
        ]

    # ---- construction from CLI flags

    @classmethod
    def from_files(cls, root: str | Path | None, config: str | Path | None, **kwargs: Any) -> "SetupsApi":
        cfg: dict[str, Any] = {}
        base_dir = Path.cwd()
        if config is not None:
            config_path = Path(config)
            cfg = read_yaml(config_path)
            base_dir = config_path.resolve().parent
        unknown = set(cfg) - {"root", "layers", "global_allow", "policy", "ui"}
        if unknown:
            raise SetupError(f"setups config: unknown keys {sorted(unknown)}")
        if root is None:
            if not cfg.get("root"):
                raise SetupError("setups root is required (--setups-root, or `root:` in the config)")
            root_path = Path(str(cfg["root"]))
            root_path = root_path if root_path.is_absolute() else base_dir / root_path
        else:
            root_path = Path(root)
        layers = cfg.get("layers")
        if layers is not None and not isinstance(layers, list):
            raise SetupError("setups config: layers must be a list")
        return cls(root_path, layers=layers, global_allow=cfg.get("global_allow"),
                   policy=str(cfg.get("policy") or "strict"), ui=cfg.get("ui"), **kwargs)

    # ---- paths

    def layer_path(self, scope: Scope) -> Path:
        template = self.templates.get(scope.kind)
        if template is None:
            raise ApiError(400, f"this server has no {scope.kind} layer configured")
        path = self.root / template.format(pack=scope.pack, org=scope.org, project=scope.project)
        resolved = path.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise ApiError(400, "scope resolves outside the layer root")
        return resolved

    def _existing(self, scope: Scope) -> Path:
        path = self.layer_path(scope)
        if not path.is_dir():
            raise ApiError(404, f"unknown scope {scope.text}")
        return path

    def _names_under(self, kind: str, org: str | None = None) -> list[str]:
        template = self.templates.get(kind)
        if template is None:
            return []
        parts = template.split("/")
        var = {"pack": "pack", "org": "org", "project": "project"}[kind]
        index = next(i for i, p in enumerate(parts) if p == "{" + var + "}")
        parent = self.root.joinpath(*[p.format(org=org) for p in parts[:index]]) if index else self.root
        try:
            parent = parent.resolve()
            if parent != self.root and self.root not in parent.parents:
                return []
            return sorted(p.name for p in parent.iterdir() if p.is_dir() and SLUG.fullmatch(p.name))
        except OSError:
            return []

    def org_packs(self, org: str) -> list[str]:
        org_dir = self.layer_path(Scope("org", org=org))
        packs = read_yaml(org_dir / "setup.yaml").get("packs") if org_dir.is_dir() else None
        return [p for p in packs if isinstance(p, str) and SLUG.fullmatch(p)] if isinstance(packs, list) else []

    def chain(self, scope: Scope) -> list[Layer]:
        """The layers that make up a scope's view, shallowest first. Layers
        whose directory does not exist are left out."""
        layers: list[Layer] = []

        def add(s: Scope) -> None:
            if s.kind in self.templates:
                path = self.layer_path(s)
                if path.is_dir():
                    layers.append(Layer(s.text, path))

        add(Scope("baseline"))
        if scope.kind == "pack":
            add(scope)
        elif scope.kind in ("org", "project"):
            assert scope.org is not None
            for pack in self.org_packs(scope.org) if "org" in self.templates else []:
                add(Scope("pack", pack=pack))
            add(Scope("org", org=scope.org))
            if scope.kind == "project":
                add(scope)
        return layers

    def setups(self, scope: Scope) -> Setups:
        return Setups(self.chain(scope), policy=self.policy, transport=self.transport, store=self.store)

    # ---- audit

    def audit(self, layer_dir: Path, actor: str, action: str, scope: Scope, kind: str, name: str,
              payload: Any) -> None:
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "actor": actor,
                           "action": action, "scope": scope.text, "kind": kind, "name": name,
                           "content_hash": digest})
        with self._write_lock:
            with open(layer_dir / AUDIT_FILE, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    # ---- routing

    def _build_routes(self) -> dict[tuple[str, str], tuple[Callable, bool]]:
        r = self
        return {
            ("GET", "/setups/scopes"): (r.get_scopes, False),
            ("POST", "/setups/scopes"): (r.post_scopes, True),
            ("GET", "/setups/items"): (r.get_items, False),
            ("GET", "/setups/item"): (r.get_item, False),
            ("GET", "/setups/item/history"): (r.get_item_history, False),
            ("PUT", "/setups/item"): (r.put_item, True),
            ("DELETE", "/setups/item"): (r.delete_item, True),
            ("POST", "/setups/item/disable"): (r.post_disable, True),
            ("GET", "/setups/resolved"): (r.get_resolved, False),
            ("GET", "/setups/preview"): (r.get_preview, False),
            ("GET", "/setups/ui"): (r.get_ui, False),
            ("POST", "/setups/import"): (r.post_import, True),
            ("GET", "/setups/lint"): (r.get_lint, False),
            ("GET", "/setups/agent-types"): (r.get_agent_types, False),
            ("GET", "/tools/catalog"): (r.get_catalog, False),
            ("POST", "/tools/register"): (r.post_register, True),
            ("POST", "/tools/unregister"): (r.post_unregister, True),
            ("POST", "/tools/enable"): (r.post_enable, True),
            ("POST", "/tools/sync"): (r.post_sync, True),
            ("POST", "/tools/probe"): (r.post_probe, True),
            ("GET", "/tools/sources"): (r.get_tool_sources, False),
            ("POST", "/tools/sources"): (r.post_tool_source, True),
            ("POST", "/tools/sources/enable"): (r.post_tool_source_enable, True),
            ("DELETE", "/tools/sources"): (r.delete_tool_source, True),
            ("GET", "/tools/connectors"): (r.get_connectors, False),
        }

    def matches(self, raw_path: str) -> bool:
        path = urlsplit(raw_path).path.rstrip("/")
        return any(p == path for _, p in self._routes) or any(rx.fullmatch(path) for _, rx, _, _ in self._dyn_routes)

    def handle(self, handler: Any, method: str) -> None:
        """Serve one request. `handler` is the http_server _Handler."""
        parts = urlsplit(handler.path)
        path = parts.path.rstrip("/")
        entry = self._routes.get((method, path))
        params: dict[str, str] = {}
        if entry is None:
            for m, rx, dfn, dwrite in self._dyn_routes:
                hit = rx.fullmatch(path)
                if m == method and hit:
                    entry, params = (dfn, dwrite), hit.groupdict()
                    break
        try:
            if entry is None:
                raise ApiError(405, f"{method} is not allowed on {path}")
            fn, write = entry
            actor = self._authorize(handler, write=write)
            query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
            body = self._read_body(handler) if method in ("POST", "PUT") else {}
            status, payload = fn(Request(actor=actor, query=query, body=body, params=params))
        except ApiError as exc:
            out: dict[str, Any] = {"error": exc.message}
            if exc.reasons:
                out["reasons"] = exc.reasons
            self._send(handler, exc.status, out)
            return
        except (SetupError, PolicyError, RegistrationError, ApprovalError, ToolSourceError, EntryError) as exc:
            self._send(handler, 400, {"error": str(exc), "reasons": [str(exc)]})
            return
        self._send(handler, status, payload)

    @staticmethod
    def _send(handler: Any, status: int, payload: Any) -> None:
        if payload is None:
            handler.send_response(status)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        handler._send_json(status, scrub(payload))

    @staticmethod
    def _read_body(handler: Any) -> dict[str, Any]:
        try:
            length = int(handler.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "invalid Content-Length") from None
        if length < 0 or length > MAX_BODY_BYTES:
            raise ApiError(413, f"body is over {MAX_BODY_BYTES} bytes")
        raw = handler.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except ValueError as exc:
            raise ApiError(400, f"invalid request JSON: {exc}") from None
        if not isinstance(body, dict):
            raise ApiError(400, "request body must be a JSON object")
        return body

    # ---- auth

    def _authorize(self, handler: Any, *, write: bool) -> str:
        service = handler.service
        principal = handler._principal()
        if principal is None:
            session_auth = bool(getattr(service, "auth_enabled", False))
            read_open = not session_auth and not os.environ.get("FABRI_SERVICE_TOKEN")
            write_open = read_open and not os.environ.get("FABRI_ADMIN_TOKEN")
            if (write and write_open) or (not write and read_open):
                return "anonymous"
            raise ApiError(401, "auth required")
        if write and not principal.is_admin:
            raise ApiError(403, "admin role required")
        return principal.actor

    # ---- helpers

    @staticmethod
    def _need(source: Mapping[str, Any], key: str) -> Any:
        if key not in source or source[key] in (None, ""):
            raise ApiError(400, f"missing required field {key!r}")
        return source[key]

    def _kind(self, value: Any) -> str:
        if value not in API_KINDS:
            raise ApiError(400, f"kind must be one of {', '.join(API_KINDS)}")
        return value

    @staticmethod
    def _item_name(kind: str, value: Any) -> str:
        if not isinstance(value, str) or not value:
            raise ApiError(400, "missing required field 'name'")
        if kind == "setup":
            if value != "setup.yaml":
                raise ApiError(400, "setup items are named setup.yaml")
            return value
        if kind == "tools":
            if value != "tools.yaml":
                raise ApiError(400, "tools items are named tools.yaml; register single tools with /tools/register")
            return value
        if kind == "data":
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.[a-z0-9]{1,8}", value):
                raise ApiError(400, "data items are named <slug>.<ext>")
            return value
        check_slug(value)
        return value

    @staticmethod
    def _item_label(kind: str, name: str) -> str:
        return "tools/tools.yaml" if kind == "tools" else f"{kind}/{name}"

    @staticmethod
    def _item_files(kind: str, name: str) -> list[str]:
        return {"agents": ["agent.yaml", "prompt.md", "prompt.append.md"],
                "skills": ["skill.yaml", "prompt.md"], "plays": ["play.yaml"],
                "context": [f"{name}.md"], "data": [name], "tools": ["tools.yaml"],
                "setup": ["setup.yaml"]}[kind]

    def _tool_names(self, setups: Setups) -> list[str]:
        tools = setups.tools()
        return [n for t in tools.tools.values() for n in [t.name, *t.aliases]]

    def _lint(self, setups: Setups) -> list[dict[str, str]]:
        from fabri.setups import lint_setup

        resolved = setups.resolve()
        tools = setups.tools()
        findings = lint_setup(resolved, tool_names=[n for t in tools.tools.values() for n in [t.name, *t.aliases]],
                              policy=self.policy, tool_set=tools)
        out = []
        for f in findings:
            kind, _, name = f.item.partition("/")
            if f.item == "setup.yaml":
                kind, name = "setup", "setup.yaml"
            elif f.item == "tools/tools.yaml":
                kind, name = "tools", "tools.yaml"
            out.append({"severity": f.level, "kind": kind, "name": name, "message": f.message, "layer": f.layer})
        for error in tools.errors:
            out.append({"severity": "error", "kind": "tools", "name": "", "message": str(error), "layer": ""})
        return out

    @staticmethod
    def _stat(paths: list[Path]) -> tuple[int, str]:
        size, newest = 0, 0.0
        for path in paths:
            if path.is_file():
                st = path.stat()
                size += st.st_size
                newest = max(newest, st.st_mtime)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(newest)) if newest else ""
        return size, stamp

    # ---------------------------------------------------------------- handlers

    def get_scopes(self, req: "Request"):
        packs = self._names_under("pack")
        orgs = []
        for org in self._names_under("org"):
            orgs.append({"name": org, "packs": self.org_packs(org),
                         "projects": self._names_under("project", org=org)})
        baseline_exists = "baseline" in self.templates and self.layer_path(Scope("baseline")).is_dir()
        return 200, {"baseline": {"scope": "baseline", "exists": baseline_exists}, "packs": packs, "orgs": orgs}

    def post_scopes(self, req: "Request"):
        kind = req.body.get("kind")
        if kind not in ("pack", "org", "project"):
            raise ApiError(400, "kind must be pack, org or project")
        name = self._need(req.body, "name")
        try:
            check_slug(name, "scope name")
        except SetupError as exc:
            raise ApiError(400, str(exc)) from None
        org = req.body.get("org")
        if kind == "project":
            org = self._need(req.body, "org")
            try:
                check_slug(org, "org")
            except SetupError as exc:
                raise ApiError(400, str(exc)) from None
            scope = Scope("project", org=org, project=name)
            self._existing(Scope("org", org=org))
        elif kind == "org":
            scope = Scope("org", org=name)
        else:
            scope = Scope("pack", pack=name)
        path = self.layer_path(scope)
        with self._write_lock:
            if path.exists():
                raise ApiError(409, f"scope {scope.text} already exists")
            path.mkdir(parents=True)
            self.audit(path, req.actor, "create-scope", scope, "scope", name, {"kind": kind, "name": name, "org": org})
        out: dict[str, Any] = {"scope": scope.text, "kind": kind, "name": name}
        if kind == "project":
            out["org"] = org
        return 201, out

    def get_items(self, req: "Request"):
        scope = parse_scope(req.query.get("scope"))
        kind = self._kind(req.query.get("kind"))
        scope_dir = self._existing(scope)
        layers = {layer.name: layer.path for layer in self.chain(scope)}
        resolved = resolve_setup(list(layers.items()))
        items: list[dict[str, Any]] = []

        def add(name: str, source: str, files: list[Path], enabled: bool = True) -> None:
            size, stamp = self._stat(files)
            items.append({"name": name, "kind": kind, "source_layer": source, "size": size,
                          "updated_at": stamp, "enabled": enabled})

        if kind == "agents":
            for name, agent in resolved.agents.items():
                base = layers[agent["source"]] / "agents" / name
                add(name, agent["source"], [base / f for f in self._item_files(kind, name)],
                    agent.get("enabled", True))
        elif kind in ("skills", "plays"):
            for name, item in (resolved.skills if kind == "skills" else resolved.plays).items():
                base = Path(item["path"])
                add(name, item["source"], [base / f for f in self._item_files(kind, name)])
        elif kind == "context":
            for name, item in resolved.context.items():
                add(name, item["source"], [Path(item["path"])])
        elif kind == "data":
            for name, item in resolved.data.items():
                add(item["file"], item["source"], [Path(item["path"])])
        elif kind == "setup":
            if (scope_dir / "setup.yaml").is_file():
                add("setup.yaml", scope.text, [scope_dir / "setup.yaml"])
        else:
            tools_yaml = scope_dir / "tools" / "tools.yaml"
            if tools_yaml.is_file():
                add("tools.yaml", scope.text, [tools_yaml])
        if kind not in ("tools", "setup"):
            listed = {i["name"] for i in items}
            folder = scope_dir / kind
            for marker in sorted(folder.glob("*.disabled")) if folder.is_dir() else []:
                stem = marker.name[: -len(".disabled")]
                if stem not in listed and not any(i["name"].partition(".")[0] == stem for i in items):
                    add(stem, scope.text, [marker], False)
        items.sort(key=lambda i: i["name"])
        return 200, items

    def _read_files(self, scope_dir: Path, kind: str, name: str) -> dict[str, str] | None:
        content = read_item(scope_dir, kind, name)
        if content is None:
            return None
        if isinstance(content, dict):
            return content
        if isinstance(content, bytes):
            try:
                return {name: content.decode("utf-8")}
            except UnicodeDecodeError:
                raise ApiError(415, f"data/{name} is binary and cannot be shown as text") from None
        return {self._item_files(kind, name)[0]: content}

    def get_item(self, req: "Request"):
        scope = parse_scope(req.query.get("scope"))
        kind = self._kind(req.query.get("kind"))
        name = self._item_name(kind, req.query.get("name"))
        scope_dir = self._existing(scope)
        files = self._read_files(scope_dir, kind, name)
        if files is None:
            raise ApiError(404, f"no {kind} item {name!r} in {scope.text}")
        redacted = False
        if kind == "tools":
            files["tools.yaml"], redacted = _redact_tools_yaml(files["tools.yaml"])
        meta = self._meta(kind, name, files)
        if redacted:
            meta["redacted"] = True
        label = self._item_label(kind, name)
        lint = [f for f in self._lint(self.setups(scope)) if f["layer"] == scope.text
                and (f"{f['kind']}/{f['name']}" == label or (kind == "tools" and f["kind"] == "tools"))]
        body = {"name": name, "kind": kind, "files": files, "meta": meta, "lint": lint}
        if kind == "skills":
            body["revision"] = item_revision(scope_dir, kind, name)
        return 200, body

    def get_item_history(self, req: "Request"):
        scope = parse_scope(req.query.get("scope"))
        kind = self._kind(req.query.get("kind"))
        if kind != "skills":
            raise ApiError(400, "history is recorded for skills only")
        name = self._item_name(kind, req.query.get("name"))
        scope_dir = self._existing(scope)
        return 200, {"name": name, "kind": kind, "revision": item_revision(scope_dir, kind, name),
                     "history": item_history(scope_dir, kind, name)}

    @staticmethod
    def _meta(kind: str, name: str, files: Mapping[str, str]) -> dict[str, Any]:
        try:
            if kind in ("agents", "skills", "plays"):
                main = {"agents": "agent.yaml", "skills": "skill.yaml", "plays": "play.yaml"}[kind]
                data = yaml.safe_load(files.get(main, "")) or {}
                return data if isinstance(data, dict) else {}
            if kind == "context":
                meta, _ = split_frontmatter(files.get(f"{name}.md", ""))
                return meta
            if kind == "setup":
                data = yaml.safe_load(files.get("setup.yaml", "")) or {}
                return data if isinstance(data, dict) else {}
            if kind == "tools":
                data = yaml.safe_load(files.get("tools.yaml", "")) or {}
                sources = data.get("sources") if isinstance(data, dict) else None
                return {"sources": [s.get("name") for s in sources if isinstance(s, dict)]
                        if isinstance(sources, list) else [],
                        "allow": data.get("allow") or [] if isinstance(data, dict) else []}
        except (yaml.YAMLError, SetupError):
            return {}
        return {"bytes": len(files.get(name, "").encode("utf-8"))}

    def put_item(self, req: "Request"):
        scope = parse_scope(req.query.get("scope"))
        kind = self._kind(req.query.get("kind"))
        name = self._item_name(kind, req.query.get("name"))
        scope_dir = self._existing(scope)
        files = req.body.get("files")
        if not isinstance(files, dict) or not files or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
            raise ApiError(400, "files must be a non-empty mapping of file path to text")
        allowed = self._item_files(kind, name)
        bad = sorted(f for f in files if f not in allowed and not (kind == "skills" and allowed_file(kind, f)))
        if bad:
            raise ApiError(400, f"unexpected files {bad}; {kind}/{name} takes {allowed}")
        if kind in ("context", "data", "tools", "setup"):
            content: Any = files[allowed[0]] if allowed[0] in files else None
            if content is None:
                raise ApiError(400, f"files must hold {allowed[0]!r}")
        else:
            content = dict(files)
        if kind == "tools":
            try:
                parsed = yaml.safe_load(content)
            except yaml.YAMLError as exc:
                raise ApiError(400, f"tools.yaml is not valid YAML: {exc}") from None
            literals = _tools_yaml_literals(parsed)
            if literals:
                raise ApiError(400, "tools.yaml holds literal secret values; use secret_ref or env",
                               [f"{lit} is a raw value" for lit in literals])
        setups = self.setups(scope)
        with self._write_lock:
            result = setups.put(scope.text, kind, name, content, actor=req.actor)
            self.audit(scope_dir, req.actor, "put", scope, kind, name, files)
        lint = [{"severity": "warning", "kind": kind, "name": name, "message": w, "layer": scope.text}
                for w in result.warnings]
        label = self._item_label(kind, name)
        seen = {f["message"] for f in lint}
        for f in self._lint(setups):
            if f["layer"] == scope.text and f"{f['kind']}/{f['name']}" == label and f["message"] not in seen:
                lint.append(f)
        return 200, {"ok": True, "lint": lint}

    def delete_item(self, req: "Request"):
        scope = parse_scope(req.query.get("scope"))
        kind = self._kind(req.query.get("kind"))
        name = self._item_name(kind, req.query.get("name"))
        scope_dir = self._existing(scope)
        with self._write_lock:
            previous = read_item(scope_dir, kind, name)
            if not self.setups(scope).delete(scope.text, kind, name, actor=req.actor):
                raise ApiError(404, f"no {kind} item {name!r} in {scope.text}")
            self.audit(scope_dir, req.actor, "delete", scope, kind, name,
                       previous.decode("utf-8", "replace") if isinstance(previous, bytes) else previous)
        return 204, None

    def post_disable(self, req: "Request"):
        scope = parse_scope(req.body.get("scope"))
        kind = self._kind(req.body.get("kind"))
        if kind in ("tools", "setup"):
            raise ApiError(400, f"{kind} items cannot be disabled"
                                + ("; use /tools/enable for registered tools" if kind == "tools" else ""))
        name = self._item_name(kind, req.body.get("name"))
        disabled = req.body.get("disabled")
        if not isinstance(disabled, bool):
            raise ApiError(400, "disabled must be true or false")
        scope_dir = self._existing(scope)
        with self._write_lock:
            self.setups(scope).disable(scope.text, kind, name, disabled)
            self.audit(scope_dir, req.actor, "disable" if disabled else "enable", scope, kind, name,
                       {"disabled": disabled})
        return 200, {"ok": True}

    def get_resolved(self, req: "Request"):
        org, project = req.query.get("org") or None, req.query.get("project") or None
        agent_type = req.query.get("type") or None
        scope = self._view_scope(org, project)
        if scope.kind != "baseline":
            self._existing(scope)
        self._check_type_enabled(org, agent_type)
        setups = self.setups(scope)
        resolved = setups.resolve()
        try:
            view = resolved.to_dict(agent_type)
        except SetupError as exc:
            raise ApiError(404, str(exc)) from None
        view["layers"] = [{"name": layer.name} for layer in resolved.layers]
        tools = setups.tools()
        chosen = tools.for_type(resolved, agent_type) if agent_type else list(tools.tools.values())
        view["resolved_tools"] = [{**t.row(), "title": t.manifest.title} for t in sorted(chosen, key=lambda t: t.name)]
        view["tool_errors"] = [str(e) for e in tools.errors]
        view["scope"] = scope.text
        if agent_type:
            view["skill_order"] = self._skill_order(resolved, agent_type)
        return 200, view

    def get_ui(self, req: "Request"):
        return 200, {"ui": self.ui}

    def get_preview(self, req: "Request"):
        """What one agent type would get for a task: which skills sit in the
        prompt, which on-demand skills get a full index line, which tools the
        model is shown, with scores, and how much of each budget is used.

        Skills go through the same split as ResolvedSetup.system_prompt and
        tools through the same registry and retrieve_tools call as a run built
        with build_agent_config. Tool retrieval is off by default, and then
        every tool is shown; `tool_top_k` previews it turned on."""
        from fabri.memory.embeddings import embeddings_available
        from fabri.orchestrator.retrieval import _tool_text, retrieve_tools
        from fabri.ranking import rank
        from fabri.setups.agent import build_agent_config

        org, project = req.query.get("org") or None, req.query.get("project") or None
        agent_type = req.query.get("type") or None
        task = (req.query.get("task") or "").strip()
        if not agent_type or not task:
            raise ApiError(400, "type and task are required")
        scope = self._view_scope(org, project)
        if scope.kind != "baseline":
            self._existing(scope)
        self._check_type_enabled(org, agent_type)
        setups = self.setups(scope)
        resolved = setups.resolve()
        try:
            agent = resolved.agent(agent_type)
        except SetupError as exc:
            raise ApiError(404, str(exc)) from None

        def int_param(key: str) -> int | None:
            raw = req.query.get(key)
            if raw in (None, ""):
                return None
            try:
                value = int(raw)
            except ValueError:
                raise ApiError(400, f"{key} must be an integer") from None
            if value < 1:
                raise ApiError(400, f"{key} must be at least 1")
            return value

        skill_top_k = int_param("skill_top_k")
        tool_top_k = int_param("tool_top_k")

        # Skills: the same modes and split the prompt uses.
        modes = resolved.skill_modes(agent_type)
        always = [{"name": n, "chars": len((resolved.skills[n].get("prompt") or "").strip()),
                   "source": resolved.skills[n].get("source")}
                  for n, (mode, _) in modes.items() if mode == "always"]
        moved = [{"name": n, "reason": reason} for n, (mode, reason) in modes.items() if reason]
        on_demand = [n for n, (mode, _) in modes.items() if mode == "on_demand"]
        full, rest = resolved.split_skill_index(on_demand, task, skill_top_k)
        scores = dict(resolved.rank_skills_scored(task, on_demand)[0]) if on_demand else {}
        on_demand_rows = [{"name": n, "score": scores.get(n, 0.0), "full": n in full,
                           "source": resolved.skills[n].get("source")} for n in [*full, *rest]]

        # Tools: the registry a run gets, then the run's own selection.
        tools = setups.tools()
        try:
            config, registry = build_agent_config(resolved, agent_type, tool_set=tools, task=task)
        except SetupError as exc:
            raise ApiError(400, str(exc)) from None
        retrieval = dict(config["tools"].get("retrieval") or {})
        if tool_top_k is not None:
            retrieval.update(enabled=True, top_k=tool_top_k)
        manifests = registry.list()
        always_include = set(retrieval.get("always_include") or [])
        if retrieval.get("enabled"):
            shown = {m.name for m in retrieve_tools(task, registry, top_k=int(retrieval.get("top_k") or 6),
                                                    always_include=always_include)}
        else:
            shown = {m.name for m in manifests}
        tool_scores, method = rank(task, {m.name: _tool_text(m) for m in manifests}) if manifests else ([], None)
        by_name = {m.name: m for m in manifests}
        rows_by_layer = {t.name: t.row() for t in tools.tools.values()}  # meta tools have none
        tool_rows = [{"name": n, "title": by_name[n].title, "score": score, "shown": n in shown,
                      "pinned": n in always_include, "approval": by_name[n].approval,
                      "source": (rows_by_layer.get(n) or {}).get("layer")}
                     for n, score in tool_scores]

        prompt, warnings = resolved.system_prompt(agent_type, task=task, skill_top_k=skill_top_k)
        context_chars = sum(len(f"### {n}\n{c['text'].strip()}") for n, c in resolved.context_for(agent_type).items())
        budgets = resolved.budgets
        return 200, {
            "scope": scope.text, "type": agent_type, "task": task,
            "ranking": "embeddings" if embeddings_available() else "words",
            "skill_top_k": skill_top_k if skill_top_k is not None else resolved.setup.get("skill_index_top_k"),
            "tool_retrieval": {"enabled": bool(retrieval.get("enabled")), "top_k": retrieval.get("top_k"),
                               "always_include": sorted(always_include)},
            "skills": {"always": always, "on_demand": on_demand_rows, "moved": moved},
            "tools": tool_rows,
            "prompt": {"chars": len(prompt), "agent_chars": len((agent.get("prompt") or "").strip()),
                       "skills_chars": sum(r["chars"] for r in always), "context_chars": context_chars,
                       "budgets": {k: budgets[k] for k in ("prompt_chars", "skills_total_chars", "context_chars")}},
            "warnings": warnings,
        }

    def _view_scope(self, org: str | None, project: str | None) -> Scope:
        if project and not org:
            raise ApiError(400, "project needs org")
        try:
            if org:
                check_slug(org, "org")
            if project:
                check_slug(project, "project")
        except SetupError as exc:
            raise ApiError(400, str(exc)) from None
        if project:
            return Scope("project", org=org, project=project)
        return Scope("org", org=org) if org else Scope("baseline")

    def post_import(self, req: "Request"):
        scope = parse_scope(req.body.get("scope"))
        fmt = req.body.get("format")
        if fmt not in ("skill-md", "fabri"):
            raise ApiError(400, "format must be skill-md or fabri")
        files = req.body.get("files")
        if not isinstance(files, dict) or not files or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
            raise ApiError(400, "files must be a non-empty mapping of path to text")
        dry_run = req.body.get("dry_run", False)
        if not isinstance(dry_run, bool):
            raise ApiError(400, "dry_run must be true or false")
        scope_dir = self._existing(scope)
        setups = self.setups(scope)
        with tempfile.TemporaryDirectory(prefix="fabri-import-") as tmp:
            source = Path(tmp)
            for rel, text in files.items():
                parts = self._safe_relpath(rel)
                if len(parts) == 1:
                    parts = ("imported-skill", *parts)
                target = source.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            with self._write_lock:
                report = setups.import_skills(source, scope.text, fmt=fmt, dry_run=dry_run)
                if not dry_run:
                    for action, names in (("import-create", report.created), ("import-update", report.updated)):
                        for item in names:
                            self.audit(scope_dir, req.actor, action, scope, "skills", item,
                                       read_item(scope_dir, "skills", item))
        return 200, report.to_dict()

    @staticmethod
    def _safe_relpath(rel: str) -> tuple[str, ...]:
        pure = PurePosixPath(rel)
        if pure.is_absolute() or "\\" in rel or not rel or any(
                part in ("", ".", "..") or part.startswith(".") or "\0" in part for part in pure.parts):
            raise ApiError(400, f"unsafe file path {rel!r}")
        return pure.parts

    def get_lint(self, req: "Request"):
        scope = parse_scope(req.query.get("scope"))
        self._existing(scope)
        return 200, self._lint(self.setups(scope))

    # ---- agent types and companies

    def _org_agents(self, org: str) -> list[str] | None:
        """The `agents:` list in the org's setup.yaml, or None when it has none
        (then every type is enabled)."""
        org_dir = self.layer_path(Scope("org", org=org))
        agents = read_yaml(org_dir / "setup.yaml").get("agents") if org_dir.is_dir() else None
        return [a for a in agents if isinstance(a, str)] if isinstance(agents, list) else None

    def _org_allows(self, org: str, agent_type: str) -> bool:
        listed = self._org_agents(org)
        return listed is None or agent_type in listed

    def _check_type_enabled(self, org: str | None, agent_type: str | None) -> None:
        if org and agent_type and not self._org_allows(org, agent_type):
            raise ApiError(404, f"agent type {agent_type!r} is not enabled for org {org!r}")

    @staticmethod
    def _skill_order(resolved: Any, agent_type: str) -> list[dict[str, str]]:
        """Skills an agent of this type loads, in the order the API defines:
        the type's global skills, then project skills, then org and pack skills."""
        names = resolved.skills_for(agent_type)
        rank = {}
        for name in names:
            skill = resolved.skills[name]
            source = skill["source"]
            if source.startswith("org:") and "/project:" in source:
                group = 1
            elif source == "baseline" and agent_type in (skill.get("types") or []):
                group = 0
            elif source.startswith(("org:", "pack:")):
                group = 2
            else:
                group = 3
            rank[name] = group
        ordered = sorted(names, key=lambda n: (rank[n], names.index(n)))
        label = {0: "global", 1: "project", 2: "org-or-pack", 3: "baseline"}
        return [{"name": n, "source_layer": resolved.skills[n]["source"], "group": label[rank[n]]} for n in ordered]

    def _baseline(self) -> tuple[Scope, Path, Setups]:
        scope = Scope("baseline")
        return scope, self._existing(scope), self.setups(scope)

    def get_agent_types(self, req: "Request"):
        _, _, setups = self._baseline()
        resolved = setups.resolve()
        orgs = self._names_under("org")
        out = []
        for agent_type, agent in sorted(resolved.agents.items()):
            config = agent["config"]
            row: dict[str, Any] = {
                "type": agent_type, "title": str(config.get("title") or agent_type),
                "global_skills": sorted(n for n, s in resolved.skills.items() if agent_type in (s.get("types") or [])),
                "used_by": [o for o in orgs if self._org_allows(o, agent_type)],
            }
            route = config.get("route")
            if isinstance(route, Mapping):
                row["route"] = dict(route)
            out.append(row)
        return 200, out

    def _known_type(self, resolved: Any, agent_type: str) -> dict[str, Any]:
        try:
            check_slug(agent_type, "agent type")
        except SetupError as exc:
            raise ApiError(400, str(exc)) from None
        agent = resolved.agents.get(agent_type)
        if agent is None:
            raise ApiError(404, f"unknown agent type {agent_type!r}")
        return agent

    def get_agent_type(self, req: "Request"):
        _, _, setups = self._baseline()
        resolved = setups.resolve()
        agent_type = req.params["type"]
        agent = self._known_type(resolved, agent_type)
        return 200, {
            "type": agent_type, "title": str(agent["config"].get("title") or agent_type),
            "prompt": agent.get("prompt", ""),
            "global_skills": sorted(n for n, s in resolved.skills.items() if agent_type in (s.get("types") or [])),
        }

    def put_agent_type(self, req: "Request"):
        scope, scope_dir, setups = self._baseline()
        resolved = setups.resolve()
        agent_type = req.params["type"]
        self._known_type(resolved, agent_type)
        prompt, wanted = req.body.get("prompt"), req.body.get("global_skills")
        if prompt is None and wanted is None:
            raise ApiError(400, "give prompt, global_skills, or both")
        if prompt is not None and not isinstance(prompt, str):
            raise ApiError(400, "prompt must be text")
        writes: list[tuple[str, str, Any]] = []
        if prompt is not None:
            writes.append(("agents", f"{agent_type}/prompt.md", prompt))
        if wanted is not None:
            if not isinstance(wanted, list) or not all(isinstance(n, str) for n in wanted):
                raise ApiError(400, "global_skills must be a list of skill names")
            local = {n: read_item(scope_dir, "skills", n) for n in wanted if SLUG.fullmatch(n)}
            missing = [n for n in wanted if not isinstance(local.get(n), dict)]
            if missing:
                raise ApiError(400, "global skills must exist in the baseline layer",
                               [f"no baseline skill {n!r}" for n in missing])
            current = {n for n, s in resolved.skills.items() if agent_type in (s.get("types") or [])
                       and s["source"] == "baseline"}
            for name in sorted(current | set(wanted)):
                files = read_item(scope_dir, "skills", name)
                if not isinstance(files, dict) or "skill.yaml" not in files:
                    continue
                data = yaml.safe_load(files["skill.yaml"]) or {}
                types = [t for t in (data.get("types") or []) if isinstance(t, str)]
                if name in wanted and agent_type not in types:
                    types.append(agent_type)
                elif name not in wanted and agent_type in types:
                    types.remove(agent_type)
                    if not types:
                        raise ApiError(400, "removing the type would make the skill load for every agent type",
                                       [f"skill {name!r} would get types: []; delete or disable it instead"])
                else:
                    continue
                data["types"] = types
                writes.append(("skills", name, {**files, "skill.yaml": yaml.safe_dump(data, sort_keys=False,
                                                                                         allow_unicode=True)}))
        for kind, name, content in writes:
            validate_item(scope_dir, kind, name, content, policy=self.policy)
        with self._write_lock:
            for kind, name, content in writes:
                setups.put(scope.text, kind, name, content)
                self.audit(scope_dir, req.actor, "put", scope, kind, name, content)
        return self.get_agent_type(req)

    def get_org_agent_types(self, req: "Request"):
        org = req.params["org"]
        try:
            check_slug(org, "org")
        except SetupError as exc:
            raise ApiError(400, str(exc)) from None
        scope = Scope("org", org=org)
        self._existing(scope)
        available = sorted(self.setups(scope).resolve().agents)
        listed = self._org_agents(org)
        return 200, {"agents": available if listed is None else listed, "available": available,
                     "explicit": listed is not None}

    def put_org_agent_types(self, req: "Request"):
        org = req.params["org"]
        try:
            check_slug(org, "org")
        except SetupError as exc:
            raise ApiError(400, str(exc)) from None
        scope = Scope("org", org=org)
        scope_dir = self._existing(scope)
        agents = req.body.get("agents")
        if not isinstance(agents, list) or not all(isinstance(a, str) for a in agents):
            raise ApiError(400, "agents must be a list of agent type names")
        setups = self.setups(scope)
        available = sorted(setups.resolve().agents)
        unknown = sorted(set(agents) - set(available))
        if unknown:
            raise ApiError(400, "unknown agent types", [f"no agent type {a!r}" for a in unknown])
        agents = list(dict.fromkeys(agents))
        with self._write_lock:
            data = read_yaml(scope_dir / "setup.yaml")
            data["agents"] = agents
            flags = dict(data.get("agent_types") or {})
            for agent_type in available:
                if agent_type in agents:
                    if flags.get(agent_type) is False:
                        del flags[agent_type]
                else:
                    flags[agent_type] = False
            if flags:
                data["agent_types"] = flags
            else:
                data.pop("agent_types", None)
            setups.put(scope.text, "setup", "setup.yaml", yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
            self.audit(scope_dir, req.actor, "put", scope, "setup", "setup.yaml", {"agents": agents})
        return 200, {"ok": True, "agents": agents}

    # ---- tools

    def get_catalog(self, req: "Request"):
        scope = self._view_scope(req.query.get("org") or None, req.query.get("project") or None)
        if scope.kind != "baseline":
            self._existing(scope)
        agent_type = req.query.get("type") or None
        self._check_type_enabled(req.query.get("org") or None, agent_type)
        setups = self.setups(scope)
        resolved = setups.resolve()
        tools = setups.tools()
        catalog = tools.catalog
        if agent_type:
            try:
                resolved.agent(agent_type)
            except SetupError as exc:
                raise ApiError(404, str(exc)) from None
            names = {t.name for t in tools.for_type(resolved, agent_type)}
        else:
            names = set(tools.tools)
        out = []
        for name in sorted(names):
            entry = catalog.entries.get(name) if catalog is not None else None
            if entry is None:
                continue
            row = entry.to_dict()
            out.append({
                "name": entry.name, "alias": row["alias"], "title": entry.title,
                "description": entry.description, "approval": entry.approval, "source": entry.source,
                "endpoint": {"method": entry.method, "path": entry.path, "kind": entry.kind},
                "params": row["params"], "options": row["options"], "layer": entry.layer,
                "origin": entry.origin, "examples": row["examples"], "probed_at": entry.probed_at,
            })
        return 200, out

    def _registered_json(self, tool: RegisteredTool) -> dict[str, Any]:
        return {"name": tool.name, "version": tool.version, "kind": tool.kind, "approval": tool.approval,
                "enabled": tool.enabled, "content_hash": tool.content_hash, "registered_by": tool.registered_by,
                "registered_at": tool.registered_at, "changed": tool.changed, "decl": tool.decl}

    def _tool_scope(self, req: "Request") -> tuple[Scope, Path]:
        scope = parse_scope(req.body.get("scope"))
        return scope, self._existing(scope)

    @staticmethod
    def _tool_name(value: Any) -> str:
        if not isinstance(value, str) or not TOOL_NAME.fullmatch(value) or value.startswith("_") \
                or "/" in value or ".." in value:
            raise ApiError(400, "name must be a valid tool name")
        return value

    def post_register(self, req: "Request"):
        scope, scope_dir = self._tool_scope(req)
        decl = req.body.get("decl")
        if not isinstance(decl, dict):
            raise ApiError(400, "decl must be a tool declaration object")
        if decl.get("name") is not None:
            self._tool_name(decl.get("name"))
        try:
            with self._write_lock:
                tool = register_tool(scope_dir, decl, actor=req.actor, policy=self.policy,
                                     transport=self.transport)
                self.audit(scope_dir, req.actor, "register-tool", scope, "tools", tool.name,
                           {"version": tool.version, "content_hash": tool.content_hash})
        except (RegistrationError, PolicyError, ApprovalError, ToolSourceError, ValueError) as exc:
            raise ApiError(400, f"registration refused: {exc}", [str(exc)]) from None
        return 200, self._registered_json(tool)

    def post_unregister(self, req: "Request"):
        scope, scope_dir = self._tool_scope(req)
        name = self._tool_name(req.body.get("name"))
        with self._write_lock:
            if not unregister_tool(scope_dir, name, actor=req.actor):
                raise ApiError(404, f"no registered tool named {name!r} in {scope.text}")
            self.audit(scope_dir, req.actor, "unregister-tool", scope, "tools", name, {"name": name})
        return 200, {"ok": True}

    def post_enable(self, req: "Request"):
        scope, scope_dir = self._tool_scope(req)
        name = self._tool_name(req.body.get("name"))
        enabled = req.body.get("enabled")
        if not isinstance(enabled, bool):
            raise ApiError(400, "enabled must be true or false")
        if name not in {t.name for t in list_registered(scope_dir)}:
            raise ApiError(404, f"no registered tool named {name!r} in {scope.text}")
        with self._write_lock:
            set_enabled(scope_dir, name, enabled, actor=req.actor)
            self.audit(scope_dir, req.actor, "enable-tool" if enabled else "disable-tool", scope, "tools", name,
                       {"enabled": enabled})
        return 200, {"ok": True}

    def post_sync(self, req: "Request"):
        scope, scope_dir = self._tool_scope(req)
        setups = self.setups(scope)
        resolved = setups.resolve()
        tools = setups.tools()
        state_dir = default_state_dir(resolved)
        assert state_dir is not None
        with self._write_lock:
            previous = read_state(state_dir, "sync.json").get("tools") or {}
            current = tools.snapshot()
            diff = diff_snapshots(previous, current)
            write_state(state_dir, "sync.json", {"version": 1, "content_hash": resolved.content_hash, "tools": current})
            if tools.catalog is not None:
                save_catalog(tools.catalog, state_dir)
            self.audit(scope_dir, req.actor, "sync-tools", scope, "tools", "",
                       {"content_hash": resolved.content_hash, **diff})
        return 200, {"added": diff["added"], "removed": diff["removed"], "changed": diff["changed"]}

    def post_probe(self, req: "Request"):
        scope, scope_dir = self._tool_scope(req)
        ops = req.body.get("ops")
        if ops is not None and not (isinstance(ops, list) and all(isinstance(o, str) for o in ops)):
            raise ApiError(400, "ops must be a list of tool names")
        chain = self.chain(scope)
        with self._write_lock:
            report = probe_tools(chain, policy=self.policy, ops=ops, transport=self.transport, store=self.store)
            self.audit(scope_dir, req.actor, "probe-tools", scope, "tools", "",
                       {"ops": ops, "probed": report["probed"]})
        return 200, {"probed": report["probed"], "skipped": report["skipped"]}

    # ---- tool sources (tools/sources/<name>.yaml)

    def get_tool_sources(self, req: "Request"):
        scope = parse_scope(req.query.get("scope"))
        scope_dir = self._existing(scope)
        setups = self.setups(scope)
        tools = setups.tools()
        summary = stack_summary(tools, layer_name=scope.text)
        entries = list_entries(scope_dir)
        for entry in entries:
            rows = [summary[s] for s in entry["sources"] if s in summary]
            entry["tools"] = sum(r["tools"] for r in rows)
            entry["approvals"] = {c: sum(r["approvals"][c] for r in rows) for c in ("read", "write", "always_approve")}
        return 200, {"scope": scope.text, "entries": entries, "errors": [str(e) for e in tools.errors]}

    @staticmethod
    def _opt_list(body: Mapping[str, Any], key: str) -> list[str] | None:
        value = body.get(key)
        if value is None:
            return None
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ApiError(400, f"{key} must be a list of strings")
        return value

    @staticmethod
    def _opt_str(body: Mapping[str, Any], key: str) -> str | None:
        value = body.get(key)
        if value is not None and not isinstance(value, str):
            raise ApiError(400, f"{key} must be a string")
        return value or None

    @staticmethod
    def _opt_bool(body: Mapping[str, Any], key: str, default: bool) -> bool:
        value = body.get(key, default)
        if not isinstance(value, bool):
            raise ApiError(400, f"{key} must be true or false")
        return value

    def _body_headers(self, body: Mapping[str, Any]) -> dict[str, Any]:
        auth = body.get("auth") or {}
        headers = body.get("headers") or {}
        if not isinstance(auth, Mapping) or not isinstance(headers, Mapping) \
                or not all(isinstance(v, str) for v in headers.values()):
            raise ApiError(400, "auth must be {env | secret_ref, header?, prefix?}; headers must map names to env vars")
        prefix = auth.get("prefix", "Bearer ")
        return auth_headers(auth_env=auth.get("env"), secret_ref=auth.get("secret_ref"),
                            auth_header=auth.get("header"), auth_prefix=prefix, header_env=dict(headers))

    def _entry_from_body(self, body: Mapping[str, Any]):
        source = body.get("source")
        approval = self._opt_str(body, "approval")
        if source == "openapi":
            spec_url, spec_text = self._opt_str(body, "spec_url"), body.get("spec_text")
            if (spec_url is None) == (spec_text is None):
                raise ApiError(400, "give exactly one of spec_url or spec_text")
            if spec_text is not None and not isinstance(spec_text, str):
                raise ApiError(400, "spec_text must be the spec as text")
            if spec_url is not None and not spec_url.startswith(("http://", "https://")):
                raise ApiError(400, "spec_url must be an http or https URL; upload a file as spec_text")
            if spec_url is not None:
                self.policy.check_host(spec_url, "spec_url")
            return openapi_entry(
                spec_url, spec_text=spec_text, name=self._opt_str(body, "name"),
                include=self._opt_list(body, "include"), exclude=self._opt_list(body, "exclude"),
                methods=self._opt_list(body, "methods"), allow_delete=self._opt_bool(body, "allow_delete", False),
                approval=approval, base_url=self._opt_str(body, "base_url"), headers=self._body_headers(body),
                snapshot=self._opt_bool(body, "snapshot", False),
                strip_prefixes=self._opt_list(body, "strip_prefixes"), transport=self.transport)
        if source == "mcp":
            command = body.get("command")
            if command is not None and not (isinstance(command, str) or (
                    isinstance(command, list) and all(isinstance(c, str) for c in command))):
                raise ApiError(400, "command must be a string or a list of strings")
            env = body.get("env")
            if env is not None and not isinstance(env, (list, Mapping)):
                raise ApiError(400, "env must be a list of NAME or NAME=VAR, or a mapping")
            name = self._opt_str(body, "name")
            if not name:
                raise ApiError(400, "missing required field 'name'")
            return mcp_entry(name, command=command, url=self._opt_str(body, "url"), env=env,
                             headers=self._body_headers(body), include=self._opt_list(body, "include"),
                             approval=approval, connect=self._opt_bool(body, "connect", True))
        if source == "connector":
            connector = self._opt_str(body, "connector")
            if not connector:
                raise ApiError(400, "missing required field 'connector'")
            return connector_tool_entry(
                connector, name=self._opt_str(body, "name"), token_env=self._opt_str(body, "token_env"),
                workspace=self._opt_str(body, "workspace"), remote=self._opt_bool(body, "remote", False),
                approval=approval, connect=self._opt_bool(body, "connect", False))
        raise ApiError(400, "source must be openapi, mcp or connector")

    def post_tool_source(self, req: "Request"):
        scope, scope_dir = self._tool_scope(req)
        dry_run = self._opt_bool(req.body, "dry_run", False)
        replace = self._opt_bool(req.body, "replace", False)
        entry = self._entry_from_body(req.body)
        with self._write_lock:
            report = add_entry(scope_dir, entry, chain=self.chain(scope), policy=self.policy, replace=replace,
                               dry_run=dry_run, actor=req.actor, scope=scope.text, transport=self.transport,
                               store=self.store)
        body = report.to_dict()
        body["yaml"] = entry.to_yaml()
        if report.errors and not dry_run:
            raise ApiError(409 if any("already exists" in e for e in report.errors) else 400,
                           "tool source not added", report.errors)
        return 200, body

    def post_tool_source_enable(self, req: "Request"):
        scope, scope_dir = self._tool_scope(req)
        name = self._need(req.body, "name")
        enabled = self._opt_bool(req.body, "enabled", True)
        try:
            with self._write_lock:
                changed = set_entry_enabled(scope_dir, name, enabled, actor=req.actor, scope=scope.text)
        except EntryError as exc:
            raise ApiError(404 if "no tool source" in str(exc) else 400, str(exc)) from None
        return 200, {"ok": True, "changed": changed}

    def delete_tool_source(self, req: "Request"):
        scope = parse_scope(req.query.get("scope"))
        scope_dir = self._existing(scope)
        name = req.query.get("name") or ""
        try:
            with self._write_lock:
                removed = remove_entry(scope_dir, name, actor=req.actor, scope=scope.text)
        except EntryError as exc:
            raise ApiError(404 if "no tool source" in str(exc) else 400, str(exc)) from None
        return 200, {"ok": True, "removed": removed}

    def get_connectors(self, req: "Request"):
        from fabri.tools.connectors import list_connectors

        return 200, list_connectors()


@dataclass
class Request:
    actor: str
    query: dict[str, str]
    body: dict[str, Any]
    params: dict[str, str] = field(default_factory=dict)
