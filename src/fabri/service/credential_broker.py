"""Credential broker for ``fabri serve``.

A run started by the service never holds a stored token. Its credentialed
http, openapi and http MCP sources send a request spec over a per-run Unix
socket to this broker, which lives in the service process. The broker:

1. checks the peer (same uid, a process in the run's own process tree) and
   the run's 256-bit capability key,
2. checks the request against what the run's bound ``run.yaml`` declared
   (source, tool, method, path parameters rendered into the bound template),
3. works out the approval class itself and, for ``always_approve``, asks a
   human through the run's ``AskUserListener`` and mints a one-time grant
   bound to the hash of the exact prepared request,
4. resolves the credential through ``ConnectionStore.get_token`` on this call,
   so a revoke or a refresh takes effect on the next call,
5. sends the request (no redirects), redacts the response with the resolved
   values and answers with the same dict ``http_call.send`` returns.

Protocol: one connection per request, one UTF-8 JSON line in, one JSON line
out, then close. See ``docs/connections.md`` for the operator view.

No token value is written to a log line, an audit line, an error text or a
reply. Secrets at rest stay plaintext in the 0600 install database; the
broker only reads them.
"""
from __future__ import annotations

import base64
import binascii
import ctypes
import ctypes.util
import fcntl
import fnmatch
import hmac
import json
import logging
import os
import re
import secrets
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import yaml

from fabri.tools.credential_store import (
    CredentialNotFoundError,
    EnvCredentialStore,
    ScopedCredentialStore,
)
from fabri.tools.security.ssrf import PinnedTransport

if TYPE_CHECKING:  # pragma: no cover
    from fabri.service.ask_user_listener import AskUserListener

logger = logging.getLogger("fabri.service.credential_broker")

MAX_REQUEST_BYTES = 1_048_576
MAX_RAW_BODY_BYTES = 4 * 1_048_576
MAX_INFLIGHT_PER_RUN = 8
# Requests parked on a human answer hold no send slot, so they get their own cap.
MAX_PENDING_APPROVALS_PER_RUN = 32
MAX_SEND_TIMEOUT_S = 120.0
MIN_SEND_TIMEOUT_S = 1.0
GRANT_TTL_S = 120.0
SNAPSHOT_ENV = "FABRI_CRED_SNAPSHOT"
# Must match fabri.tools.credential_broker_client. Both start with FABRI_CRED_
# so every tool subprocess loses them through env_without_credentials.
SOCKET_ENV = "FABRI_CRED_BROKER_SOCKET"
KEY_ENV = "FABRI_CRED_BROKER_KEY"
PROTOCOL_VERSION = 1
# How long a connection waits for the service to tell the broker the run's pid.
PID_ATTACH_WAIT_S = 10.0
# How long the broker waits for the request line once a client connected.
READ_TIMEOUT_S = 15.0
BAD_KEY_DELAY_S = 0.5
MAX_SOCKET_PATH_BYTES = 96

MCP_READ_METHODS = frozenset({
    "initialize", "notifications/initialized", "ping", "tools/list", "resources/list", "resources/read",
    "resources/templates/list", "prompts/list", "prompts/get", "completion/complete",
})
_PLACEHOLDER = re.compile(r"\{([^{}/]+)\}")
_LABEL = re.compile(r"[^A-Za-z0-9_.:-]")
_ACTOR = "${actor}"


class BindError(ValueError):
    """A run's config cannot be bound to the broker. The message names the
    source and the reason, never a value."""


class _Refuse(Exception):
    """Internal: stop handling a request and answer with a refusal."""

    def __init__(self, status: str, text: str, decision: str = "refused") -> None:
        super().__init__(text)
        self.status = status
        self.text = text
        self.decision = decision


# ---------------------------------------------------------------- bindings


@dataclass(frozen=True)
class ToolBinding:
    name: str
    method: str
    template_url: str
    approval: str
    # The {placeholder} names in template_url, in order. The broker renders
    # the URL itself from these, so no client URL is ever matched.
    placeholders: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceBinding:
    name: str
    kind: str                                   # "http" | "openapi" | "mcp"
    header_specs: Mapping[str, Any]             # the declaration's headers, specs not values
    refs: tuple[tuple[str, str], ...]           # (provider, handle) after ${actor} substitution
    grants: tuple[str, ...] | None              # policy.grants_for(name)
    tools: Mapping[str, ToolBinding] = field(default_factory=dict)   # http/openapi only
    url: str | None = None                      # mcp only
    approval_decl: Any = None                   # mcp only: decl.get("approval")
    catalog_owner: str | None = None            # mcp only: catalog_for_url(url)
    floors: tuple[tuple[str, str], ...] = ()    # policy.approval_floor
    catalog_marker: str | None = None           # mcp only: decl.get("catalog")
    # mcp only: the approval and catalog declarations of the same source in
    # other configs of this run. The broker applies all of them, so the
    # strictest declaration decides.
    approval_decls: tuple[Any, ...] = ()
    catalog_markers: tuple[str, ...] = ()

    def _signature(self) -> tuple:
        """What must be the same for two configs to share one binding: where
        the request goes and which credential it carries."""
        tools = tuple(sorted((t.name, t.method, t.template_url, t.placeholders) for t in self.tools.values()))
        return (self.kind, json.dumps(self.header_specs, sort_keys=True, default=str), self.refs, tools, self.url)

    def _authorization(self) -> tuple:
        """Every field that decides how much approval a call needs."""
        tools = tuple(sorted((t.name, t.approval) for t in self.tools.values()))
        return (tools, self.grants, self.floors, json.dumps(self.all_approval_decls(), sort_keys=True, default=str),
                self.all_catalog_markers(), self.catalog_owner)

    def all_approval_decls(self) -> tuple[Any, ...]:
        return (self.approval_decl, *self.approval_decls)

    def all_catalog_markers(self) -> tuple[str, ...]:
        first = (self.catalog_marker,) if self.catalog_marker else ()
        return tuple(dict.fromkeys((*first, *self.catalog_markers)))


def _merge_strictest(prior: SourceBinding, bound: SourceBinding) -> SourceBinding:
    """One binding for a source two configs of the run declare. The request
    shape and credential must match (else BindError). Where the approval
    settings differ, the result keeps the strictest of each: the stricter
    class per tool, every policy floor, every MCP approval declaration and
    catalog marker, and only the credential grant patterns both configs
    list. Every ref was already checked against each config's own grants."""
    from fabri.tools.approval import stricter

    if prior._signature() != bound._signature():
        raise BindError(f"source {bound.name}: declared twice with different templates or refs")
    if prior._authorization() == bound._authorization():
        return prior
    logger.info("credential broker: source %s is declared twice with different approval settings; "
                "using the strictest of each", _label(bound.name, 64))
    tools = {name: ToolBinding(name=tb.name, method=tb.method, template_url=tb.template_url,
                               approval=stricter(tb.approval, bound.tools[name].approval),
                               placeholders=tb.placeholders)
             for name, tb in prior.tools.items()}
    if prior.grants is None:
        grants = bound.grants
    elif bound.grants is None:
        grants = prior.grants
    else:
        grants = tuple(g for g in prior.grants if g in bound.grants)
    decls: list[Any] = []
    for decl in (*prior.all_approval_decls(), *bound.all_approval_decls()):
        if decl is not None and decl not in decls:
            decls.append(decl)
    markers = tuple(dict.fromkeys((*prior.all_catalog_markers(), *bound.all_catalog_markers())))
    return SourceBinding(
        name=prior.name, kind=prior.kind, header_specs=prior.header_specs, refs=prior.refs, grants=grants,
        tools=tools, url=prior.url, approval_decl=decls[0] if decls else None,
        catalog_owner=prior.catalog_owner, floors=tuple(dict.fromkeys((*prior.floors, *bound.floors))),
        catalog_marker=markers[0] if markers else None, approval_decls=tuple(decls[1:]),
        catalog_markers=markers[1:])


@dataclass
class BrokerBinding:
    session_id: str
    socket_path: str
    key: str = field(repr=False)
    actor: str | None
    sources: dict[str, SourceBinding]

    def env(self) -> dict[str, str]:
        return {SOCKET_ENV: self.socket_path, KEY_ENV: self.key}


class _RefusingStore:
    """Credential store used while binding: building a source never resolves a value."""

    def get(self, provider: str, handle: str) -> str:
        raise CredentialNotFoundError("credentials are not resolved while binding")


def _load_yaml(path: str | Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _policy(raw: Any, where: str):
    from fabri.tools.policy import PolicyError, ToolPolicy

    try:
        if raw is None:
            return ToolPolicy()
        if isinstance(raw, (str, Path)):
            return ToolPolicy.from_file(raw, base=ToolPolicy())
        return ToolPolicy.from_data(raw, base=ToolPolicy())
    except (PolicyError, OSError, ValueError) as exc:
        raise BindError(f"{where}: tools.policy cannot be read ({type(exc).__name__})") from None


def _substitute_actor(specs: Any, actor: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for header, spec in (specs or {}).items() if isinstance(specs, Mapping) else ():
        if isinstance(spec, Mapping) and isinstance(spec.get("secret_ref"), str) and actor:
            provider, _, handle = spec["secret_ref"].partition(":")
            if handle == _ACTOR:
                spec = {**spec, "secret_ref": f"{provider}:{actor}"}
        out[str(header)] = dict(spec) if isinstance(spec, Mapping) else spec
    return out


def _spec_refs(specs: Mapping[str, Any]) -> list[tuple[str, str]]:
    refs = []
    for spec in specs.values():
        if isinstance(spec, Mapping) and isinstance(spec.get("secret_ref"), str):
            ref = spec["secret_ref"]
            if ref.count(":") == 1:
                provider, handle = ref.split(":", 1)
                if provider and handle:
                    refs.append((provider, handle))
    return refs


def _templated_host(url: str) -> bool:
    rest = url.split("://", 1)[-1]
    return "{" in re.split(r"[/?#]", rest, maxsplit=1)[0]


def _bind_one(decl: Mapping[str, Any], policy, enabled, *, actor, base_dir, transport) -> SourceBinding:
    from fabri.tools.connectors import catalog_for_url
    from fabri.tools.policy import PolicyError
    from fabri.tools.sources import ToolSourceError, _request_url, build_source_tools, validate_source_decl

    name = str(decl.get("name"))
    try:
        validate_source_decl(decl, policy)
    except (ToolSourceError, PolicyError, ValueError) as exc:
        raise BindError(f"source {name}: {exc}") from None
    kind = decl["kind"]
    if kind not in ("http", "openapi", "mcp"):
        raise BindError(f"source {name}: a {kind} source cannot use a stored credential under fabri serve")
    if kind == "mcp" and decl.get("command"):
        raise BindError(f"source {name}: a stdio MCP server cannot use a stored credential under fabri serve; "
                        "use the http transport, or set FABRI_CRED_SNAPSHOT=1 on the service")
    if any(isinstance(s, Mapping) and "secret_ref" in s for s in (decl.get("env") or {}).values()):
        raise BindError(f"source {name}: a secret_ref in env cannot be brokered; put it in headers")
    specs = _substitute_actor(decl.get("headers"), actor)
    refs = tuple(dict.fromkeys((p, h) for p, h in _spec_refs(specs) if h != _ACTOR))
    grants = policy.grants_for(name)
    if kind == "mcp":
        url = str(decl["url"])
        if _templated_host(url):
            raise BindError(f"source {name}: the server URL host is templated")
        approval = decl.get("approval")
        return SourceBinding(name=name, kind="mcp", header_specs=specs, refs=refs, grants=grants, tools={},
                             url=url, approval_decl=dict(approval) if isinstance(approval, Mapping) else approval,
                             catalog_owner=catalog_for_url(url), floors=tuple(policy.approval_floor),
                             catalog_marker=decl.get("catalog"))
    from fabri.tools.approval import stricter

    try:
        built = build_source_tools(decl, base_dir=base_dir, policy=policy, transport=transport,
                                   store=_RefusingStore())
    except BindError:
        raise
    except Exception as exc:  # noqa: BLE001 -- reported as a bind failure, without values
        raise BindError(f"source {name}: cannot be built ({type(exc).__name__}: {exc})") from None
    tools: dict[str, ToolBinding] = {}
    for tool in built:
        template = _request_url(tool)
        if template is None:
            continue
        if _templated_host(template):
            raise BindError(f"source {name}: tool {tool.manifest.name} has a templated host")
        aliases = tool.manifest.aliases or ()
        if not policy.name_allowed(tool.manifest.name, aliases):
            continue
        if enabled is not None and tool.manifest.name not in enabled:
            continue
        approval = tool.manifest.approval or "write"
        floor = policy.floor_for(tool.manifest.name, aliases)
        if floor is not None:
            approval = stricter(approval, floor)
        tools[tool.manifest.name] = ToolBinding(
            name=tool.manifest.name, method=str(tool.method or "GET").upper(), template_url=template,
            approval=approval, placeholders=tuple(dict.fromkeys(_PLACEHOLDER.findall(template))))
    return SourceBinding(name=name, kind=kind, header_specs=specs, refs=refs, grants=grants, tools=tools,
                         floors=tuple(policy.approval_floor))


def bind_sources(
    config: Mapping,
    *,
    actor: str | None,
    base_dir: str | Path | None = None,
    max_depth: int = 3,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, SourceBinding]:
    """Every credentialed source the run's bound config (and its static
    ``tools.agents[*].config`` files, to `max_depth`) declares.

    Only declarations with a ``secret_ref`` in ``headers`` are kept. Each one
    is validated against its own config's ``tools.policy`` and built with a
    store that refuses every lookup, so binding never reads a token. Raises
    BindError on a ref the policy does not grant, a templated host, a stdio
    MCP server with a ref, or two configs declaring one source name with a
    different request shape or credential. Two configs that declare one
    source with different approval settings share one binding that keeps the
    strictest of each (see _merge_strictest)."""
    from fabri.tools.sources import secret_refs

    out: dict[str, SourceBinding] = {}
    seen_configs: set[str] = set()

    def walk(cfg: Mapping, depth: int, where: str) -> None:
        tools = cfg.get("tools") if isinstance(cfg, Mapping) else None
        if not isinstance(tools, Mapping):
            return
        decls = tools.get("sources")
        credentialed = [d for d in decls if isinstance(d, Mapping) and secret_refs(d)] \
            if isinstance(decls, (list, tuple)) else []
        if credentialed:
            policy = _policy(tools.get("policy"), where)
            enabled = tools.get("enabled")
            enabled = set(enabled) if isinstance(enabled, (list, tuple)) else None
            for decl in credentialed:
                bound = _bind_one(decl, policy, enabled, actor=actor, base_dir=base_dir, transport=transport)
                prior = out.get(bound.name)
                out[bound.name] = bound if prior is None else _merge_strictest(prior, bound)
        if depth >= max_depth:
            return
        for entry in tools.get("agents") or ():
            if not isinstance(entry, Mapping) or not entry.get("config"):
                continue
            path = str(Path(str(entry["config"])).resolve())
            if path in seen_configs:
                continue
            seen_configs.add(path)
            sub = _load_yaml(path)
            if sub is not None:
                walk(sub, depth + 1, f"agent config {Path(path).name}")

    walk(config, 0, "run config")
    return out


# ---------------------------------------------------------------- sockets and peers


def socket_path_for(run_home: Path, session_id: str) -> Path:
    """``run_home/brk.sock`` when that path is at most 96 bytes, else a short
    path under a per-user temp directory. The parent directory is 0700."""
    run_home = Path(run_home)
    candidate = run_home / "brk.sock"
    if len(str(candidate).encode("utf-8")) <= MAX_SOCKET_PATH_BYTES:
        run_home.mkdir(parents=True, exist_ok=True)
        os.chmod(run_home, 0o700)
        return candidate
    parent = Path(tempfile.gettempdir()) / f"fabri-brk-{os.getuid()}"
    try:
        parent.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise BindError(f"credential broker socket directory cannot be created ({type(exc).__name__})") from None
    info = os.lstat(parent)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise BindError("credential broker socket directory is not owned by the service user")
    os.chmod(parent, 0o700)
    short = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)[:12] or uuid.uuid4().hex[:12]
    return parent / f"{short}.sock"


_SOL_LOCAL = 0
_LOCAL_PEERCRED = 0x001
_LOCAL_PEERPID = 0x002
_XUCRED_SIZE = 76


def _linux_peercred(conn: socket.socket) -> tuple[int, int] | None:
    opt = getattr(socket, "SO_PEERCRED", None)
    if opt is None:
        return None
    try:
        pid, uid, _gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, opt, struct.calcsize("3i")))
    except OSError:
        return None
    return pid, uid


def peer_uid(conn: socket.socket) -> int | None:
    """The uid of the process on the other end of a Unix socket, or None
    when the platform does not say."""
    if sys.platform.startswith("linux"):
        cred = _linux_peercred(conn)
        return cred[1] if cred else None
    if sys.platform == "darwin":
        try:
            raw = conn.getsockopt(_SOL_LOCAL, _LOCAL_PEERCRED, _XUCRED_SIZE)
        except OSError:
            return None
        return struct.unpack_from("I", raw, 4)[0] if len(raw) >= 8 else None
    return None


def peer_pid(conn: socket.socket) -> int | None:
    """The pid of the process on the other end of a Unix socket, or None."""
    if sys.platform.startswith("linux"):
        cred = _linux_peercred(conn)
        return cred[0] if cred and cred[0] > 0 else None
    if sys.platform == "darwin":
        try:
            raw = conn.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID, 4)
        except OSError:
            return None
        pid = struct.unpack("i", raw)[0] if len(raw) == 4 else 0
        return pid if pid > 0 else None
    return None


_LIBPROC: Any = None
_PROC_PIDTBSDINFO = 3
_PROC_BSDINFO_SIZE = 136
_PBI_PPID_OFFSET = 16


def parent_pid(pid: int) -> int | None:
    """The parent of `pid`, or None when it cannot be read."""
    global _LIBPROC
    if sys.platform.startswith("linux"):
        try:
            text = Path(f"/proc/{pid}/stat").read_text()
            return int(text.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return None
    if sys.platform == "darwin":
        try:
            if _LIBPROC is None:
                _LIBPROC = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib")
            buf = ctypes.create_string_buffer(_PROC_BSDINFO_SIZE)
            n = _LIBPROC.proc_pidinfo(int(pid), _PROC_PIDTBSDINFO, ctypes.c_uint64(0), buf, _PROC_BSDINFO_SIZE)
            if n == _PROC_BSDINFO_SIZE:
                return struct.unpack_from("I", buf.raw, _PBI_PPID_OFFSET)[0]
        except (OSError, AttributeError):
            pass
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(int(pid))], capture_output=True, text=True,
                             timeout=5, check=False)
        return int(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def is_descendant(pid: int, ancestor: int, *, max_hops: int = 64) -> bool:
    """True when `pid` is `ancestor` or one of its descendants."""
    current: int | None = pid
    for _ in range(max_hops):
        if current is None or current <= 1:
            return False
        if current == ancestor:
            return True
        current = parent_pid(current)
    return False


def _read_request_line(conn: socket.socket, limit: int = MAX_REQUEST_BYTES) -> bytes | None:
    """One request line (without the newline), or None when no newline came
    within `limit` bytes or before the deadline."""
    deadline = time.monotonic() + READ_TIMEOUT_S
    buf = bytearray()
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        conn.settimeout(left)
        try:
            chunk = conn.recv(min(65536, limit + 1 - len(buf)))
        except (TimeoutError, OSError):
            return None
        if not chunk:
            return None
        cut = chunk.find(b"\n")
        if cut >= 0:
            buf += chunk[:cut]
            return bytes(buf) if len(buf) <= limit else None
        buf += chunk
        if len(buf) > limit:
            return None


def _client_gone(conn: socket.socket) -> bool:
    """True when the client closed its end (a peek reads EOF)."""
    timeout = conn.gettimeout()
    try:
        conn.setblocking(False)
        try:
            data = conn.recv(1, socket.MSG_PEEK)
        except (BlockingIOError, InterruptedError):
            return False
        except OSError:
            return True
        return data == b""
    finally:
        try:
            conn.settimeout(timeout)
        except OSError:
            pass


# ---------------------------------------------------------------- approval text and grants


def _label(value: Any, cap: int = 80) -> str:
    return _LABEL.sub("_", str(value))[:cap] or "?"


def _redacted(value: Any) -> Any:
    from fabri.tools.approver import redact_args

    return redact_args(value)


APPROVAL_CARD_BODY_CAP = 2000


class RequestNotShowable(ValueError):
    """The approval card cannot show this request exactly (a body over the
    card cap, a body that is not JSON or form data, or JSON with a repeated
    key). The message names the reason, never a value."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise RequestNotShowable("the JSON body repeats a key")
        out[key] = value
    return out


def _shown_pairs(pairs: list[tuple[str, str]]) -> list[list[str]]:
    """Query or form pairs in the order they are sent, repeats kept, each
    value redacted when its name (or the object as a whole) names a secret."""
    whole = _redacted(dict(pairs))
    out = []
    for key, value in pairs:
        shown = _redacted({key: value})[key]
        if whole.get(key) == "[redacted]":
            shown = "[redacted]"
        out.append([key, shown])
    return out


def describe_request(
    tool: str,
    source: str,
    cls: str,
    provider: str | None,
    request: httpx.Request,
    extra_headers: Mapping[str, str],
    cap: int = APPROVAL_CARD_BODY_CAP,
    *,
    exact: bool = False,
) -> str:
    """The approval card text for one prepared request: method, URL, the
    query as the ordered list of name/value pairs (repeats kept), the literal
    extra headers with their values, and the body with secrets redacted.
    Credential headers are not in the request yet, so no token can appear
    here.

    With `exact`, a request the card cannot show in full raises
    RequestNotShowable instead of being cut or summarised: a body over `cap`
    characters, a body that is neither JSON nor form data, or a JSON body
    with a repeated key (a server may read the copy the card does not show)."""
    url = request.url
    lines = [
        f"{tool} needs your approval (class {cls})",
        f"source: {source} ({provider or 'no stored credential'})",
        f"request: {request.method} {url.scheme}://{url.netloc.decode('ascii', 'replace')}{url.path}",
    ]
    if url.query:
        query = parse_qsl(url.query.decode("ascii", "replace"), keep_blank_values=True)
        lines.append("query: " + json.dumps(_shown_pairs(query), ensure_ascii=False))
    if extra_headers:
        shown = _redacted({str(k): str(v) for k, v in extra_headers.items()})
        lines.append("headers: " + ", ".join(f"{k}: {v}" for k, v in sorted(shown.items())))
    else:
        lines.append("headers: none")
    content = request.content or b""
    body_text: str
    if not content:
        body_text = "(empty)"
    else:
        ctype = request.headers.get("content-type", "")
        try:
            if "x-www-form-urlencoded" in ctype:
                form = parse_qsl(content.decode("utf-8"), keep_blank_values=True)
                body_text = "form: " + json.dumps(_shown_pairs(form), ensure_ascii=False)
            else:
                hook = _reject_duplicate_keys if exact else None
                parsed = json.loads(content, object_pairs_hook=hook)
                body_text = json.dumps(_redacted(parsed), sort_keys=True, indent=2, ensure_ascii=False)
        except RequestNotShowable:
            raise
        except (ValueError, UnicodeDecodeError):
            if exact:
                raise RequestNotShowable("the body is not JSON or form data") from None
            body_text = f"({len(content)} bytes, not JSON)"
    if len(body_text) > cap:
        if exact:
            raise RequestNotShowable(f"the body is {len(body_text)} characters and the approval card "
                                     f"shows at most {cap}")
        body_text = body_text[:cap] + f"... [cut {len(body_text) - cap} chars]"
    lines.append("body:")
    lines.append(body_text)
    return "\n".join(lines)


class GrantTable:
    """One-time grants, each bound to the hash of one prepared request."""

    def __init__(self, ttl_s: float = GRANT_TTL_S) -> None:
        self._ttl = ttl_s
        self._grants: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def mint(self, request_hash: str, *, now: float | None = None) -> str:
        now = time.monotonic() if now is None else now
        grant_id = uuid.uuid4().hex
        with self._lock:
            self._grants[grant_id] = (request_hash, now + self._ttl)
        return grant_id

    def consume(self, grant_id: str, request_hash: str, *, now: float | None = None) -> bool:
        """True once for a live grant whose hash matches. The grant is gone
        after any call, matching or not."""
        now = time.monotonic() if now is None else now
        with self._lock:
            entry = self._grants.pop(grant_id, None)
        if entry is None:
            return False
        bound, expires = entry
        return now <= expires and hmac.compare_digest(bound, request_hash)

    def discard(self, grant_id: str) -> None:
        with self._lock:
            self._grants.pop(grant_id, None)

    def clear(self) -> None:
        with self._lock:
            self._grants.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._grants)


# ---------------------------------------------------------------- per-call credential store


class _CallStore:
    """Resolves one call's refs exactly once each, through ConnectionStore.

    A row that exists decides alone: not ``ok``, another user's, or no usable
    token refuses, and the environment is never asked. Only a ref with no row
    falls back to ``FABRI_CRED_*`` in the service env."""

    def __init__(self, install_db: Path, actor: str | None, tool: str) -> None:
        self._db = Path(install_db)
        self._actor = actor
        self._tool = tool
        self._values: dict[tuple[str, str], str] = {}
        self._scopes: dict[tuple[str, str], str | None] = {}

    def load(self, refs: Iterable[tuple[str, str]]) -> None:
        connections = None
        if self._db.exists():
            from fabri.service.connection_store import ConnectionStore, default_refreshers

            connections = ConnectionStore(self._db, refreshers=default_refreshers())
        for provider, handle in dict.fromkeys(refs):
            p = _label(provider, 40)
            if handle == _ACTOR:
                raise _Refuse("credential_unavailable",
                              f"{self._tool}: the {p} credential is per user and this run has no user",
                              "unavailable")
            row = connections.get(provider, handle) if connections is not None else None
            if row is not None:
                status = row.get("status")
                if status != "ok":
                    raise _Refuse("credential_unavailable",
                                  f"{self._tool}: the stored {p} credential is {_label(status, 20)}; "
                                  f"run fabri connect {p}", "unavailable")
                if row.get("scope_kind") == "user" and handle != self._actor:
                    raise _Refuse("credential_unavailable",
                                  f"{self._tool}: the {p} credential belongs to another user", "unavailable")
                token = connections.get_token(provider, handle)
                if not token:
                    raise _Refuse("credential_unavailable",
                                  f"{self._tool}: the stored {p} credential has expired or could not be "
                                  f"refreshed; run fabri connect {p}", "unavailable")
                self._scopes[(provider, handle)] = row.get("scope_kind")
            else:
                try:
                    token = EnvCredentialStore().get(provider, handle)
                except CredentialNotFoundError:
                    raise _Refuse("credential_unavailable",
                                  f"{self._tool}: no {p} credential is connected for this run; "
                                  f"run fabri connect {p}", "unavailable") from None
                self._scopes[(provider, handle)] = None
            self._values[(provider, handle)] = token

    def get(self, provider: str, handle: str) -> str:
        try:
            return self._values[(provider, handle)]
        except KeyError:
            raise CredentialNotFoundError("credential was not resolved for this call") from None

    def scope_kind(self, provider: str, handle: str) -> str | None:
        return self._scopes.get((provider, handle))

    def values(self) -> list[str]:
        return list(self._values.values())

    def __repr__(self) -> str:
        return "_CallStore()"


# ---------------------------------------------------------------- the broker


class _Slot:
    """One send slot of a run, released while a request waits for a human."""

    def __init__(self, sem: threading.BoundedSemaphore, stop: threading.Event) -> None:
        self._sem = sem
        self._stop = stop
        self.held = False

    def acquire(self) -> bool:
        while not self._stop.is_set():
            if self._sem.acquire(timeout=0.2):
                self.held = True
                return True
        return False

    def release(self) -> None:
        if self.held:
            self.held = False
            self._sem.release()


class _Run:
    def __init__(self, binding: BrokerBinding, listener, client: httpx.Client, run_pid: int | None) -> None:
        self.binding = binding
        self.listener = listener
        self.client = client
        self.grants = GrantTable()
        self.slots = threading.BoundedSemaphore(MAX_INFLIGHT_PER_RUN)
        self.stop = threading.Event()
        self.server: socket.socket | None = None
        self.thread: threading.Thread | None = None
        self.run_pid = run_pid
        self.pid_set = threading.Event()
        if run_pid is not None:
            self.pid_set.set()
        self.lock = threading.Lock()
        self.pending_approvals = 0
        self.mcp_tools: dict[str, dict[str, tuple[str, Any]]] = {}


def _refusal(status: str, text: str) -> dict:
    return {"v": PROTOCOL_VERSION, "ok": False, "error": text, "result": {"status": status, "code": None}}


def _clamp_timeout(raw: Any) -> float:
    try:
        value = float(raw) if raw is not None else 30.0
    except (TypeError, ValueError):
        value = 30.0
    if value != value:  # NaN
        value = 30.0
    return min(max(value, MIN_SEND_TIMEOUT_S), MAX_SEND_TIMEOUT_S)


def _approval_timeout(value: float | None) -> float:
    if value is not None and value > 0:
        return float(value)
    from fabri.tools.approver import _timeout_from_env

    return _timeout_from_env()


def _scalar_ok(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


class CredentialBroker:
    """Owns one Unix socket per bound run and answers credentialed requests."""

    def __init__(
        self,
        *,
        install_db: Path,
        audit_path: str | Path | None = None,
        approval_timeout_s: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.install_db = Path(install_db)
        self.audit_path = Path(audit_path) if audit_path else None
        self.approval_timeout_s = _approval_timeout(approval_timeout_s)
        self._transport = transport
        self._runs: dict[str, _Run] = {}
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"CredentialBroker(runs={len(self._runs)})"

    # -- lifecycle ------------------------------------------------------

    def bind(
        self,
        *,
        session_id: str,
        config_path: Path,
        run_home: Path,
        actor: str | None,
        listener: AskUserListener | None,
        run_pid: int | None = None,
    ) -> BrokerBinding | None:
        """Bind one run. Returns None (no socket, no env) when its config
        declares no credentialed source. Raises BindError for a config that
        cannot be bound and ValueError when the session is already bound.

        Connections are accepted only from the run's own process tree, so
        the caller passes `run_pid` here or calls :meth:`attach_run_pid`
        right after it starts the run."""
        with self._lock:
            if session_id in self._runs:
                raise ValueError("this session is already bound to the credential broker")
        config = _load_yaml(config_path)
        if config is None:
            return None
        sources = bind_sources(config, actor=actor, transport=self._transport)
        if not sources:
            return None
        path = socket_path_for(Path(run_home), session_id)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(path))
            os.chmod(path, 0o600)
            server.listen(64)
            server.settimeout(0.5)
        except OSError:
            server.close()
            raise
        binding = BrokerBinding(session_id=session_id, socket_path=str(path), key=secrets.token_hex(32),
                                actor=actor, sources=sources)
        # Without an injected transport the client connects only to addresses
        # checked in the same step that connects (no DNS rebinding between
        # the check and the send). trust_env=False keeps httpx from mounting
        # proxy transports from the environment, which would bypass that.
        client = httpx.Client(transport=self._transport or PinnedTransport(), follow_redirects=False,
                              trust_env=False)
        run = _Run(binding, listener, client, run_pid)
        run.server = server
        with self._lock:
            if session_id in self._runs:
                server.close()
                client.close()
                raise ValueError("this session is already bound to the credential broker")
            self._runs[session_id] = run
        run.thread = threading.Thread(target=self._accept_loop, args=(run,), name=f"cred-broker-{session_id[:8]}",
                                      daemon=True)
        run.thread.start()
        return binding

    def attach_run_pid(self, session_id: str, pid: int) -> None:
        """Tell the broker which process is the run, once it is started."""
        with self._lock:
            run = self._runs.get(session_id)
        if run is None:
            return
        run.run_pid = int(pid)
        run.pid_set.set()

    def release(self, session_id: str) -> None:
        """Close and unlink the run's socket, drop its grants and caches,
        wake every request waiting on it. Unknown sessions are a no-op."""
        with self._lock:
            run = self._runs.pop(session_id, None)
        if run is None:
            return
        run.stop.set()
        run.pid_set.set()
        if run.server is not None:
            try:
                run.server.close()
            except OSError:
                pass
        try:
            Path(run.binding.socket_path).unlink()
        except (FileNotFoundError, OSError):
            pass
        run.grants.clear()
        with run.lock:
            run.mcp_tools.clear()
        try:
            run.client.close()
        except Exception:  # noqa: BLE001 -- closing is best effort
            pass

    def close(self) -> None:
        with self._lock:
            sessions = list(self._runs)
        for session_id in sessions:
            self.release(session_id)

    def bindings(self) -> dict[str, BrokerBinding]:
        with self._lock:
            return {sid: run.binding for sid, run in self._runs.items()}

    def _run_for(self, binding: BrokerBinding) -> _Run:
        with self._lock:
            run = self._runs.get(binding.session_id)
        if run is None or run.binding is not binding:
            raise _Refuse("refused", "credential broker: this run is no longer bound")
        return run

    # -- connections ----------------------------------------------------

    def _accept_loop(self, run: _Run) -> None:
        server = run.server
        assert server is not None
        while not run.stop.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve_conn, args=(run, conn), name="cred-broker-conn",
                             daemon=True).start()

    def _peer_allowed(self, run: _Run, conn: socket.socket) -> bool:
        uid = peer_uid(conn)
        if uid is not None and uid != os.getuid():
            return False
        pid = peer_pid(conn)
        if pid is None:
            # The platform cannot name the peer; the file mode and the key remain.
            return True
        if not run.pid_set.wait(PID_ATTACH_WAIT_S) or run.stop.is_set() or run.run_pid is None:
            return False
        return is_descendant(pid, run.run_pid)

    def _serve_conn(self, run: _Run, conn: socket.socket) -> None:
        slot = _Slot(run.slots, run.stop)
        try:
            if not self._peer_allowed(run, conn):
                logger.info("credential broker: closed a connection from outside run %s",
                            run.binding.session_id[:8])
                return
            if not slot.acquire():
                return
            line = _read_request_line(conn)
            if line is None:
                reply = _refusal("refused", "credential broker refused the request: not one line within "
                                            f"{MAX_REQUEST_BYTES} bytes")
            else:
                reply = self._handle_line(run.binding, line, conn=conn, slot=slot)
            conn.settimeout(10.0)
            try:
                conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
            except OSError:
                pass
        except Exception as exc:  # noqa: BLE001 -- a broken request must not kill the broker
            logger.warning("credential broker: request failed (%s)", type(exc).__name__)
        finally:
            slot.release()
            try:
                conn.close()
            except OSError:
                pass

    # -- request handling ------------------------------------------------

    def _handle_line(self, binding: BrokerBinding, line: bytes, *, conn: socket.socket | None = None,
                     slot: _Slot | None = None) -> dict:
        """Answer one request line. Never raises; every failure is a refusal."""
        own_slot = None
        try:
            run = self._run_for(binding)
            if slot is None:
                own_slot = slot = _Slot(run.slots, run.stop)
                if not slot.acquire():
                    raise _Refuse("refused", "credential broker: this run is no longer bound")
            if len(line) > MAX_REQUEST_BYTES:
                raise _Refuse("refused", "credential broker refused the request: too large")
            try:
                req = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise _Refuse("refused", "credential broker refused the request: not JSON") from None
            if not isinstance(req, dict):
                raise _Refuse("refused", "credential broker refused the request: not a JSON object")
            key = req.get("key")
            if not isinstance(key, str) or not hmac.compare_digest(key.encode("utf-8"),
                                                                   binding.key.encode("utf-8")):
                time.sleep(BAD_KEY_DELAY_S)
                self._audit(binding, None, None, None, None, None, "refused", None)
                return _refusal("refused", "credential broker refused the capability")
            if req.get("v") != PROTOCOL_VERSION:
                raise _Refuse("refused", "credential broker refused the request: unsupported protocol version")
            op = req.get("op")
            if op == "http":
                return self._serve_http(run, req, conn, slot)
            if op == "mcp":
                return self._serve_mcp(run, req, conn, slot)
            raise _Refuse("refused", "credential broker refused the request: op must be http or mcp")
        except _Refuse as r:
            return _refusal(r.status, r.text)
        finally:
            if own_slot is not None:
                own_slot.release()

    def _source(self, binding: BrokerBinding, req: Mapping, tool: str) -> SourceBinding:
        name = req.get("source")
        source = binding.sources.get(name) if isinstance(name, str) else None
        if source is None:
            raise _Refuse("refused", f"{tool}: source {_label(name, 64)} is not bound to this run")
        return source

    def _check_http(self, binding: BrokerBinding, req: Mapping
                    ) -> tuple[SourceBinding, ToolBinding, httpx.Request, dict[str, str], str]:
        from fabri.tools.approval import stricter
        from fabri.tools.connectors import request_floor
        from fabri.tools.http_call import (
            HeaderSpecError,
            PathParamError,
            _floor_drift,
            check_extra_headers,
            prepare,
            render_path_value,
        )

        tool_name = req.get("tool")
        tool = _label(tool_name, 64)
        source = self._source(binding, req, tool)
        if source.kind not in ("http", "openapi"):
            raise _Refuse("refused", f"{tool}: source {source.name} is not an http or openapi source")
        tb = source.tools.get(tool_name) if isinstance(tool_name, str) else None
        if tb is None:
            raise _Refuse("refused", f"{tool}: not a tool of source {source.name} in this run")
        method = req.get("method")
        if method is not None and (not isinstance(method, str) or method.upper() != tb.method):
            raise _Refuse("refused", f"{tool}: method does not match the bound tool ({tb.method})")
        path_params = req.get("path_params") or {}
        if not isinstance(path_params, Mapping) or not all(isinstance(k, str) for k in path_params):
            raise _Refuse("refused", f"{tool}: path_params must be an object")
        unknown = sorted(set(path_params) - set(tb.placeholders))
        if unknown:
            raise _Refuse("refused", f"{tool}: unknown path parameters {[_label(u, 40) for u in unknown]}")
        url = tb.template_url
        for name in tb.placeholders:
            if name not in path_params or path_params[name] is None:
                raise _Refuse("refused", f"{tool}: missing path parameter {_label(name, 40)!r}")
            value = path_params[name]
            if not _scalar_ok(value):
                raise _Refuse("refused", f"{tool}: path parameter {_label(name, 40)!r} must be a scalar")
            try:
                rendered = render_path_value(name, value)
            except PathParamError as exc:
                raise _Refuse("refused", f"{tool}: {exc}") from None
            url = url.replace("{" + name + "}", rendered)
        self._check_url_shape(tool, tb.template_url, url)
        params = req.get("params")
        if params is not None:
            if not isinstance(params, Mapping) or not all(isinstance(k, str) for k in params):
                raise _Refuse("refused", f"{tool}: params must be an object")
            for value in params.values():
                if not (_scalar_ok(value) or (isinstance(value, list) and all(_scalar_ok(v) for v in value))):
                    raise _Refuse("refused", f"{tool}: params values must be scalars or lists of scalars")
        json_body, form_body = req.get("json"), req.get("form")
        if json_body is not None and form_body is not None:
            raise _Refuse("refused", f"{tool}: send json or form, not both")
        if form_body is not None and not isinstance(form_body, Mapping):
            raise _Refuse("refused", f"{tool}: form must be an object")
        try:
            extra = check_extra_headers(req.get("extra_headers"), declared=tuple(source.header_specs))
        except HeaderSpecError as exc:
            raise _Refuse("refused", f"{tool}: {exc}") from None
        except (TypeError, ValueError, AttributeError):
            raise _Refuse("refused", f"{tool}: extra_headers must be an object of strings") from None
        try:
            prepared = prepare(tb.method, url, params=params, json_body=json_body, form_body=form_body)
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            raise _Refuse("refused", f"{tool}: request refused: cannot encode it ({type(exc).__name__})") from None
        if _floor_drift(tb.template_url, tb.method, url):
            raise _Refuse("refused", f"{tool}: request refused: the rendered path is outside what this tool "
                                     "was approved for")
        cls = tb.approval
        _owner, floor = request_floor(url, tb.method)
        cls = stricter(cls, floor or cls)
        return source, tb, prepared, extra, cls

    @staticmethod
    def _check_url_shape(tool: str, template: str, url: str) -> None:
        from fabri.tools.connector_catalog import _hosts_of
        from fabri.tools.security.ssrf import validate_url

        try:
            got, want = urlsplit(url), urlsplit(template)
        except ValueError:
            raise _Refuse("refused", f"{tool}: request refused: the URL cannot be parsed") from None
        if got.query or got.fragment or "?" in url or "#" in url:
            raise _Refuse("refused", f"{tool}: request refused: the URL may not carry a query or fragment; "
                                     "send params")
        if got.username is not None or got.password is not None or "@" in got.netloc:
            raise _Refuse("refused", f"{tool}: request refused: the URL may not carry userinfo")
        if any(seg in (".", "..") for seg in got.path.split("/")):
            # httpx drops these when it builds the request, which would send to another path.
            raise _Refuse("refused", f"{tool}: request refused: the URL path holds a dot segment")
        if got.scheme.lower() != want.scheme.lower() or got.netloc.lower() != want.netloc.lower():
            raise _Refuse("refused", f"{tool}: request refused: the URL host differs from the bound template")
        hosts = _hosts_of(url)
        if len(hosts) != 1 or hosts != _hosts_of(template):
            raise _Refuse("refused", f"{tool}: request refused: the URL host reads differently to two parsers")
        try:
            validate_url(url)
        except ValueError as exc:
            reason = str(exc).removeprefix("refused: ")
            raise _Refuse("refused", f"{tool}: request refused: {reason}") from None

    @staticmethod
    def _revalidate(tool: str, url: str) -> None:
        """Resolve the host again right before the send, so a DNS answer that
        changed during an approval wait is checked too."""
        from fabri.tools.security.ssrf import validate_url

        try:
            validate_url(url)
        except ValueError as exc:
            raise _Refuse("refused", f"{tool}: request refused: {str(exc).removeprefix('refused: ')}") from None

    def _serve_http(self, run: _Run, req: Mapping, conn: socket.socket | None, slot: _Slot) -> dict:
        from fabri.tools.http_call import request_hash, secret_ref_provider, send_prepared

        binding = run.binding
        audit: dict[str, Any] = {"source": req.get("source"), "tool": req.get("tool")}
        try:
            source, tb, prepared, extra, cls = self._check_http(binding, req)
            audit.update(source=source.name, tool=tb.name, method=tb.method, url=prepared.url, cls=cls)
            timeout = _clamp_timeout(req.get("timeout_s"))
            digest = request_hash(prepared, extra)
            grant_id = None
            if cls == "always_approve":
                text = self._card(tb.name, source.name, cls, secret_ref_provider(source.header_specs),
                                  prepared, extra)
                grant_id = self._approve_with_slot(run, slot, tb.name, text, digest)
            store = self._store_for(binding, source, tool=tb.name)
            self._final_checks(run, conn, grant_id, prepared, extra, tb.name, digest)
            self._revalidate(tb.name, str(prepared.url))
            out = send_prepared(run.client, prepared, headers=source.header_specs, store=store,
                                extra_headers=extra, timeout_s=timeout, tool_name=tb.name,
                                floor_url=tb.template_url)
        except _Refuse as r:
            self._audit(binding, audit.get("source"), audit.get("tool"), audit.get("method"), audit.get("url"),
                        audit.get("cls"), r.decision, None)
            return _refusal(r.status, r.text)
        result = out.get("result") if isinstance(out, Mapping) else None
        code = result.get("code") if isinstance(result, Mapping) else None
        self._audit(binding, source.name, tb.name, tb.method, prepared.url, cls, "sent", code)
        return {**out, "v": PROTOCOL_VERSION}

    @staticmethod
    def _card(tool: str, source: str, cls: str, provider: str | None, request: httpx.Request,
              extra: Mapping[str, str]) -> str:
        """The approval card, or a refusal when the card cannot show the
        exact request a person would be approving."""
        try:
            return describe_request(tool, source, cls, provider, request, extra, exact=True)
        except RequestNotShowable as exc:
            raise _Refuse("refused", f"{tool}: this call needs approval and the approval card cannot show it "
                                     f"exactly ({exc}); the call was not made. Send a smaller request.") from None

    def _approve_with_slot(self, run: _Run, slot: _Slot, tool: str, text: str, digest: str) -> str:
        """Ask a human with the send slot released, then take a slot back."""
        slot.release()
        try:
            grant_id, status = self._ask(run, tool, text, digest)
        finally:
            if not slot.acquire():
                raise _Refuse("approval_timeout", f"{tool}: the run ended before the call was made; "
                                                  "the call was not made", "timeout")
        if grant_id is None:
            decision = {"approval_declined": "declined", "approval_timeout": "timeout"}.get(status, "refused")
            texts = {
                "approval_required": f"{tool} needs human approval and this run has no approver; "
                                     "the call was not made",
                "approval_declined": f"{tool}: the person declined this call; the call was not made",
                "approval_timeout": f"{tool}: no answer to the approval question in time; the call was not made",
            }
            raise _Refuse(status, texts.get(status, f"{tool}: approval failed; the call was not made"), decision)
        return grant_id

    def _final_checks(self, run: _Run, conn: socket.socket | None, grant_id: str | None,
                      prepared: httpx.Request, extra: Mapping[str, str], tool: str, digest: str) -> None:
        """Right before the send: the run is still bound, the client is still
        there, and the grant (if any) matches the very request about to go."""
        from fabri.tools.http_call import request_hash

        if run.stop.is_set():
            if grant_id:
                run.grants.discard(grant_id)
            raise _Refuse("approval_timeout", f"{tool}: the run ended before the call was made", "timeout")
        if conn is not None and _client_gone(conn):
            if grant_id:
                run.grants.discard(grant_id)
            raise _Refuse("refused", f"{tool}: the caller went away; the call was not made")
        if grant_id is not None and not run.grants.consume(grant_id, request_hash(prepared, extra)):
            raise _Refuse("refused", f"{tool}: the approval does not match this request; the call was not made")

    def _ask(self, run: _Run, tool: str, text: str, digest: str) -> tuple[str | None, str]:
        listener = run.listener
        if listener is None:
            return None, "approval_required"
        with run.lock:
            if run.pending_approvals >= MAX_PENDING_APPROVALS_PER_RUN:
                return None, "approval_required"
            run.pending_approvals += 1
        question_id = uuid.uuid4().hex
        try:
            pending = listener.ask_local(question_id=question_id, kind="approval", question=text,
                                         options=["approve", "deny"], default="deny")
            deadline = time.monotonic() + self.approval_timeout_s
            while not pending.event.wait(0.1):
                if run.stop.is_set() or time.monotonic() >= deadline:
                    return None, "approval_timeout"
            reply = pending.reply
            if run.stop.is_set() or not isinstance(reply, Mapping):
                return None, "approval_timeout"
            if reply.get("question_id") != question_id:
                return None, "approval_declined"
            selected = reply.get("selected_option")
            if selected is not None:
                yes = selected == "approve"
            else:
                answer = reply.get("answer")
                yes = isinstance(answer, str) and answer.strip().lower() in {"approve", "yes", "y"}
            if not yes:
                return None, "approval_declined"
            return run.grants.mint(digest), "approved"
        finally:
            try:
                listener.forget(question_id)
            except Exception:  # noqa: BLE001 -- forgetting is best effort
                pass
            with run.lock:
                run.pending_approvals -= 1

    def _approve(self, binding: BrokerBinding, text: str, request_hash: str) -> str | None:
        """Ask the run's listener; a grant id on approve, else None."""
        try:
            run = self._run_for(binding)
        except _Refuse:
            return None
        grant_id, _status = self._ask(run, "request", text, request_hash)
        return grant_id

    def _store_for(self, binding: BrokerBinding, source: SourceBinding, *, tool: str = "request"):
        """A store that holds this call's credentials, each resolved once
        through ConnectionStore.get_token, wrapped for the audit log. The
        source's grants were enforced on its declared refs at bind time."""
        call = _CallStore(self.install_db, binding.actor, tool)
        call.load(_spec_refs(source.header_specs))
        return ScopedCredentialStore(call, None, audit_path=self.audit_path,
                                     actor=f"broker {binding.session_id[:8]}")

    # -- mcp ------------------------------------------------------------

    def _mcp_class(self, run: _Run, source: SourceBinding, body: Mapping) -> str:
        from fabri.tools.approval import check_approval, mcp_approval, stricter
        from fabri.tools.connectors import catalog_minimum
        from fabri.tools.mcp_client import _sanitize_name

        method = body.get("method")
        if not isinstance(method, str):
            return "always_approve"
        if method in MCP_READ_METHODS:
            return "read"
        if method != "tools/call":
            return "always_approve"
        params = body.get("params")
        remote = params.get("name") if isinstance(params, Mapping) else None
        if not isinstance(remote, str):
            return "always_approve"
        with run.lock:
            known = run.mcp_tools.get(source.name, {}).get(remote)
        if known is None:
            return "always_approve"
        description, annotations = known
        fabri_name = f"mcp_{_sanitize_name(source.name)}_{_sanitize_name(remote)}"
        cls = mcp_approval(remote, description, annotations)
        for decl in source.all_approval_decls():
            if isinstance(decl, Mapping):
                decl = decl.get(fabri_name)
            if decl is not None:
                try:
                    cls = stricter(cls, check_approval(decl))
                except ValueError:
                    return "always_approve"
        for pattern, floor in source.floors:
            if fnmatch.fnmatchcase(fabri_name, pattern):
                cls = stricter(cls, floor)
        for owner in dict.fromkeys(o for o in (*source.all_catalog_markers(), source.catalog_owner) if o):
            try:
                minimum = catalog_minimum(owner, fabri_name, None)
            except Exception:  # noqa: BLE001 -- an unknown catalog id fails closed
                return "always_approve"
            if minimum:
                cls = stricter(cls, minimum)
        return cls

    def _check_mcp(self, binding: BrokerBinding, req: Mapping
                   ) -> tuple[SourceBinding, bytes, dict[str, str], str]:
        from fabri.tools.http_call import HeaderSpecError, check_extra_headers

        name = _label(req.get("source"), 64)
        source = self._source(binding, req, f"mcp {name}")
        if source.kind != "mcp" or not source.url:
            raise _Refuse("refused", f"mcp {name}: source {source.name} is not an http MCP source")
        raw = req.get("body_b64")
        if not isinstance(raw, str):
            raise _Refuse("refused", f"mcp {name}: body_b64 is required")
        try:
            body = base64.b64decode(raw.encode("ascii"), validate=True)
        except (binascii.Error, ValueError, UnicodeEncodeError):
            raise _Refuse("refused", f"mcp {name}: body_b64 is not base64") from None
        try:
            parsed = json.loads(body.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        except RequestNotShowable:
            # The broker classifies the last copy of a key; a server may read the first.
            raise _Refuse("refused", f"mcp {name}: the body repeats a JSON key") from None
        except (UnicodeDecodeError, ValueError):
            raise _Refuse("refused", f"mcp {name}: the body is not JSON") from None
        if not isinstance(parsed, dict) or parsed.get("jsonrpc") != "2.0":
            raise _Refuse("refused", f"mcp {name}: the body must be one JSON-RPC 2.0 object")
        try:
            extra = check_extra_headers(req.get("extra_headers"), declared=tuple(source.header_specs))
        except HeaderSpecError as exc:
            raise _Refuse("refused", f"mcp {name}: {exc}") from None
        except (TypeError, ValueError, AttributeError):
            raise _Refuse("refused", f"mcp {name}: extra_headers must be an object of strings") from None
        run = self._run_for(binding)
        return source, body, extra, self._mcp_class(run, source, parsed)

    def _serve_mcp(self, run: _Run, req: Mapping, conn: socket.socket | None, slot: _Slot) -> dict:
        from fabri.tools.http_call import request_hash, resolve_headers, secret_ref_provider
        from fabri.tools.mcp_client import _secret_pieces

        binding = run.binding
        audit: dict[str, Any] = {"source": req.get("source"), "tool": "mcp"}
        try:
            source, body, extra, cls = self._check_mcp(binding, req)
            parsed = json.loads(body)
            rpc = str(parsed.get("method") or "response")
            params = parsed.get("params")
            tool = rpc
            if rpc == "tools/call" and isinstance(params, Mapping) and isinstance(params.get("name"), str):
                tool = f"tools/call {_label(params['name'], 64)}"
            audit.update(source=source.name, tool=tool, method="POST", url=httpx.URL(source.url), cls=cls)
            label = f"{source.name} {tool}"
            self._revalidate(label, source.url)
            timeout = _clamp_timeout(req.get("timeout_s"))
            request = httpx.Request("POST", source.url, content=body)
            digest = request_hash(request, extra)
            grant_id = None
            if cls == "always_approve":
                text = self._card(label, source.name, cls, secret_ref_provider(source.header_specs),
                                  request, extra)
                grant_id = self._approve_with_slot(run, slot, label, text, digest)
            store = self._store_for(binding, source, tool=label)
            self._final_checks(run, conn, grant_id, request, extra, label, digest)
            try:
                resolved = resolve_headers(source.header_specs, store)
            except ValueError:
                raise _Refuse("credential_unavailable", f"{label}: the stored credential could not be applied",
                              "unavailable") from None
            self._revalidate(label, source.url)
            for k, v in {**resolved, **extra}.items():
                request.headers[k] = v
            request.extensions["timeout"] = {"connect": timeout, "read": timeout, "write": timeout,
                                             "pool": timeout}
            pieces = _secret_pieces([v for v in resolved.values() if isinstance(v, str)])
            try:
                resp = run.client.send(request, stream=True)
                try:
                    raw_body, truncated = _read_capped(resp, pieces)
                finally:
                    resp.close()
            except httpx.HTTPError as exc:
                self._audit(binding, source.name, tool, "POST", request.url, cls, "sent", None)
                return {"v": PROTOCOL_VERSION, "ok": False, "error": f"{label}: request failed: "
                        f"{type(exc).__name__}", "result": {"status": "error", "code": None}}
        except _Refuse as r:
            self._audit(binding, audit.get("source"), audit.get("tool"), audit.get("method"), audit.get("url"),
                        audit.get("cls"), r.decision, None)
            return _refusal(r.status, r.text)
        headers = {}
        for name in ("content-type", "mcp-session-id"):
            if name in resp.headers:
                headers[name] = _redact_text(resp.headers[name], pieces)
        if rpc == "tools/list" and 200 <= resp.status_code < 300 and not truncated:
            self._cache_tools(run, source.name, raw_body)
        self._audit(binding, source.name, tool, "POST", request.url, cls, "sent", resp.status_code)
        return {"v": PROTOCOL_VERSION, "ok": True, "status": resp.status_code, "headers": headers,
                "body_b64": base64.b64encode(raw_body).decode("ascii"), "truncated": truncated}

    def _cache_tools(self, run: _Run, source: str, body: bytes) -> None:
        for message in _jsonrpc_messages(body):
            result = message.get("result") if isinstance(message, Mapping) else None
            tools = result.get("tools") if isinstance(result, Mapping) else None
            if not isinstance(tools, list):
                continue
            with run.lock:
                cache = run.mcp_tools.setdefault(source, {})
                for spec in tools:
                    if isinstance(spec, Mapping) and isinstance(spec.get("name"), str):
                        cache[spec["name"]] = (str(spec.get("description") or ""), spec.get("annotations"))

    # -- audit ----------------------------------------------------------

    def _audit(self, binding: BrokerBinding, source: Any, tool: Any, method: Any, url: Any, cls: Any,
               decision: str, code: Any) -> None:
        host = path = None
        if url is not None:
            try:
                u = httpx.URL(str(url))
                host, path = u.host, u.path
            except (httpx.InvalidURL, ValueError, TypeError):
                pass
        record = {"ts": round(time.time(), 3), "session": binding.session_id,
                  "source": _label(source, 64) if source is not None else None,
                  "tool": _label(tool, 80) if tool is not None else None,
                  "method": method, "host": host, "path": path, "class": cls, "decision": decision,
                  "code": code if isinstance(code, int) else None}
        logger.info("credential broker decision %s", json.dumps(record, sort_keys=True))
        if self.audit_path is None:
            return
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                f.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError as exc:
            logger.warning("credential broker audit write failed (%s)", type(exc).__name__)


def _redact_bytes(data: bytes, pieces: list[str]) -> bytes:
    for piece in pieces:
        for form in {piece, json.dumps(piece)[1:-1]}:
            data = data.replace(form.encode("utf-8"), b"[redacted]")
    return data


def _redact_text(text: str, pieces: list[str]) -> str:
    return _redact_bytes(text.encode("utf-8", "replace"), pieces).decode("utf-8", "replace")


def _read_capped(resp: httpx.Response, pieces: list[str]) -> tuple[bytes, bool]:
    """Read at most MAX_RAW_BODY_BYTES of the body, redacted. A margin past
    the cap is read first so a secret that straddles the cut is still caught."""
    margin = max((len(json.dumps(p)) for p in pieces), default=0)
    limit = MAX_RAW_BODY_BYTES + margin
    buf = bytearray()
    more = False
    for chunk in resp.iter_bytes():
        buf += chunk
        if len(buf) > limit:
            more = True
            break
    data = _redact_bytes(bytes(buf), pieces)
    truncated = more or len(data) > MAX_RAW_BODY_BYTES
    return data[:MAX_RAW_BODY_BYTES], truncated


def _jsonrpc_messages(body: bytes) -> list[Any]:
    """JSON-RPC messages from a plain JSON body or an SSE stream."""
    text = body.decode("utf-8", "replace")
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else [parsed]
    except ValueError:
        pass
    out = []
    for event in re.split(r"\r?\n\r?\n", text):
        data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
        if not data:
            continue
        try:
            out.append(json.loads(data))
        except ValueError:
            continue
    return out


__all__ = [
    "BAD_KEY_DELAY_S",
    "APPROVAL_CARD_BODY_CAP",
    "GRANT_TTL_S",
    "KEY_ENV",
    "MAX_INFLIGHT_PER_RUN",
    "MAX_RAW_BODY_BYTES",
    "MAX_REQUEST_BYTES",
    "MAX_SEND_TIMEOUT_S",
    "SNAPSHOT_ENV",
    "SOCKET_ENV",
    "BindError",
    "BrokerBinding",
    "CredentialBroker",
    "GrantTable",
    "RequestNotShowable",
    "SourceBinding",
    "ToolBinding",
    "bind_sources",
    "describe_request",
    "is_descendant",
    "parent_pid",
    "peer_pid",
    "peer_uid",
    "socket_path_for",
]
