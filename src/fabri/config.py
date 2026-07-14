import os
from pathlib import Path

import yaml

from fabri.core.agent import DEFAULT_MAX_PARALLEL_SPAWNS
from fabri.core.decompose import DEFAULT_MAX_SUBQUESTIONS
from fabri.core.llm import Provider
from fabri.memory.compress import DEFAULT_MAX_TOKENS
from fabri.memory.pruning import PROMOTION_THRESHOLD_SESSIONS, SIMILARITY_THRESHOLD
from fabri.memory.store import COLLECTION_NAME
from fabri.orchestrator.retrieval import DEFAULT_TOP_K

DEFAULT_TOOLS_DIR = Path(__file__).resolve().parent / "tools" / "examples"

DEFAULT_CONFIG = {
    "agent": {
        "name": "default",
        "max_steps": 10,
        # None = no budget. When set, the run breaks out with
        # Outcome.BUDGET_EXCEEDED before issuing an LLM call whose result
        # would push total COGS past this threshold.
        "max_cost_usd": None,
        # If set, REPLACES the framework's generic "You are an autonomous
        # agent..." boilerplate. `system_prompt_prefix` is prepended to
        # whatever follows.
        "system_prompt": "",
        "system_prompt_prefix": "",
        # Format the model is asked to PRODUCE structured output in
        # (decompose). Native tool-call arguments are always provider JSON
        # regardless. "toon" is opt-in with json fallback.
        "output_format": "json",
        # O1: structured / typed output. When `response_schema` is a JSON
        # Schema (dict), the final answer is parsed as JSON and validated
        # against it; a mismatch re-prompts the model with the validation
        # errors up to `response_retries` times. After that, `error_strategy`
        # decides: "strict" fails the run (Outcome.INVALID_OUTPUT), "warn"
        # returns the unvalidated text as success, "fallback" returns
        # `response_fallback` (or {}) as success. None disables the whole path
        # (today's free-text behaviour, zero extra LLM calls).
        "response_schema": None,
        "response_retries": 1,
        "error_strategy": "strict",
        "response_fallback": None,
        # Planner/executor split. `off` keeps the single-loop behaviour;
        # `auto` runs the planner only on tasks long enough to benefit;
        # `force` always runs it.
        "planner": {
            "enabled": False,
            "mode": "off",
            "max_items": 8,
            "auto_token_threshold": 80,
        },
        # Independent budget for spawned sub-agents. Each non-None field
        # overrides agent.max_steps / agent.max_cost_usd for children only,
        # so a fan-out doesn't let every child loop on the parent's inflated
        # budget. Both None: children inherit the parent's agent.* values.
        "subagent": {
            "max_steps": None,
            "max_cost_usd": None,
        },
        # B8: bounded auto-repair. After a run finishes, if `enabled` and a
        # failure signal is present, fabri re-enters the agent with the failure
        # injected as a fresh instruction and a fresh step budget, up to
        # `max_attempts` re-runs. `verify_command` is an optional host-supplied
        # check (a list argv or a shell string) run after each attempt; a
        # nonzero exit (or stdout JSON `{"ok": false}`) is the failure to
        # repair. When it's null, the run's own failure outcome is the signal.
        # `verify_cwd` is where the verifier runs (default: the process CWD; the
        # verifier is trusted host code, NOT sandboxed). `stop_on_no_progress`
        # aborts early when the error signature is unchanged between attempts so
        # a stuck agent stops instead of burning the whole budget.
        # `repair_prompt` (with a `{errors}` placeholder) overrides the built-in
        # neutral repair instruction. Disabled by default -> zero behaviour
        # change.
        "repair": {
            "enabled": False,
            "max_attempts": 2,
            "verify_command": None,
            "verify_cwd": None,
            "stop_on_no_progress": True,
            "repair_prompt": None,
        },
    },
    "llm": {
        # Default provider is Gemini: lowest cost + generous free tier, so a
        # fresh `fabri run` needs only a GEMINI_API_KEY. Switch to anthropic /
        # openai / openrouter / bedrock per-config or per-role; all SDKs ship by
        # default. `.value` keeps this a plain str (yaml-serializable) while
        # sourcing the name from the Provider enum.
        "provider": Provider.GEMINI.value,
        "model": "gemini-2.5-pro",
        "max_tokens": 1024,
        # No literal env-var name here on purpose: `_normalize_llm_roles`
        # resolves it from whichever `provider` is actually configured (see
        # `_PROVIDER_DEFAULT_API_KEY_ENV`). A hardcoded "GEMINI_API_KEY" here
        # would silently leak into any config that sets `provider`/`model`
        # but not `api_key_env` -- switching provider would then still demand
        # a Gemini key. Set `api_key_env` explicitly only to name something
        # other than the provider's own convention.
        "api_key_env": None,
        # Opt-in extended prompt caching. When true, marks the last
        # message's tail block with cache_control so the history prefix
        # reads from Anthropic's 5-min ephemeral cache on the next turn
        # (~0.1x input bill on the cached prefix). Anthropic-only; a no-op
        # on other providers.
        "cache_messages": False,
        # Per-role overrides. Each entry may be:
        #   - null / absent  -> the role inherits provider/model/api_key_env
        #                       from the parent llm.* defaults
        #   - a string       -> just a model id; provider+api_key_env inherit
        #   - a dict         -> any subset of {provider, model, api_key_env,
        #                        max_tokens, base_url, cache_messages, aws_region}
        # `_normalize_llm_roles` resolves these into a fully-merged dict per
        # role before any downstream code (runtime.build_role_llm, cli
        # pre-flight, etc.) reads them.
        "decompose": None,
        "planner": None,
        # Narrator emits short user-facing status updates between tool steps.
        # No "model" here on purpose: `_normalize_llm_roles` fills in a cheap
        # model for whichever provider `llm.provider` resolves to (see
        # `_CHEAP_NARRATOR_MODEL`), so switching the main provider doesn't
        # leave narration pointed at a model that provider doesn't serve.
        # Set this dict to None to silence narration entirely, or give it an
        # explicit `model`/`provider` to pin narration to something else.
        "narrator": {"max_tokens": 60},
        # Legacy flat keys -- still honored for backward compatibility.
        # `_normalize_llm_roles` lifts them into the corresponding role
        # dict above when the role dict is absent. Prefer the dict form in
        # new configs; these continue to work indefinitely.
        "decompose_model": None,
        "narrator_model": None,
        "narrator_max_tokens": None,
    },
    "tools": {
        "manifest_dir": str(DEFAULT_TOOLS_DIR),
        "enabled": None,
        "sandbox_root": ".",
        "agents": [],
        # How tool results are serialized INTO the model's context. "toon"
        # saves input tokens; the framework encodes this end so there's no
        # model reliability risk. "json" to opt out.
        "result_format": "toon",
        # Cap on how many spawn_subagent calls in one parallel_group run
        # concurrently. Each spawn is a fresh subprocess, so a very wide wave
        # can spike memory enough to OOM the host in a memory-capped container.
        # Members beyond the cap queue and run as slots free up — same result,
        # bounded peak memory. Lower it on tight memory budgets.
        "max_parallel_spawns": DEFAULT_MAX_PARALLEL_SPAWNS,
        "decompose": {"enabled": False, "max_subquestions": DEFAULT_MAX_SUBQUESTIONS},
        # Narrow the system prompt + provider tool list to a task-relevant
        # subset via cosine similarity against each tool's description.
        # `always_include` lists tools the orchestrator prompt assumes exist
        # regardless of how the task is worded.
        "retrieval": {
            "enabled": False,
            "top_k": 6,
            "always_include": ["spawn_subagent", "ask_user", "decompose"],
        },
        # Each entry is connected at agent-build time and its tools are
        # wrapped as fabri tools named `mcp_<server>_<remote_tool>`.
        # Connection failures are logged and skipped, not fatal.
        "mcp_servers": [],
    },
    "memory": {
        # "qdrant" (networked) or "sqlite" (in-process, file-backed, no
        # docker). The two are interchangeable from the agent's perspective.
        "backend": "qdrant",
        "collection": COLLECTION_NAME,
        "qdrant_url": "http://localhost:6333",
        "sqlite_path": ".fabri/memory.db",
        "top_k": DEFAULT_TOP_K,
        "similarity_threshold": SIMILARITY_THRESHOLD,
        "promotion_threshold_sessions": PROMOTION_THRESHOLD_SESSIONS,
        "guideline_max_tokens": DEFAULT_MAX_TOKENS,
        # M1: when true, every run (any outcome) also writes one deterministic
        # whole-run postmortem to memory — task + outcome + retry/cost signal —
        # retrieved by task similarity so a similar future task sees "last time
        # this took N retries; tool X failed K times". Off keeps today's
        # failure/success-only mining (and unchanged entry counts).
        "record_postmortems": False,
        # Retrieval strategy. Default "hybrid" — the offline retrieval eval
        # (python -m fabri.benchmarks.retrieval_eval) measured hybrid recall@5
        # 0.94 vs dense 0.79, and hybrid falls back to dense wherever BM25 is
        # unavailable, so it is never worse. Set "dense" for the pre-v0.9.x
        # vector-only behaviour. See docs/design/memory-observability-plan.md.
        # "dense"      — vector similarity only (pre-v0.9.x default)
        # "sparse"     — BM25 only (SQLite FTS5 / Qdrant client-side BM25)
        # "hybrid"     — RRF fusion of dense + sparse (default)
        # "hybrid+mmr" — hybrid + MMR diversification on final candidate pool
        "retrieval_strategy": "hybrid",
        # Exponential temporal decay: score *= exp(-ln(2)*age_days/half_life).
        # Recent entries get ~1.0; entries half_life_days old get ~0.5.
        "temporal_decay": False,
        "temporal_half_life_days": 30.0,
        # MMR lambda: 0=pure diversity, 1=pure relevance. Only applies when
        # retrieval_strategy is "hybrid+mmr".
        "mmr_lambda": 0.7,
        # RRF fusion constant for "hybrid". The web-scale default is 60, but
        # fabri fuses two short pools where 60 flattens the rank term and lets
        # mere agreement outrank the single best match — the offline eval
        # measured recall@3 0.60 (k=60) -> 0.90 (k=20). Lower = sharper rank
        # discrimination; raise toward 60 only for very large stores.
        "rrf_k": 20,
        # Keyword heuristic domain classifier (code/planning/search/api/generic).
        # When true, entries whose domain matches the query get a 1.15× score boost.
        "domain_routing": False,
        # Importance weight: 0=off. Boosts entries by hit_count + strategic bonus.
        # importance = min(1, hit_count/10 + 0.3 if strategic). Applied as
        # score *= (1 + importance_weight * importance).
        "importance_weight": 0.2,
        # Reserved for future multi-query expansion — no-op when False.
        "query_expansion": False,
        # Optional cross-collection "global lessons" tier: a second Qdrant
        # collection (same `qdrant_url` — the shared Qdrant instance, no new
        # URL field) whose candidates are merged into retrieval alongside the
        # primary collection's. None (default) = today's single-collection
        # behaviour, byte-identical. Only consulted by the retrieval pipeline
        # (fabri.orchestrator.retrieval); build_memory_store's single-store
        # return contract used by cli/mcp/ingest tooling is unaffected.
        "global_collection": None,
        # Memory eviction: cap the total number of stored entries so the
        # collection doesn't grow unboundedly. When set, after each ingest
        # the N lowest-scoring entries are deleted to bring the count back
        # to max_entries. Score = hit_count * temporal_decay(age) — old,
        # rarely-retrieved entries are evicted first; frequently-used and
        # recent entries survive longest. Strategic entries are protected
        # until all non-strategic entries are gone.
        # None = no limit (default, current behaviour).
        "max_entries": None,
        # Half-life for the eviction age-weight (same formula as
        # temporal_decay in retrieval). An entry that is eviction_half_life_days
        # old has its hit_count weighted at 0.5 for the eviction ranking.
        # Reuses temporal_half_life_days when not set separately.
        "eviction_half_life_days": None,
        # What to do with entries selected for eviction:
        #   "delete"    — plain delete (default, zero LLM cost)
        #   "summarize" — compress groups of evicted entries into one shorter
        #                 guideline before deleting (MemGPT-style recursive
        #                 summarization). Uses the run's compress_llm so cost
        #                 is billed to the same post-run usage bucket.
        "eviction_strategy": "delete",
        # Staleness report thresholds (fabri.memory.staleness / `fabri memory
        # stale`): a guideline is "stale" when hit_count <= stale_max_hit_count
        # AND age_days >= stale_min_age_days. Pure read-side report -- these
        # two knobs don't feed retrieval, scoring, or eviction at all; they
        # only gate what `find_stale_guidelines` returns. Defaults mirror
        # find_stale_guidelines' own defaults so an omitted config and an
        # omitted call-site kwarg agree.
        "stale_max_hit_count": 2,
        "stale_min_age_days": 7.0,
    },
    # The Improver (fabri.readlogs / `fabri ingest`): feed EXTERNAL logs into the
    # same self-improving memory loop. All keys default so omission = today's
    # behaviour (the loop only ever mined fabri's own runs).
    "ingest": {
        "default_adapter": "auto",
        # Deterministic-first: the default ingest path is LLM-free ($0). Flip to
        # true (or pass --synthesize) to compress mined signals into richer
        # guidelines at token cost.
        "synthesize": False,
        # Honor third-party adapters registered via the `fabri.adapters`
        # setuptools entry-point group. Set false on a locked-down host to only
        # use in-repo/config-declared adapters.
        "load_plugins": True,
        # External logs default to writing the whole-run postmortem signal.
        "record_postmortems": True,
        # Declarative adapters addressable by name with no Python. Each entry:
        #   {name, kind: configmap|tool, mapping|manifest: ...}. See
        #   fabri/ingest/adapters/configmap.py for the mapping keys.
        "adapters": [],
    },
    # X1: external trace export. Fabri's JSONL spine stays the source of truth;
    # this exports it to any OpenTelemetry OTLP backend (Langfuse, Honeycomb,
    # Datadog, Tempo, Jaeger, …). OFF unless `otlp_endpoint` is set — when unset,
    # `fabri run` behaves byte-identically. Needs the optional extra:
    # `pip install 'fabri[otel]'`. Env overrides: FABRI_OTLP_ENDPOINT,
    # FABRI_OTLP_HEADERS ("k=v,k2=v2"), FABRI_OTLP_PROTOCOL, FABRI_OTLP_INSECURE.
    # See docs/observability.md for the Langfuse recipe.
    "observability": {
        "otlp_endpoint": None,        # e.g. "https://cloud.langfuse.com/api/public/otel/v1/traces"
        "otlp_protocol": "http",      # "http" (OTLP/HTTP protobuf, default) or "grpc"
        "otlp_headers": {},           # e.g. {"Authorization": "Basic <base64 pk:sk>"}
        "otlp_insecure": False,       # allow plaintext (gRPC only)
        "service_name": "fabri",
    },
}


class ConfigError(ValueError):
    """Raised when an agent.yaml is missing, malformed, or overrides a section
    with the wrong shape. cli.py catches this and prints a clean stderr
    message + exit 1, rather than letting the raw yaml/KeyError traceback out."""


def _deep_merge(base: dict, override: dict, *, path: str = "") -> dict:
    merged = dict(base)
    for key, value in override.items():
        here = f"{path}.{key}" if path else key
        base_val = merged.get(key)
        if isinstance(base_val, dict):
            # Refuse a scalar overriding a dict — silently dropping the
            # subtree surfaces as an opaque KeyError several layers deeper.
            if not isinstance(value, dict):
                raise ConfigError(
                    f"config key {here!r} must be a mapping (got {type(value).__name__}); "
                    f"this overrides a default that is itself a mapping."
                )
            merged[key] = _deep_merge(base_val, value, path=here)
        else:
            merged[key] = value
    return merged


# Roles whose backend is resolved from llm.<role>; "main" maps to the parent
# llm.* keys themselves rather than a nested entry.
_LLM_ROLES = ("decompose", "planner", "narrator")

# Per-role default api_key_env when the role's provider differs from the
# parent llm.provider. Lets a user write `narrator: {provider: openai}`
# without also having to spell out `api_key_env: OPENAI_API_KEY`. Keyed by the
# Provider enum; StrEnum members look up by plain string too. Bedrock is
# intentionally absent -- it has no api_key_env (creds come from the AWS chain),
# so resolution yields None and the pre-flight skips it.
_PROVIDER_DEFAULT_API_KEY_ENV = {
    Provider.ANTHROPIC: "ANTHROPIC_API_KEY",
    Provider.OPENAI: "OPENAI_API_KEY",
    Provider.OPENROUTER: "OPENROUTER_API_KEY",
    Provider.GEMINI: "GEMINI_API_KEY",
}

# Cheap/fast model per provider, used as the narrator role's default `model`
# when neither the user nor DEFAULT_CONFIG names one explicitly (see
# `_normalize_llm_roles`). Keyed so narration always rides the SAME provider
# the user already configured for `main` -- no second API key required just
# to get status updates. Bedrock is absent: no cheap-tier convention to pick
# automatically, so narration falls back to the main model on Bedrock.
_CHEAP_NARRATOR_MODEL = {
    Provider.GEMINI: "gemini-2.5-flash-lite",
    Provider.ANTHROPIC: "claude-haiku-4-5",
    Provider.OPENAI: "gpt-4o-mini",
    Provider.OPENROUTER: "google/gemini-2.5-flash-lite",
}


def _normalize_llm_roles(cfg: dict) -> dict:
    """Resolve `llm.<role>` and legacy flat keys into a single normalized
    `llm.roles` dict shaped {decompose|planner|narrator: <full role cfg>|None}.

    A role's full config is `{provider, model, api_key_env, max_tokens,
    base_url, cache_messages, aws_region}`. Missing keys inherit from the parent `llm.*`
    defaults. A role entry that is None means the role is disabled (no
    backend built; the agent loop falls back to main for decompose/planner,
    and silences narration for narrator).

    Legacy flat keys (`decompose_model`, `narrator_model`,
    `narrator_max_tokens`) are lifted ONLY when the corresponding role dict
    is absent, so a user who has both the legacy and new shape gets the new
    shape (clean incremental migration).
    """
    llm = dict(cfg.get("llm") or {})
    parent_provider = (llm.get("provider") or Provider.GEMINI).lower()
    parent_defaults = {
        "provider": parent_provider,
        "model": llm.get("model"),
        # Bedrock has no api_key_env (creds come from the AWS chain), so force it
        # None even if DEFAULT_CONFIG's gemini api_key_env leaked in via
        # deep-merge -- otherwise the pre-flight would wrongly demand that key.
        "api_key_env": None if parent_provider == Provider.BEDROCK
        else (llm.get("api_key_env") or _PROVIDER_DEFAULT_API_KEY_ENV.get(parent_provider)),
        "max_tokens": llm.get("max_tokens"),
        "cache_messages": bool(llm.get("cache_messages", False)),
        "base_url": llm.get("base_url"),
        # AWS Bedrock only: region for the bedrock-runtime client. None for
        # other providers; falls back to AWS_REGION / AWS_DEFAULT_REGION at
        # client-build time.
        "aws_region": llm.get("aws_region"),
    }

    # Lift legacy flat keys into a synthetic role override, only when the
    # matching role dict isn't already set. Lift wins nothing if both exist.
    legacy_map = {
        "decompose": llm.get("decompose_model"),
        "narrator": llm.get("narrator_model"),
    }
    for role, legacy_model in legacy_map.items():
        if legacy_model and llm.get(role) is None:
            llm[role] = {"model": legacy_model}
    if (llm.get("narrator") is not None
            and isinstance(llm.get("narrator"), dict)
            and llm["narrator"].get("max_tokens") is None
            and llm.get("narrator_max_tokens") is not None):
        llm["narrator"] = {**llm["narrator"], "max_tokens": llm["narrator_max_tokens"]}

    roles: dict[str, dict | None] = {}
    for role in _LLM_ROLES:
        raw = llm.get(role)
        if raw is None:
            roles[role] = None
            continue
        if isinstance(raw, str):
            raw = {"model": raw}
        if not isinstance(raw, dict):
            raise ConfigError(
                f"config key llm.{role} must be a mapping, a model-id string, "
                f"or null (got {type(raw).__name__})."
            )
        provider = (raw.get("provider") or parent_defaults["provider"]).lower()
        # When the role overrides the provider but not the api_key_env, pick
        # the provider's conventional env var instead of leaking the parent's.
        # Bedrock never uses one (AWS chain), so force None.
        if provider == Provider.BEDROCK:
            api_key_env = None
        else:
            api_key_env = raw.get("api_key_env")
            if api_key_env is None:
                api_key_env = (
                    parent_defaults["api_key_env"]
                    if provider == parent_defaults["provider"]
                    else _PROVIDER_DEFAULT_API_KEY_ENV.get(provider)
                )
        # Narrator's cheap-model default is provider-aware: pick the cheap
        # model for whichever provider this role resolved to (normally the
        # same as `main`), rather than falling through to `parent_defaults`'s
        # (possibly expensive, possibly wrong-provider) main model.
        if role == "narrator" and raw.get("model") is None:
            model = _CHEAP_NARRATOR_MODEL.get(provider) or parent_defaults["model"]
        else:
            model = raw.get("model") or parent_defaults["model"]
        roles[role] = {
            "provider": provider,
            "model": model,
            "api_key_env": api_key_env,
            "max_tokens": raw.get("max_tokens") or parent_defaults["max_tokens"],
            "cache_messages": bool(raw.get("cache_messages", parent_defaults["cache_messages"])),
            "base_url": raw.get("base_url") or parent_defaults["base_url"],
            "aws_region": raw.get("aws_region") or parent_defaults["aws_region"],
        }

    # `main` is always present; it's the parent llm.* defaults verbatim.
    roles["main"] = parent_defaults
    llm["roles"] = roles
    return {**cfg, "llm": llm}


def _apply_env_overrides(cfg: dict) -> dict:
    """Env-var overrides applied after load so a container host can inject
    addresses/endpoints across the subprocess boundary without rewriting each
    on-disk yaml. Never mutates the shared DEFAULT_CONFIG; each override acts
    only when its env var is set.

    - ``QDRANT_URL`` -> ``memory.qdrant_url``
    - ``FABRI_OTLP_ENDPOINT`` -> ``observability.otlp_endpoint``
    - ``FABRI_OTLP_PROTOCOL`` -> ``observability.otlp_protocol`` ("http"|"grpc")
    - ``FABRI_OTLP_INSECURE`` -> ``observability.otlp_insecure`` (1/true/yes/on)
    - ``FABRI_OTLP_HEADERS`` -> ``observability.otlp_headers`` ("k=v,k2=v2")
    """
    out = cfg

    url = os.environ.get("QDRANT_URL")
    if url:
        mem = dict(out.get("memory") or {})
        if mem.get("qdrant_url") != url:
            mem["qdrant_url"] = url
            out = {**out, "memory": mem}

    obs_over: dict = {}
    endpoint = os.environ.get("FABRI_OTLP_ENDPOINT")
    if endpoint:
        obs_over["otlp_endpoint"] = endpoint
    protocol = os.environ.get("FABRI_OTLP_PROTOCOL")
    if protocol:
        obs_over["otlp_protocol"] = protocol
    insecure = os.environ.get("FABRI_OTLP_INSECURE")
    if insecure is not None:
        obs_over["otlp_insecure"] = insecure.strip().lower() in ("1", "true", "yes", "on")
    headers = os.environ.get("FABRI_OTLP_HEADERS")
    if headers:
        parsed = {
            k.strip(): v.strip()
            for pair in headers.split(",")
            if "=" in pair
            for k, v in [pair.split("=", 1)]
        }
        if parsed:
            obs_over["otlp_headers"] = parsed
    if obs_over:
        obs = {**(out.get("observability") or {}), **obs_over}
        out = {**out, "observability": obs}

    return out


def load_config(path: str | None) -> dict:
    """Load an agent.yaml config, merged on top of DEFAULT_CONFIG so omitted
    fields fall back to today's hardcoded behavior unchanged. `path=None`
    returns the framework defaults as-is -- the same shape a consuming
    project's own agent.yaml would produce, so callers don't special-case it.
    A `QDRANT_URL` env var, if set, overrides `memory.qdrant_url` (see
    `_apply_env_overrides`). Raises ConfigError on missing file, malformed YAML,
    or a shape mismatch."""
    if path is None:
        return _apply_env_overrides(_normalize_llm_roles(DEFAULT_CONFIG))
    try:
        with open(path) as f:
            user_config = yaml.safe_load(f) or {}
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {path}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"malformed YAML in {path}: {e}") from e
    if not isinstance(user_config, dict):
        raise ConfigError(
            f"top-level of {path} must be a mapping (got {type(user_config).__name__})."
        )
    return _apply_env_overrides(_normalize_llm_roles(_deep_merge(DEFAULT_CONFIG, user_config)))
