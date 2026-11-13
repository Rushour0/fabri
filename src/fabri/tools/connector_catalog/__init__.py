"""The connector catalog: one module per ready-made connector.

Each module ``connector_catalog/<id>.py`` exposes:

    ENTRY    a ``Connector`` whose ``name`` equals the module name
    expand(name, *, token_env, workspace, remote, url, all_tools) -> dict
             the ``{sources: [...], allow: [...]}`` mapping it writes
    minimum(tool_name, method) -> str | None
             the trusted approval floor for one built tool (None means no floor)

and may expose:

    HOSTS    API hosts the connector owns, lower case, for example ("slack.com",)
    READS    (method, path pattern) pairs it trusts as read, for example
             (("GET", r"/api/conversations[.]list"),); the pattern must match
             the whole URL path

A new connector adds its own hosts by setting ``HOSTS`` in its module (the
Cakemail module is meant to set ``("api.cakemail.dev",)``); nothing in this
file changes. Two modules may not claim the same host.

Modules load in ``MODULE_ORDER``, which is also the order of ``CONNECTORS``
and of ``list_connectors()``. A module listed there whose file does not exist
yet is skipped; any other import error propagates. Every loaded module is
checked once at import time.

Every source a module returns carries ``approval_mode: tighten`` and
``catalog: <id>``, and a stdio MCP source carries ``env_mode: allowlist``
(also the default since sources stopped inheriting the host environment; the
key stays so the written YAML says so).
The ``catalog`` key lets ``fabri.tools.sources`` look up ``minimum`` when the
source builds.

Those two keys live in editable YAML, so they are not what keeps a write
behind approval. For http and openapi tools the floor is bound to the request
target instead: ``request_floor(url, method)`` finds the connector whose
``HOSTS`` contain the URL's host (or a parent of it), and every call to that
host is ``always_approve`` unless its (method, path) is in the connector's
``READS``. That holds whatever the tool is named, whatever method the YAML
says, and whether or not the markers are still there. MCP tools have no URL
per tool, so their floor stays tool-name based: an http MCP server is matched
by the host of its server URL, a stdio one only by its ``catalog`` marker.

This package must not import ``fabri.tools.sources`` (that module imports the
catalog lazily through ``fabri.tools.connectors``).
"""
from __future__ import annotations

import importlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

MODULE_ORDER: tuple[str, ...] = ("github", "linear", "slack", "cakemail", "gmail", "composio")

_CHECK_URL = "https://example.test/mcp"


class ConnectorError(ValueError):
    """An unknown connector or a bad connector option."""


@dataclass(frozen=True)
class OAuthSpec:
    """How a connector runs the OAuth authorization code flow.

    ``client_id_env`` and ``secret_env`` are names of environment variables,
    never values. ``scopes_optional`` maps a short name a user can ask for
    to the full scope string; its help text lives in the connector module.
    ``callback_alias`` is the redirect path the vendor app was registered
    with before the generic ``/connect/<provider>/callback`` existed.
    ``scope_separator`` joins scopes in the authorize URL."""
    authorize_url: str
    token_url: str
    scopes_default: tuple[str, ...]
    scopes_optional: Mapping[str, str] = field(default_factory=dict)
    client_id_env: str = ""
    secret_env: str = ""
    pkce: bool = True
    loopback: bool = False
    extra_authorize: Mapping[str, str] = field(default_factory=dict)
    identity_url: str | None = None
    refresh: bool = True
    callback_alias: str | None = None
    scope_separator: str = " "


@dataclass(frozen=True)
class Connector:
    name: str
    title: str
    summary: str
    kinds: tuple[str, ...]  # source kinds it expands to; the policy must allow them
    token_env: str  # default environment variable that holds the credential
    secret_provider: str | None = None  # provider for --workspace secret refs
    remote_url: str | None = None
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer "
    auth_kind: str = "pat"  # pat | api_key | mcp | oauth
    spec_file: str | None = None  # basename under connector_catalog/specs/
    default_allow: tuple[str, ...] | None = None  # curated tool names, when the connector has a list
    needs_include: bool = False  # the caller must name the exact tools to expose
    needs_url: bool = False  # the caller must give the server URL
    experimental: bool = False  # the expansion is written disabled
    verify_path: str | None = None
    scope_default: str = "org"  # org | user: who a connection belongs to by default
    oauth: OAuthSpec | None = None  # set when the connector can connect through OAuth


def oauth_configured(connector: Connector) -> bool:
    """True when this server can start the connector's OAuth flow: the client
    id variable is set, and so is the client secret variable when the flow has
    no PKCE to stand in for it."""
    spec = connector.oauth
    if spec is None or not spec.client_id_env or not os.environ.get(spec.client_id_env):
        return False
    if spec.pkce:
        return True
    return bool(spec.secret_env and os.environ.get(spec.secret_env))


def _credential(connector: Connector, token_env: str | None, workspace: str | None,
                prefix: str | None = None) -> dict[str, Any]:
    """A header value spec: a secret ref when `workspace` is set, else an env
    var name. `prefix` defaults to the connector's ``auth_prefix``; an empty
    prefix writes no ``prefix`` key."""
    if prefix is None:
        prefix = connector.auth_prefix
    if workspace:
        if not connector.secret_provider:
            raise ConnectorError(f"connector {connector.name} takes no workspace")
        spec: dict[str, Any] = {"secret_ref": f"{connector.secret_provider}:{workspace}"}
    else:
        spec = {"env": token_env or connector.token_env}
    if prefix:
        spec["prefix"] = prefix
    return spec


def _validate(mod: ModuleType, module_name: str) -> Connector:
    entry = getattr(mod, "ENTRY", None)
    if not isinstance(entry, Connector):
        raise ConnectorError(f"connector module {module_name}: ENTRY must be a Connector")
    if entry.name != module_name:
        raise ConnectorError(f"connector module {module_name}: ENTRY.name is {entry.name!r}, "
                             f"expected {module_name!r}")
    for attr in ("expand", "minimum"):
        if not callable(getattr(mod, attr, None)):
            raise ConnectorError(f"connector module {module_name}: {attr} must be callable")
    data = mod.expand(entry.name, token_env=None, workspace=None, remote=False,
                      url=_CHECK_URL if entry.needs_url else None, all_tools=False)
    sources = data.get("sources") if isinstance(data, Mapping) else None
    if not isinstance(sources, list) or not sources:
        raise ConnectorError(f"connector module {module_name}: expand returned no sources")
    kinds = set()
    for source in sources:
        if not isinstance(source, Mapping):
            raise ConnectorError(f"connector module {module_name}: every source must be a mapping")
        kinds.add(source.get("kind"))
        if source.get("approval_mode") != "tighten" or source.get("catalog") != module_name:
            raise ConnectorError(f"connector module {module_name}: source {source.get('name')!r} must carry "
                                 f"approval_mode: tighten and catalog: {module_name}")
    if kinds & {"openapi", "mcp"} and not data.get("allow"):
        raise ConnectorError(f"connector module {module_name}: expand returned an empty allow list")
    hosts = getattr(mod, "HOSTS", ())
    if not isinstance(hosts, tuple) or not all(isinstance(h, str) and h and h == h.lower().strip(".")
                                               for h in hosts):
        raise ConnectorError(f"connector module {module_name}: HOSTS must be a tuple of lower case host names")
    reads = getattr(mod, "READS", ())
    if not isinstance(reads, tuple):
        raise ConnectorError(f"connector module {module_name}: READS must be a tuple of (method, pattern) pairs")
    for pair in reads:
        if (not isinstance(pair, tuple) or len(pair) != 2 or not all(isinstance(x, str) for x in pair)
                or pair[0] != pair[0].upper()):
            raise ConnectorError(f"connector module {module_name}: READS must be a tuple of (METHOD, pattern) pairs")
        re.compile(pair[1])
    if entry.oauth is not None:
        if not isinstance(entry.oauth, OAuthSpec):
            raise ConnectorError(f"connector module {module_name}: ENTRY.oauth must be an OAuthSpec")
        if not callable(getattr(mod, "identity", None)):
            raise ConnectorError(f"connector module {module_name}: identity must be callable when ENTRY.oauth is set")
        if not entry.oauth.client_id_env:
            raise ConnectorError(f"connector module {module_name}: ENTRY.oauth needs client_id_env")
    return entry


def _load_modules(order: tuple[str, ...] | None = None) -> dict[str, ModuleType]:
    """Import and check each connector module in `order` (default ``MODULE_ORDER``)."""
    loaded: dict[str, ModuleType] = {}
    for module_name in MODULE_ORDER if order is None else order:
        full = f"{__name__}.{module_name}"
        try:
            mod = importlib.import_module(full)
        except ModuleNotFoundError as exc:
            if exc.name == full:
                continue
            raise
        _validate(mod, module_name)
        for host in getattr(mod, "HOSTS", ()):
            for other_name, other in loaded.items():
                if host in getattr(other, "HOSTS", ()):
                    raise ConnectorError(f"connector modules {other_name} and {module_name} both claim host {host}")
        loaded[module_name] = mod
    return loaded


_MODULES: dict[str, ModuleType] = {}
CONNECTORS: dict[str, Connector] = {}


def _module(kind: str) -> ModuleType:
    mod = _MODULES.get(kind)
    if mod is None:
        raise ConnectorError(f"unknown connector {kind!r}; known: {', '.join(sorted(CONNECTORS))}")
    return mod


def list_connectors() -> list[dict[str, Any]]:
    # Key names avoid "token" and "secret" so the API's response scrub leaves them alone.
    return [{"name": c.name, "title": c.title, "summary": c.summary, "kinds": list(c.kinds),
             "env_var": c.token_env, "store_provider": c.secret_provider,
             "remote_option": c.name == "github",
             "auth_kind": c.auth_kind, "curated": c.default_allow is not None,
             "pinned_spec": c.spec_file is not None, "needs_include": c.needs_include,
             "needs_url": c.needs_url, "experimental": c.experimental,
             "scope_default": c.scope_default, "oauth": c.oauth is not None,
             "oauth_configured": oauth_configured(c),
             "oauth_loopback": bool(c.oauth is not None and c.oauth.loopback)}
            for c in CONNECTORS.values()]


def connector_module(kind: str) -> ModuleType:
    """The loaded catalog module for one connector id (for its ``identity``)."""
    return _module(kind)


def connector_entry(
    kind: str,
    *,
    name: str | None = None,
    token_env: str | None = None,
    workspace: str | None = None,
    remote: bool = False,
    url: str | None = None,
    all_tools: bool = False,
) -> dict[str, Any]:
    """The ``{sources: [...], allow: [...]}`` mapping a connector expands to.
    `name` is the source name (default: the connector name); `token_env`
    replaces the default credential variable; `workspace` uses the secret ref
    ``<provider>:<workspace>`` instead; `remote` picks GitHub's hosted server;
    `url` is the server URL for a connector that needs one; `all_tools` widens
    a curated connector to every tool it has."""
    connector = CONNECTORS.get(kind)
    if connector is None:
        raise ConnectorError(f"unknown connector {kind!r}; known: {', '.join(sorted(CONNECTORS))}")
    if remote and kind != "github":
        raise ConnectorError("--remote only applies to github; linear is always the hosted server")
    if url and not connector.needs_url:
        raise ConnectorError(f"connector {kind} takes no url")
    if connector.needs_url and not url:
        raise ConnectorError(f"connector {kind} needs a server url (--url)")
    if all_tools and connector.default_allow is None:
        raise ConnectorError(f"connector {kind} has no curated tool list, so all_tools does not apply")
    return _module(kind).expand(name or connector.name, token_env=token_env, workspace=workspace,
                                remote=remote, url=url or None, all_tools=all_tools)


def connector_kinds(entry: Mapping[str, Any]) -> set[str]:
    return {str(s.get("kind")) for s in entry.get("sources") or [] if isinstance(s, Mapping)}


def spec_text(kind: str) -> str | None:
    """The packaged spec text for a connector with a pinned spec, else None."""
    connector = CONNECTORS.get(kind)
    if connector is None:
        raise ConnectorError(f"unknown connector {kind!r}; known: {', '.join(sorted(CONNECTORS))}")
    if not connector.spec_file:
        return None
    return resources.files(__package__).joinpath("specs", connector.spec_file).read_text(encoding="utf-8")


def spec_rel(kind: str, name: str) -> str | None:
    """Where the pinned spec goes inside a layer's ``tools/`` dir, else None."""
    connector = CONNECTORS.get(kind)
    if connector is None:
        raise ConnectorError(f"unknown connector {kind!r}; known: {', '.join(sorted(CONNECTORS))}")
    return f"specs/{name}.json" if connector.spec_file else None


def catalog_minimum(kind: str, tool_name: str, method: str | None) -> str | None:
    """The trusted approval floor the catalog module sets for one built tool.
    An unknown id raises, so a source cannot lose its floor by a typo."""
    return _module(kind).minimum(tool_name, method)


def _hosts_of(url: str) -> set[str]:
    """Every host name the URL could be read as. Two parsers are asked, so a
    URL that one of them reads oddly (a backslash before an @, say) still
    counts as the host the other one sees."""
    hosts: set[str] = set()
    try:
        hosts.add(urlsplit(url).hostname or "")
    except ValueError:
        pass
    try:
        import httpx

        hosts.add(httpx.URL(url).host)
    except Exception:  # noqa: BLE001 -- a URL httpx cannot parse is simply not matched by it
        pass
    return {h.lower().rstrip(".") for h in hosts if h}


def _owns(mod: ModuleType, host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in getattr(mod, "HOSTS", ()))


def catalog_for_url(url: str | None) -> str | None:
    """The id of the connector whose ``HOSTS`` own the URL's host, else None."""
    if not url:
        return None
    hosts = _hosts_of(str(url))
    for kind, mod in _MODULES.items():
        if any(_owns(mod, host) for host in hosts):
            return kind
    return None


def request_floor(url: str | None, method: str | None) -> tuple[str | None, str | None]:
    """``(catalog id, floor)`` for one http request target. The id is None
    when no connector owns the host. The floor is ``always_approve`` unless
    the (method, path) pair is in that connector's ``READS``, in which case
    it is None and the tool keeps the class it was built with."""
    kind = catalog_for_url(url)
    if kind is None:
        return None, None
    verb = (method or "GET").upper()
    try:
        path = urlsplit(str(url)).path or "/"
    except ValueError:
        return kind, "always_approve"
    for read_method, pattern in getattr(_MODULES[kind], "READS", ()):
        if verb == read_method and re.fullmatch(pattern, path):
            return kind, None
    return kind, "always_approve"


_MODULES.update(_load_modules())
CONNECTORS.update({mod_name: mod.ENTRY for mod_name, mod in _MODULES.items()})

__all__ = ["CONNECTORS", "MODULE_ORDER", "Connector", "ConnectorError", "OAuthSpec", "catalog_for_url",
           "catalog_minimum", "connector_entry", "connector_kinds", "connector_module", "list_connectors",
           "oauth_configured", "request_floor", "spec_rel", "spec_text"]
