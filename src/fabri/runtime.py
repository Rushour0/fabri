"""Shared composition helpers for turning a loaded config into the objects
run_agent() needs. Used by both cli.py and tools/agent_runner_tool.py (the
agent-as-tool adapter) so the two entry points build agents identically."""
import os
from collections.abc import Iterator
from pathlib import Path

from fabri.config import DEFAULT_TOOLS_DIR
from fabri.core.agent import DECOMPOSE_TOOL_NAME
from fabri.core.llm import (
    AnthropicLLMBackend,
    BedrockLLMBackend,
    GeminiLLMBackend,
    OpenAILLMBackend,
    Provider,
)
from fabri.memory.store import QdrantMemoryStore
from fabri.tools.agent_tool import make_agent_tool_manifest
from fabri.tools.registry import ToolRegistry


def build_memory_store(mem_cfg: dict):
    """Pick the memory backend by `memory.backend`. "qdrant" (networked,
    multi-process safe) or "sqlite" (in-process, single-file). Both expose
    the same interface so the agent loop is backend-agnostic."""
    backend = (mem_cfg.get("backend") or "qdrant").lower()
    if backend == "qdrant":
        return QdrantMemoryStore(
            url=mem_cfg["qdrant_url"], collection=mem_cfg["collection"]
        )
    if backend == "sqlite":
        # Lazy import so a qdrant-only user doesn't pay sqlite-vec's
        # extension-load cost.
        from fabri.memory.embedded_store import SqliteMemoryStore
        return SqliteMemoryStore(
            path=mem_cfg.get("sqlite_path", ".fabri/memory.db"),
            collection=mem_cfg.get("collection", "fabri"),
        )
    raise ValueError(
        f"unknown memory.backend: {backend!r} (expected 'qdrant' or 'sqlite')"
    )

# Sentinel value for `tools.manifest_dir` — resolves to the framework's
# bundled tools regardless of where fabri is installed.
BUILTIN_TOOLS_TOKENS = {"builtin", "builtin:tools"}


def _resolve_manifest_dir(d: str) -> Path:
    return DEFAULT_TOOLS_DIR if d in BUILTIN_TOOLS_TOKENS else Path(d)


def build_tool_defs(registry: ToolRegistry, decompose_cfg: dict) -> list[dict]:
    defs = [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema or {"type": "object"}}
        for t in registry.list()
    ]
    if decompose_cfg.get("enabled"):
        defs.append(
            {
                "name": DECOMPOSE_TOOL_NAME,
                "description": "Break the current task into concrete sub-questions to research separately.",
                "input_schema": {"type": "object", "properties": {"task": {"type": "string"}}},
            }
        )
    return defs


# Endpoint pinned by the `openrouter` provider sugar. OpenRouter speaks
# OpenAI's chat-completions + tools wire format, so the OpenAI SDK
# (via base_url) is the cleanest client; no separate backend needed.
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Roles that may resolve to a separate backend. "main" is always present;
# the others are None when the user hasn't configured them (the agent loop
# falls back to main for decompose/planner, silences narration for narrator).
ROLES = ("main", "decompose", "planner", "narrator")


def _resolve_role_cfg(config: dict, role: str) -> dict | None:
    """Look up a role's fully-merged backend config from the normalized
    `llm.roles` dict produced by config._normalize_llm_roles. Returns None
    when the role is explicitly disabled (e.g. `llm.narrator: null`) or
    absent. `main` is always present.

    Tolerates a config that bypassed `load_config` (e.g. tests / programmatic
    use that hand-built a flat dict): if `llm.roles` is missing, normalize
    on the fly so the caller doesn't have to know about the new key."""
    llm = config.get("llm") or {}
    roles = llm.get("roles")
    if not roles:
        from fabri.config import _normalize_llm_roles
        roles = _normalize_llm_roles(config)["llm"]["roles"]
    return roles.get(role)


def _active_roles(config: dict) -> Iterator[tuple[str, dict]]:
    """Yield (role, rcfg) for every role that will actually instantiate a
    backend -- i.e. resolves to a config carrying a model. Roles that are
    disabled, absent, or model-less are skipped, mirroring build_role_llm's
    None return so pre-flight checks and the builders agree on which roles
    are live."""
    for role in ROLES:
        rcfg = _resolve_role_cfg(config, role)
        if rcfg is None or not rcfg.get("model"):
            continue
        yield role, rcfg


def _instantiate(rcfg: dict, tool_defs: list[dict]):
    """Single point of provider dispatch -- adding a provider means a new
    Provider enum member (fabri.core.llm) plus one new branch here."""
    raw_provider = rcfg.get("provider")
    provider = Provider.coerce(raw_provider) if raw_provider else Provider.GEMINI
    model = rcfg["model"]
    max_tokens = int(rcfg.get("max_tokens") or 1024)
    api_key_env = rcfg.get("api_key_env") or "GEMINI_API_KEY"
    if provider == Provider.ANTHROPIC:
        return AnthropicLLMBackend(
            model=model,
            tools=tool_defs,
            max_tokens=max_tokens,
            api_key_env=api_key_env,
            cache_messages=bool(rcfg.get("cache_messages", False)),
        )
    if provider in (Provider.OPENAI, Provider.OPENROUTER):
        base_url = rcfg.get("base_url") or (
            _OPENROUTER_BASE_URL if provider == Provider.OPENROUTER else None
        )
        # Only pass reasoning_effort when configured, so the call shape for
        # every existing config is unchanged.
        effort_kwargs = {"reasoning_effort": rcfg["reasoning_effort"]} if rcfg.get("reasoning_effort") else {}
        return OpenAILLMBackend(
            model=model,
            tools=tool_defs,
            max_tokens=max_tokens,
            api_key_env=api_key_env,
            base_url=base_url,
            **effort_kwargs,
        )
    if provider == Provider.GEMINI:
        return GeminiLLMBackend(
            model=model,
            tools=tool_defs,
            max_tokens=max_tokens,
            api_key_env=api_key_env,
        )
    if provider == Provider.BEDROCK:
        # No api_key_env: Bedrock creds come from boto3's default chain (env
        # keys / profile / IAM role / AWS_BEARER_TOKEN_BEDROCK). Region comes
        # from llm.aws_region or AWS_REGION / AWS_DEFAULT_REGION.
        return BedrockLLMBackend(
            model=model,
            tools=tool_defs,
            max_tokens=max_tokens,
            region=rcfg.get("aws_region"),
        )
    # coerce() returned None (unknown string) or a member with no branch above.
    raise ValueError(f"unknown llm provider: {raw_provider!r}")


def build_role_llm(config: dict, role: str, tool_defs: list[dict] | None = None):
    """Build the LLM backend for one role. `tool_defs` is the universal
    Anthropic-shaped tool list and should only be passed for `main`; the
    other roles run with no tools (decompose/planner emit JSON; narrator
    emits one short string). Returns None when the role is disabled or
    unset."""
    rcfg = _resolve_role_cfg(config, role)
    if rcfg is None or not rcfg.get("model"):
        return None
    return _instantiate(rcfg, tool_defs or [])


def build_llm(config: dict, tools_defs: list[dict], *, model_override: str | None = None):
    """Build the orchestrator (main) backend. `model_override`, when given,
    swaps just the model id while keeping the rest of the main role config
    intact -- used by tests / one-off scripts. Returning a real backend
    (not None) is invariant for `main`: the resolver guarantees a config."""
    rcfg = dict(_resolve_role_cfg(config, "main") or {})
    if model_override:
        rcfg["model"] = model_override
    return _instantiate(rcfg, tools_defs)


def build_decompose_llm(config: dict):
    """Returns a separate backend for the decompose meta-step, or None when
    `llm.decompose` is unset (the agent loop then reuses the main backend).
    Honors per-role provider, so a Sonnet orchestrator can run decompose on
    OpenRouter or OpenAI without affecting the main loop."""
    return build_role_llm(config, "decompose")


def find_missing_role_api_keys(config: dict) -> dict[str, list[str]]:
    """Walk every configured role; collect distinct `api_key_env` values for
    roles that will actually instantiate; return {env_var: [roles using it]}
    for the ones that aren't set in the current process environment. Empty
    dict means every required key is present."""
    needed: dict[str, list[str]] = {}
    for role, rcfg in _active_roles(config):
        env = rcfg.get("api_key_env")
        if not env:
            continue
        needed.setdefault(env, []).append(role)
    return {env: roles for env, roles in needed.items() if not os.environ.get(env)}


def find_bedrock_roles_missing_region(config: dict) -> list[str]:
    """Roles configured with `provider: bedrock` that have no region available
    (neither `aws_region` nor AWS_REGION / AWS_DEFAULT_REGION). Bedrock has no
    single api_key_env to pre-flight, but Converse needs a region; this gives
    the CLI a cheap, boto3-free fail-early check mirroring the api-key
    pre-flight the other providers get. Credentials themselves still validate
    lazily on the first call (wrapped in LLMError)."""
    if os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"):
        return []
    missing: list[str] = []
    for role, rcfg in _active_roles(config):
        if (rcfg.get("provider") or "").lower() == Provider.BEDROCK and not rcfg.get("aws_region"):
            missing.append(role)
    return missing


def build_planner_llm(config: dict):
    """Returns a separate backend for the planner meta-step from the
    `llm.planner` role, or None when unset (the agent loop then reuses
    decompose, then main). Distinct from decompose so a config can point the
    planner at a stronger model than the per-subquestion decompose calls."""
    return build_role_llm(config, "planner")


def build_narrator_llm(config: dict):
    """Returns a cheap backend that emits short user-facing status updates
    between tool steps, or None when `llm.narrator` is set to null. Defaults
    to Gemini Flash-Lite via the DEFAULT_CONFIG entry, and inherits any
    per-role provider override (gemini / anthropic / openai / openrouter)."""
    return build_role_llm(config, "narrator")


def build_run_llms(config: dict, tool_defs: list[dict]) -> dict:
    """Build the role backends `run_agent` consumes, keyed by its kwarg names:
    `llm` (main, the only one carrying the tool defs), `decompose_llm`,
    `planner_llm`, `narrator_llm`. The non-main roles return None when their
    `llm.<role>` config is unset; the agent loop falls back accordingly. One
    builder so every entry point (cli run / replay / agent-as-tool) wires the
    same backends — previously each entry point built a different subset and
    `replay` never built the planner backend at all."""
    return {
        "llm": build_llm(config, tool_defs),
        "decompose_llm": build_decompose_llm(config),
        "planner_llm": build_planner_llm(config),
        "narrator_llm": build_narrator_llm(config),
    }


def build_tools(tools_cfg: dict, *, approver=None) -> ToolRegistry:
    """Build the run's ToolRegistry from the ``tools`` block of a config.

    `approver` answers calls whose approval class is ``always_approve``
    (see fabri.tools.approver); with None those calls are refused.
    Config-declared MCP servers get an approval class per tool, and
    ``tools.sources`` entries are built the same way setups build theirs,
    approval floors included (see _register_config_sources).

    Under ``fabri serve`` the run env names a credential broker. Sources
    with a ``secret_ref`` then send through it, and the broker asks a human
    for ``always_approve`` requests itself, so those tools skip the
    registry's approver. With no broker env (``fabri run``, library hosts)
    every tool keeps the normal approver."""
    from fabri.tools.credential_broker_client import BrokerClient

    broker = BrokerClient.from_env()
    # sandbox_root is threaded to each tool spawn via env= (see
    # registry.invoke) rather than os.environ, so a parent registry and a
    # sub-agent registry with a tighter sandbox can coexist.
    sandbox_root = str(Path(os.environ.get("FABRI_SANDBOX_ROOT_OVERRIDE") or tools_cfg["sandbox_root"]).resolve())
    manifest_dirs = tools_cfg["manifest_dir"]
    if isinstance(manifest_dirs, str):
        manifest_dirs = [manifest_dirs]
    registry = ToolRegistry(
        [_resolve_manifest_dir(d) for d in manifest_dirs], sandbox_root=sandbox_root, approver=approver
    )
    # Checked here so a bad value fails at build time, before any spawn.
    from fabri.tools.subagent_envelope import DEFAULT_SUMMARY_MAX_TOKENS, check_summary_cap
    _cap = tools_cfg.get("spawn_summary_max_tokens", DEFAULT_SUMMARY_MAX_TOKENS)
    registry.spawn_summary_max_tokens = check_summary_cap(
        DEFAULT_SUMMARY_MAX_TOKENS if _cap is None else _cap)
    # `decompose` is a synthetic meta-tool the agent loop injects; a
    # user-shipped tool of the same name would shadow it. Refuse loudly.
    if DECOMPOSE_TOOL_NAME in registry.tools:
        raise ValueError(
            f"tool name {DECOMPOSE_TOOL_NAME!r} is reserved for the framework "
            f"meta-tool. Rename your tool (e.g. {DECOMPOSE_TOOL_NAME}_my)."
        )
    for entry in tools_cfg.get("agents", []):
        registry.register(make_agent_tool_manifest(entry))
    # Connection failures are logged but don't kill the build — one bad
    # MCP server shouldn't take down an otherwise-working agent.
    for server_cfg in tools_cfg.get("mcp_servers", []) or []:
        refused = _snapshot_credential_env_refusal(server_cfg, tools_cfg, broker=broker)
        if refused:
            import logging
            logging.getLogger(__name__).warning(
                "MCP server %r refused (skipping): %s", server_cfg.get("name") or "?", refused)
            continue
        try:
            from fabri.tools.approval import mcp_approval
            from fabri.tools.mcp_client import build_mcp_tools
            client, pairs = build_mcp_tools(
                server_cfg,
                approval_for=lambda remote, spec: mcp_approval(
                    remote, str(spec.get("description") or ""), spec.get("annotations")),
            )
            for manifest, handler in pairs:
                registry.register_callable(manifest, handler, owns=client)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "MCP server %r failed to start (skipping): %s",
                server_cfg.get("name") or "?", e,
            )
    if tools_cfg.get("sources"):
        _register_config_sources(registry, tools_cfg["sources"], tools_cfg.get("policy"), broker=broker)
    if tools_cfg["enabled"] is not None:
        registry.tools = {name: m for name, m in registry.tools.items() if name in tools_cfg["enabled"]}
    # With tool retrieval on (or auto), the model may not be shown every tool,
    # so give it find_tools and describe_tool to reach the hidden ones.
    from fabri.orchestrator.retrieval import tool_retrieval_mode

    if tool_retrieval_mode((tools_cfg.get("retrieval") or {}).get("enabled", False)) != "off":
        from fabri.tools.catalog import register_registry_meta_tools

        register_registry_meta_tools(registry)
    return registry


class _CollectedTools(list):
    """Stands in for a registry so register_tool_sources hands back what it
    built, letting build_tools check names and policy before registering."""

    def register_callable(self, manifest, handler, owns=None) -> None:
        self.append((manifest, handler))


def _tool_policy(raw):
    """The ToolPolicy for ``tools.sources``: ``tools.policy`` as a policy file
    path or an inline mapping (or list of names), layered on the strict
    default. With no ``tools.policy`` the strict default applies: openapi
    and http sources only, no DELETE, no python callables."""
    from fabri.tools.policy import ToolPolicy

    if raw is None:
        return ToolPolicy()
    if isinstance(raw, (str, Path)):
        return ToolPolicy.from_file(raw, base=ToolPolicy())
    return ToolPolicy.from_data(raw, base=ToolPolicy())


def _snapshot_credential_env_refusal(server_cfg, tools_cfg: dict, *, broker) -> str | None:
    """Why a ``tools.mcp_servers`` entry may not start in a snapshot run, or
    None when it may.

    A snapshot run is one the service launched (``FABRI_SESSION_ID`` is set)
    with no credential broker, so the service put ``FABRI_CRED_*`` values in
    the run env. There every ``credential_env`` name the server asks for must
    be one the run's own ``tools.sources`` declare and its ``tools.policy``
    grants (the refs the service passed); the broker names never count.
    ``fabri run`` holds only the operator's own env, and under a broker the
    run holds no credential values, so neither is checked here. The reason
    names variable names only, never a value."""
    if broker is not None or not os.environ.get("FABRI_SESSION_ID"):
        return None
    from collections.abc import Mapping

    if not isinstance(server_cfg, Mapping):
        return None
    wanted = server_cfg.get("credential_env")
    if not wanted:
        return None
    if isinstance(wanted, str) or not isinstance(wanted, (list, tuple, set, frozenset)):
        return "credential_env must be a list of variable names"
    from fabri.service.launcher import granted_secret_refs
    from fabri.tools.credential_broker_client import BROKER_ENV_NAMES
    from fabri.tools.credential_store import credential_env_var

    refs = granted_secret_refs({"tools": tools_cfg}, actor=os.environ.get("FABRI_RUN_ACTOR") or None)
    allowed = {credential_env_var(provider, handle) for provider, handle in refs} - BROKER_ENV_NAMES
    bad = sorted(str(name) for name in wanted if not isinstance(name, str) or name not in allowed)
    if bad:
        return ("credential_env names a credential this run's tools.sources do not declare or "
                f"tools.policy does not grant: {', '.join(bad)}")
    return None


def _register_config_sources(registry: ToolRegistry, decls, raw_policy, *, broker=None) -> None:
    """Register ``tools.sources`` declarations (the format setups use in
    tools.yaml) into `registry`.

    Each source is built by fabri.tools.sources, so its approval class and
    the catalog host floors are applied there. The policy's name allow list
    and approval_floor are applied here, as the setups tool set does. Relative
    spec paths resolve against the working directory, like
    ``tools.manifest_dir``. A source that fails to build is logged and
    skipped; a bad ``tools.policy`` raises, since ignoring it would hide
    what the operator asked for. A source may not replace a tool that is
    already registered or the reserved decompose name.

    With `broker` (a live BrokerClient) the builder routes sources that
    declare a ``secret_ref`` through it, and each tool it wired that way is
    marked with ``registry.mark_broker_gated``. A tool is marked only when
    this function knows its declaration sends through the broker (an http,
    openapi or url MCP source with a ref) and the builder tagged its handler,
    so a python callable cannot opt itself out of the approver. Without a
    broker nothing is marked and the builder is called exactly as before."""
    import logging

    from fabri.tools.approval import stricter
    from fabri.tools.sources import register_tool_sources, secret_refs

    log = logging.getLogger(__name__)
    policy = _tool_policy(raw_policy)
    if not isinstance(decls, (list, tuple)):
        raise ValueError("tools.sources must be a list of tool source declarations")
    build_kwargs = {"policy": policy}
    if broker is not None:
        build_kwargs["broker"] = broker
    # One builder call per declaration, so each built tool is paired with the
    # declaration it came from.
    collected: list[tuple] = []
    for decl in decls:
        built = _CollectedTools()
        for error in register_tool_sources(built, [decl], **build_kwargs):
            log.warning("tool source failed to build (skipping): %s", error)
        routed = broker is not None and _routes_through_broker(decl, secret_refs)
        collected.extend((manifest, handler, routed) for manifest, handler in built)
    for manifest, handler, routed in collected:
        aliases = manifest.aliases or ()
        if not policy.name_allowed(manifest.name, aliases):
            continue
        if manifest.name == DECOMPOSE_TOOL_NAME or manifest.name in registry.tools:
            log.warning("tool source tool %r skipped: the name is already taken", manifest.name)
            continue
        floor = policy.floor_for(manifest.name, aliases)
        if floor is not None:
            manifest.approval = stricter(manifest.approval or "write", floor)
        registry.register_callable(manifest, handler)
        if routed and getattr(handler, "approval_gate", None) == "broker":
            registry.mark_broker_gated(manifest.name)


def _routes_through_broker(decl, secret_refs) -> bool:
    """True when the source builder sends this declaration through a broker:
    an http or openapi source, or an MCP source with a url, that names at
    least one ``secret_ref``. Python and stdio MCP sources never do."""
    from collections.abc import Mapping

    if not isinstance(decl, Mapping):
        return False
    kind = decl.get("kind")
    if kind not in ("http", "openapi") and not (kind == "mcp" and decl.get("url")):
        return False
    try:
        return bool(secret_refs(decl))
    except Exception:  # noqa: BLE001 -- a malformed declaration is never marked
        return False
