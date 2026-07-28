import argparse
import importlib.metadata
import importlib.resources
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import uuid

from fabri.admin import AdminAuthError, describe_config, render_dashboard, require_admin
from fabri.agency_registry import resolve_source
from fabri.agency_scaffold import _slug, agency_next_steps, scaffold_agency, write_template
from fabri.agency_templates import TEMPLATES as AGENCY_TEMPLATES
from fabri.config import ConfigError, load_config
from fabri.company import company_next_steps, company_org, compile_company
from fabri.core.agent import run_agent
from fabri.core.llm import LLMUsage
from fabri.core.logging_setup import configure_logging
from fabri.core.outcome import Outcome
from fabri.core.run_config import AgentRunConfig
from fabri.events import EventType
from fabri.memory.action_mining import (
    action_candidate_text,
    build_truncation_action_candidate,
    observed_max_token_retries,
)
from fabri.memory.pruning import ingest_guideline
from fabri.memory.recurrence import fingerprint
from fabri.orchestrator.action_detection import detect_proposed_actions, resolve_action_scope
from fabri.orchestrator.action_execution import apply_safe_memory_actions
from fabri.orchestrator.pipeline import process_trace
from fabri.orchestrator.traces import log_event
from fabri.pricing import cost_for
from fabri.runtime import (
    build_llm,
    build_memory_store,
    build_run_llms,
    build_tool_defs,
    build_tools,
)
from fabri.repo import detect_provider, get_provider
from fabri.scaffold import SCAFFOLD_TEMPLATES, next_steps, scaffold
from fabri.tool_scaffold import SUPPORTED_LANGUAGES, scaffold_tool


logger = logging.getLogger("fabri")


def cmd_init(args: argparse.Namespace) -> None:
    template = getattr(args, "template", "default") or "default"
    try:
        result = scaffold(args.dir, force=args.force, template=template)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    where = "current directory" if args.dir in (".", "") else args.dir
    if result["created"]:
        print(f"Scaffolded a {template!r} fabri project in {where}:")
        for rel in result["created"]:
            print(f"  + {rel}")
    if result["skipped"]:
        print("\nLeft existing files untouched (pass --force to overwrite):")
        for rel in result["skipped"]:
            print(f"  . {rel}")
    print("\n" + next_steps(args.dir, template=template))


def _copy_resource_tree(source: importlib.resources.abc.Traversable, dest: Path) -> None:
    """Copy an importlib resource tree without assuming it is a real path."""
    dest.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        target = dest / child.name
        if child.is_dir():
            _copy_resource_tree(child, target)
        else:
            with child.open("rb") as source_file, target.open("wb") as dest_file:
                shutil.copyfileobj(source_file, dest_file)


def cmd_examples(args: argparse.Namespace) -> None:
    """List bundled agencies or copy every example into a target directory."""
    bundled = importlib.resources.files("fabri.examples.agencies")
    agencies = sorted(
        (
            entry
            for entry in bundled.iterdir()
            if entry.is_dir() and not entry.name.startswith((".", "__"))
        ),
        key=lambda entry: entry.name,
    )

    if args.copy is None:
        if not agencies:
            print("No bundled example agencies found.")
            return
        for agency in agencies:
            print(agency.name)
        return

    destination = Path(args.copy)
    conflicts = [destination / agency.name for agency in agencies
                 if (destination / agency.name).exists()]
    if conflicts:
        for conflict in conflicts:
            print(f"Refusing to overwrite existing example: {conflict}", file=sys.stderr)
        sys.exit(1)

    destination.mkdir(parents=True, exist_ok=True)
    for agency in agencies:
        target = destination / agency.name
        _copy_resource_tree(agency, target)
        print(f"Copied {agency.name} to {target}")


def _require_api_key(api_key_env: str) -> None:
    if not os.environ.get(api_key_env):
        print(
            f"{api_key_env} is not set. Export it before running the live agent, "
            f"e.g.: export {api_key_env}=<your-api-key>",
            file=sys.stderr,
        )
        sys.exit(1)


def _require_role_api_keys(config: dict) -> None:
    """Pre-flight every distinct api_key_env across all configured roles
    (main + decompose + planner + narrator), plus a boto3-free region check for
    any bedrock role (Bedrock has no api_key_env -- creds come from the AWS
    chain -- but Converse still needs a region). Reports ALL problems in a
    single error so a multi-provider config doesn't fail halfway through setup.
    CLI wrapper: prints to stderr + exits 1 rather than raising."""
    from fabri.runtime import (
        find_bedrock_roles_missing_region,
        find_missing_role_api_keys,
    )

    lines = [
        f"  {env} (used by: {', '.join(roles)})"
        for env, roles in find_missing_role_api_keys(config).items()
    ]
    bedrock_no_region = find_bedrock_roles_missing_region(config)
    if bedrock_no_region:
        lines.append(
            f"  AWS region for bedrock role(s): {', '.join(bedrock_no_region)} "
            f"(set llm.aws_region or AWS_REGION)"
        )
    if not lines:
        return
    print(
        "Missing required LLM credentials/configuration:\n"
        + "\n".join(lines)
        + "\nExport/set each before running the live agent.",
        file=sys.stderr,
    )
    sys.exit(1)


def _open_store(mem_cfg: dict):
    """Open the configured memory backend with a user-friendly error message
    if it's unreachable, instead of leaking a raw qdrant/grpc/sqlite traceback.

    Picks Qdrant or sqlite-vec based on `memory.backend` — see
    `runtime.build_memory_store`.
    """
    backend = (mem_cfg.get("backend") or "qdrant").lower()
    try:
        return build_memory_store(mem_cfg)
    except Exception as e:
        if backend == "qdrant":
            print(
                f"Could not reach Qdrant at {mem_cfg.get('qdrant_url')}: {e}\n"
                "Start it with: docker compose up -d  (or `docker run -p 6333:6333 qdrant/qdrant`).\n"
                "Or switch to the embedded backend: set memory.backend: sqlite in agent.yaml "
                "(pip install 'fabri[sqlite]').",
                file=sys.stderr,
            )
        else:
            print(f"Could not open memory backend {backend!r}: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_tools(args: argparse.Namespace) -> None:
    """B3: list the tools the agent would have available — builtins plus the
    dirs in `tools.manifest_dir` (or the --manifest-dir overrides) — optionally
    filtered by --search. Resolves tools the same way a run does, so "what do I
    have?" matches "what would run"."""
    from fabri.builder import filter_tools, render_tools_listing

    config = load_config(args.config)
    tools_cfg = dict(config["tools"])
    if args.manifest_dir:
        tools_cfg["manifest_dir"] = list(args.manifest_dir)
    registry = build_tools(tools_cfg)
    pairs = filter_tools(registry, args.search)
    print(render_tools_listing(pairs, search=args.search))


def _print_dry_run(config: dict, task: str) -> None:
    """B3: `fabri run --dry-run` — print the resolved config summary + the tool
    definitions that WOULD be sent to the model, then return without opening the
    memory store or constructing any LLM backend. Needs no API key: the whole
    point is to inspect a run before spending on it."""
    from fabri.builder import build_dry_run_plan, render_dry_run_plan

    tools = build_tools(config["tools"])
    tool_defs = build_tool_defs(tools, config["tools"]["decompose"])
    plan = build_dry_run_plan(config, tool_defs)
    print(render_dry_run_plan(plan, task=task))


def cmd_run(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    # B3: --dry-run inspects the plan with no network — resolve and print before
    # any API-key check, store open, or LLM call.
    if getattr(args, "dry_run", False):
        _print_dry_run(config, args.task)
        return
    _require_role_api_keys(config)
    session_id = args.session_id or str(uuid.uuid4())
    configure_logging(session_id, verbose=args.verbose)
    if getattr(args, "ask_user_socket", None):
        os.environ["FABRI_ASK_USER_SOCKET"] = args.ask_user_socket

    mem_cfg = config["memory"]
    store = _open_store(mem_cfg)

    tools_cfg = config["tools"]
    applied_memory_actions: list[dict[str, object]] = []
    if mem_cfg.get("memory_action_enabled", False):
        try:
            company, agency = resolve_action_scope(mem_cfg)
            proposals = detect_proposed_actions(
                store,
                tools_cfg.get("agents") or [],
                args.task,
                company=company or "",
                agency=agency or "",
                top_n=8,
            )
            if proposals and mem_cfg.get("memory_action_apply_enabled", False):
                applied_memory_actions = [
                    action.to_dict()
                    for action in apply_safe_memory_actions(
                        proposals,
                        tools_cfg.get("agents") or [],
                    )
                ]
                logger.info(
                    "applied %d safe ActionMemory recovery change(s)",
                    len(applied_memory_actions),
                )
                log_event(
                    session_id,
                    {
                        "type": "memory_action_applied",
                        "actions": applied_memory_actions,
                    },
                )
            elif proposals:
                logger.info("shadow ActionMemory proposals detected: %d", len(proposals))
        except Exception:
            logger.warning("ActionMemory preparation failed closed", exc_info=True)

    tools = build_tools(tools_cfg)

    decompose_cfg = tools_cfg["decompose"]
    llms = build_run_llms(config, build_tool_defs(tools, decompose_cfg))
    run_cfg = AgentRunConfig.from_config(config)

    result = run_agent(
        args.task,
        llms["llm"],
        tools,
        store,
        session_id=session_id,
        decompose_llm=llms["decompose_llm"],
        planner_llm=llms["planner_llm"],
        narrator_llm=llms["narrator_llm"],
        **run_cfg.as_kwargs(),
    )
    if applied_memory_actions:
        result["memory_actions_applied"] = applied_memory_actions
    print(json.dumps(result, indent=2))
    # Surface a non-success outcome via exit code — host services dispatch
    # on `fabri run`'s returncode. Without this, a rate-limit failure exits
    # 0 and downstream ledgers mark the run succeeded.
    # BUDGET_EXCEEDED is a deliberate halt, not a SUCCESS — it flows into
    # the failure branch below via the outcome check.
    success_outcomes = {Outcome.SUCCESS.value, Outcome.SUCCESS_WITH_RECOVERY.value}
    run_failed = not result.get("success") or result.get("outcome") not in success_outcomes

    # Accumulate memory-compression LLM usage so the host can roll it onto
    # the run's totals — these calls happen AFTER the run's `usage` event
    # is emitted, so they need to ride out as a follow-up POST_RUN_USAGE
    # event the host adds rather than replaces.
    post_run_usage = LLMUsage()
    post_run_by_model: dict[str, LLMUsage] = {}

    def _accumulate_post_run(u: LLMUsage) -> None:
        post_run_usage.input_tokens += u.input_tokens
        post_run_usage.output_tokens += u.output_tokens
        post_run_usage.cache_creation_input_tokens += u.cache_creation_input_tokens
        post_run_usage.cache_read_input_tokens += u.cache_read_input_tokens
        post_run_usage.provider_transient_retries += u.provider_transient_retries
        post_run_usage.max_token_retries += u.max_token_retries
        bucket = post_run_by_model.setdefault(u.model or "", LLMUsage(model=u.model))
        bucket.input_tokens += u.input_tokens
        bucket.output_tokens += u.output_tokens
        bucket.cache_creation_input_tokens += u.cache_creation_input_tokens
        bucket.cache_read_input_tokens += u.cache_read_input_tokens
        bucket.provider_transient_retries += u.provider_transient_retries
        bucket.max_token_retries += u.max_token_retries

    _eviction_half_life = (
        mem_cfg.get("eviction_half_life_days")
        or mem_cfg.get("temporal_half_life_days", 30.0)
    )
    entries = []
    if mem_cfg.get("mining_enabled", True):
        compress_llm = build_llm(config, [])
        entries = process_trace(
            session_id,
            store,
            compress_llm,
            guideline_max_tokens=mem_cfg["guideline_max_tokens"],
            similarity_threshold=mem_cfg["similarity_threshold"],
            promotion_threshold_sessions=mem_cfg["promotion_threshold_sessions"],
            record_postmortem=mem_cfg.get("record_postmortems", False),
            success_pattern_requires_evidence=mem_cfg.get("success_pattern_requires_evidence", False),
            on_usage=_accumulate_post_run,
            max_entries=mem_cfg.get("max_entries"),
            eviction_half_life_days=float(_eviction_half_life),
            eviction_strategy=mem_cfg.get("eviction_strategy", "delete"),
            producer_agent_id=config.get("agent", {}).get("name"),
            memory_scope=mem_cfg.get("scope", "agent"),
            config=config,
        )
    if mem_cfg.get("mining_enabled", True):
        try:
            candidate = build_truncation_action_candidate(
                config,
                observed_max_token_retries(
                    result,
                    post_run_usage.max_token_retries,
                ),
                str(result.get("outcome", "unknown")),
                args.task,
            )
            if candidate is not None:
                evidence = candidate.get("evidence")
                if isinstance(evidence, dict):
                    evidence["source_session_ids"] = [session_id]
                signature = candidate["problem_signature"]
                ingest_guideline(
                    store,
                    action_candidate_text(candidate),
                    session_id=session_id,
                    kind="success_pattern",
                    tier="quarantine",
                    resolution=candidate,
                    dedup_key=fingerprint(signature),
                    similarity_threshold=1.01,
                    producer_agent_id=config.get("agent", {}).get("name"),
                    scope=mem_cfg.get("scope", "agent"),
                )
        except Exception:
            logger.warning("truncation ActionMemory mining failed", exc_info=True)
    if (post_run_usage.input_tokens or post_run_usage.output_tokens
            or post_run_usage.cache_creation_input_tokens
            or post_run_usage.cache_read_input_tokens):
        post_cost_by_model: dict[str, float] = {}
        post_cost_total = 0.0
        for model_id, bucket in post_run_by_model.items():
            c = cost_for(bucket)
            if c is not None:
                post_cost_by_model[model_id or "unknown"] = c
                post_cost_total += c
        log_event(session_id, {
            "type": EventType.POST_RUN_USAGE.value,
            "source": "memory_compression",
            "input_tokens": post_run_usage.input_tokens,
            "output_tokens": post_run_usage.output_tokens,
            "cache_creation_input_tokens": post_run_usage.cache_creation_input_tokens,
            "cache_read_input_tokens": post_run_usage.cache_read_input_tokens,
            "provider_transient_retries": post_run_usage.provider_transient_retries,
            "max_token_retries": post_run_usage.max_token_retries,
            "cost_usd": round(post_cost_total, 6),
            "cost_by_model": post_cost_by_model,
        })
    if entries:
        # Human-informational only -> stderr. stdout must stay the machine-readable
        # result envelope (printed above): the service parses stdout as JSON, and a
        # trailing human line here would corrupt it (agent stdout was not JSON).
        print(f"\nSynthesized {len(entries)} guideline(s) from this run:", file=sys.stderr)
        for e in entries:
            print(f"  [{e.kind}] {e.text}", file=sys.stderr)

    # X1: best-effort OTLP export of the finished trace (no-op unless
    # observability.otlp_endpoint is set). Fired here, after the POST_RUN_USAGE
    # event, so the exported spans include memory-compression cost too.
    _maybe_export_trace(session_id, config)

    if run_failed:
        sys.exit(1)


def cmd_ingest_traces(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    _require_role_api_keys(config)
    configure_logging(args.session_id, verbose=args.verbose)
    mem_cfg = config["memory"]
    store = _open_store(mem_cfg)
    _eviction_half_life_ingest = (
        mem_cfg.get("eviction_half_life_days")
        or mem_cfg.get("temporal_half_life_days", 30.0)
    )
    entries = []
    if mem_cfg.get("mining_enabled", True):
        llm = build_llm(config, [])
        entries = process_trace(
            args.session_id,
            store,
            llm,
            guideline_max_tokens=mem_cfg["guideline_max_tokens"],
            similarity_threshold=mem_cfg["similarity_threshold"],
            promotion_threshold_sessions=mem_cfg["promotion_threshold_sessions"],
            record_postmortem=mem_cfg.get("record_postmortems", False),
            success_pattern_requires_evidence=mem_cfg.get("success_pattern_requires_evidence", False),
            max_entries=mem_cfg.get("max_entries"),
            eviction_half_life_days=float(_eviction_half_life_ingest),
            eviction_strategy=mem_cfg.get("eviction_strategy", "delete"),
            producer_agent_id=config.get("agent", {}).get("name"),
            memory_scope=mem_cfg.get("scope", "agent"),
        )
    print(json.dumps([e.to_payload() for e in entries], indent=2))


def _parse_options(pairs: list[str] | None) -> dict:
    """Turn repeated ``--option KEY=VALUE`` into a dict passed to the adapter.
    Dotted keys are kept flat (the adapter interprets them)."""
    opts: dict = {}
    for item in pairs or []:
        if "=" not in item:
            print(f"--option must be KEY=VALUE (got {item!r})", file=sys.stderr)
            sys.exit(1)
        k, v = item.split("=", 1)
        opts[k] = v
    return opts


def cmd_ingest(args: argparse.Namespace) -> None:
    """The Improver CLI: normalize an external log via an adapter and mine it
    into the same memory the agent retrieves from. Thin wrapper over
    ``fabri.readlogs`` (SDK-first — the CLI adds nothing the SDK can't do)."""
    from fabri.ingest import list_adapters, readlogs

    config = load_config(args.config)
    if getattr(args, "list_adapters", False):
        for name in list_adapters():
            print(name)
        return
    if not args.source:
        print("fabri ingest: SOURCE is required (a file, directory, or '-' for stdin)", file=sys.stderr)
        sys.exit(1)
    # LLM synthesis spends tokens; pre-flight the same api keys `run` needs.
    if args.synthesize:
        _require_role_api_keys(config)
    configure_logging(args.session_id or "ingest", verbose=args.verbose)
    store = _open_store(config["memory"])
    source = sys.stdin if args.source == "-" else args.source
    summary = readlogs(
        source,
        adapter=args.adapter,
        config=config,
        store=store,
        synthesize=args.synthesize,
        adapter_options=_parse_options(args.option),
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(summary.to_dict(), indent=2))
    else:
        d = summary.to_dict()
        print(
            f"ingested {d['sessions']} session(s), {d['events']} event(s) via '{d['adapter']}' "
            f"-> {d['entries']} memory entr{'y' if d['entries'] == 1 else 'ies'} "
            f"{d['by_kind']}; skipped {d['skipped_lines']} line(s); "
            f"cost ${d['llm_cost_usd']:.4f}"
        )


def cmd_inspect_memory(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    store = _open_store(config["memory"])
    print(f"tactical: {store.count(kind='tactical')}")
    print(f"strategic: {store.count(kind='strategic')}")
    if args.query:
        for entry, score in store.query(args.query, top_k=args.top_k):
            print(f"  [{entry.kind}] ({score:.2f}) {entry.text}")


def cmd_memory_show(args: argparse.Namespace) -> None:
    """G2: list guidelines in the store, filterable by kind, with --markdown
    output suitable for pasting into a deck/X/blog. By default both tactical
    and strategic are shown."""
    config = load_config(args.config)
    store = _open_store(config["memory"])
    kinds: list[str | None]
    if args.strategic:
        kinds = ["strategic"]
    elif args.tactical:
        kinds = ["tactical"]
    else:
        kinds = ["strategic", "tactical"]

    counts = {k: store.count(kind=k) for k in ("strategic", "tactical")}
    total = counts["strategic"] + counts["tactical"]

    if args.markdown:
        print(f"# fabri memory ({total} guidelines: "
              f"{counts['strategic']} strategic + {counts['tactical']} tactical)\n")
    else:
        print(f"{total} guidelines total "
              f"({counts['strategic']} strategic + {counts['tactical']} tactical)\n")

    for kind in kinds:
        entries = store.iterate(kind=kind, limit=args.limit)
        if not entries:
            continue
        if args.markdown:
            print(f"## {kind} ({len(entries)} shown)\n")
            for e in entries:
                age = ""
                if e.session_ids:
                    age = f"  _(seen in {len(e.session_ids)} session(s), hit_count={e.hit_count})_"
                tools = ""
                if e.tools:
                    tools = f"  `tools: {', '.join(e.tools)}`"
                print(f"- {e.text}{age}{tools}")
            print()
        else:
            print(f"--- {kind} ({len(entries)} shown) ---")
            for e in entries:
                hint = f"hit_count={e.hit_count}, sessions={len(e.session_ids or [])}"
                tools = f", tools={','.join(e.tools)}" if e.tools else ""
                print(f"  • {e.text}\n    ({hint}{tools})")


def cmd_memory_list(args: argparse.Namespace) -> None:
    """Lower-level cousin of memory show: just dump entries as JSONL so a
    pipeline can grep / jq / pipe them. Mirrors `kubectl get -o json` shape."""
    config = load_config(args.config)
    store = _open_store(config["memory"])
    kind = None
    if args.strategic:
        kind = "strategic"
    elif args.tactical:
        kind = "tactical"
    entries = store.iterate(kind=kind, limit=args.limit)
    for e in entries:
        print(json.dumps(e.to_payload()))


def _repo_provider_name(args: argparse.Namespace) -> str:
    requested = getattr(args, "provider", "auto")
    return detect_provider(getattr(args, "repo", None), dict(os.environ)) if requested == "auto" else requested


def _repo_token(args: argparse.Namespace, provider_name: str | None = None) -> str:
    provider = provider_name or _repo_provider_name(args)
    token = args.token or os.environ.get(f"{provider.upper()}_TOKEN")
    if token:
        return token
    print(f"{provider} token required: pass --token or set {provider.upper()}_TOKEN", file=sys.stderr)
    sys.exit(1)


def _repo_name(args: argparse.Namespace, provider_name: str | None = None) -> str:
    provider = provider_name or _repo_provider_name(args)
    env_names = {
        "github": ("GITHUB_REPOSITORY",),
        "gitlab": ("CI_PROJECT_PATH",),
        "bitbucket": ("BITBUCKET_REPO_FULL_NAME",),
    }
    repo = args.repo or next((os.environ[name] for name in env_names[provider] if os.environ.get(name)), None)
    if repo:
        return repo
    print(f"{provider} repository required: pass --repo owner/name or set its CI repository variable", file=sys.stderr)
    sys.exit(1)


def _repo_body(args: argparse.Namespace) -> str:
    if args.body is not None and args.body_file is not None:
        print("pass either --body or --body-file, not both", file=sys.stderr)
        sys.exit(1)
    if args.body_file is not None:
        try:
            return Path(args.body_file).read_text()
        except OSError as error:
            print(f"could not read body file {args.body_file}: {error}", file=sys.stderr)
            sys.exit(1)
    return args.body or ""


def cmd_repo_issue(args: argparse.Namespace) -> None:
    """Create a tracking issue or post an update to its deduplicated issue."""
    try:
        provider_name = _repo_provider_name(args)
        url = get_provider(provider_name, _repo_token(args, provider_name)).open_or_update_issue(
            _repo_name(args, provider_name), args.title, _repo_body(args), args.key, args.label
        )
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    print(url)


def cmd_repo_suggest_prompt(args: argparse.Namespace) -> None:
    """Propose proven memory guidelines for inclusion in the agent prompt."""
    config = load_config(args.config)
    store = _open_store(config["memory"])
    guidelines = store.iterate(kind=args.kind, limit=args.limit)
    if not guidelines:
        print(f"no {args.kind} guidelines yet — the agent hasn't promoted any lessons")
        return

    intro = (
        "This agent has learned the following from its own runs (guidelines promoted "
        "to `strategic` after recurring across ≥3 sessions). Consider folding them into "
        "its system prompt."
    )
    body = "\n\n".join(
        [
            intro,
            "\n".join(f"- {guideline.text}" for guideline in guidelines),
            "Current system prompt for context:\n\n```\n"
            f"{config['agent'].get('system_prompt', '')}\n```",
        ]
    )
    agent_name = config["agent"].get("name", "default")
    title = f"Consider promoted guidelines for {agent_name}'s system prompt"
    try:
        provider_name = _repo_provider_name(args)
        url = get_provider(provider_name, _repo_token(args, provider_name)).open_or_update_issue(
            _repo_name(args, provider_name),
            title,
            body,
            f"suggest-prompt:{agent_name}",
            args.label,
        )
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    print(url)


_FABRI_BLOCK_START = "# --- fabri: learned from prior runs (auto-proposed) ---"
_FABRI_BLOCK_END = "# --- end fabri ---"


def apply_learned_block(yaml_text: str, guidelines: list[str]) -> str:
    """Insert or replace fabri's literal-block comments without reformatting YAML."""
    lines = yaml_text.splitlines(keepends=True)
    scalar_at: int | None = None
    key_indent = ""
    for index, line in enumerate(lines):
        match = re.match(r"^(?P<indent>[ \t]*)system_prompt:\s*[>|][+-]?\s*(?:#.*)?(?:\r?\n)?$", line)
        if match:
            scalar_at, key_indent = index, match.group("indent")
            break
    if scalar_at is None:
        raise ValueError("system_prompt must use a YAML literal or folded block scalar")
    scalar_end = len(lines)
    for index in range(scalar_at + 1, len(lines)):
        content = lines[index]
        if content.strip() and len(content) - len(content.lstrip(" \t")) <= len(key_indent):
            scalar_end = index
            break
    content_indent = key_indent + "  "
    for line in lines[scalar_at + 1:scalar_end]:
        if line.strip():
            content_indent = line[:len(line) - len(line.lstrip(" \t"))]
            break
    newline = "\r\n" if "\r\n" in yaml_text else "\n"
    block = [content_indent + _FABRI_BLOCK_START + newline]
    block.extend(content_indent + f"# - {guideline}" + newline for guideline in guidelines)
    block.append(content_indent + _FABRI_BLOCK_END + newline)
    start = end = None
    for index in range(scalar_at + 1, scalar_end):
        stripped = lines[index].strip()
        if stripped == _FABRI_BLOCK_START:
            start = index
        elif start is not None and stripped == _FABRI_BLOCK_END:
            end = index + 1
            break
    if start is not None and end is not None:
        lines[start:end] = block
    else:
        lines[scalar_end:scalar_end] = block
    return "".join(lines)


def cmd_repo_open_pr(args: argparse.Namespace) -> None:
    """Propose learned guidelines as a one-file draft pull request."""
    config = load_config(args.config)
    store = _open_store(config["memory"])
    guidelines = store.iterate(kind=args.kind, limit=args.limit)
    if not guidelines:
        print(f"no {args.kind} guidelines yet")
        return
    config_path = Path(args.config).resolve()
    try:
        relative_path = config_path.relative_to(Path.cwd().resolve())
    except ValueError:
        print("--config must be inside the repository root", file=sys.stderr)
        sys.exit(1)
    edited = apply_learned_block(config_path.read_text(), [guideline.text for guideline in guidelines])
    agent_name = config["agent"].get("name", "default")
    title = f"fabri: propose learned guidelines for {agent_name}"
    body = "\n\n".join([
        "This draft applies promoted guidelines to the agent's `system_prompt` for maintainer review.",
        "\n".join(f"- {guideline.text}" for guideline in guidelines),
    ])
    try:
        provider_name = _repo_provider_name(args)
        provider = get_provider(provider_name, _repo_token(args, provider_name))
        provider.push_branch(
            _repo_name(args, provider_name), args.base, args.branch, str(relative_path), edited,
            f"fabri: propose learned guidelines for {agent_name}",
        )
        url = provider.open_or_update_pr(
            _repo_name(args, provider_name), title, body, f"open-pr:{agent_name}", args.branch, args.base,
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    print(url)


def cmd_repo_run(args: argparse.Namespace) -> None:
    from fabri.repo.run import run_repo_flow

    result = run_repo_flow(
        issue_id=args.from_linear,
        repo=args.repo,
        base=args.base,
        config=args.config,
        test_cmd=args.test_cmd,
        setup_cmd=args.setup_cmd,
    )
    print(json.dumps(result, indent=2, default=str))
    if not result.get("ok"):
        sys.exit(1)


def cmd_memory_diff(args: argparse.Namespace) -> None:
    """G3: compare what the memory store learned between two sessions.

    Each guideline carries a `session_ids` list — the sessions that contributed
    to it. We partition every entry into three groups by membership in
    {session_a, session_b}:

    - **shared**: both A and B in session_ids (the guideline recurred in both)
    - **new_in_b**: B in session_ids, A not — what session B taught the agent
    - **only_in_a**: A in session_ids, B not — what session A taught that B
      didn't surface (either A's lesson didn't apply to B, or it's already
      strategic and was retrieved without contributing again)

    Useful as a demo: "look what fabri learned in this 30-minute run."
    """
    config = load_config(args.config)
    store = _open_store(config["memory"])
    a, b = args.session_a, args.session_b
    shared, new_in_b, only_in_a = [], [], []
    for entry in store.iterate():
        sids = set(entry.session_ids or [])
        in_a = a in sids
        in_b = b in sids
        if in_a and in_b:
            shared.append(entry)
        elif in_b:
            new_in_b.append(entry)
        elif in_a:
            only_in_a.append(entry)

    def _render(label: str, entries: list) -> None:
        if args.markdown:
            print(f"## {label} ({len(entries)})\n")
            for e in entries:
                print(f"- [{e.kind}] {e.text}")
            print()
        else:
            print(f"--- {label} ({len(entries)}) ---")
            for e in entries:
                print(f"  [{e.kind}] {e.text}")

    if args.markdown:
        print(f"# memory diff: {a[:8]} → {b[:8]}\n")
    _render(f"new in {b[:8]}", new_in_b)
    _render(f"shared between {a[:8]} and {b[:8]}", shared)
    _render(f"only in {a[:8]}", only_in_a)


def cmd_memory_stale(args: argparse.Namespace) -> None:
    """Read-side staleness report: guidelines with low hit_count relative to
    their age (see fabri.memory.staleness). Pure report -- does not touch
    scoring, retrieval, or eviction.

    Runs against the primary store, and -- when memory.global_collection is
    configured -- ALSO against that global store, tagging each result's
    "tier" so the two are distinguishable in the merged JSON output."""
    from fabri.memory.staleness import find_stale_guidelines
    from fabri.orchestrator.retrieval import RetrievalConfig, _open_global_store

    config = load_config(args.config)
    mem_cfg = config["memory"]
    kinds = tuple(args.kind) if args.kind else ("tactical", "strategic")
    max_hit_count = (
        args.max_hit_count if args.max_hit_count is not None
        else mem_cfg.get("stale_max_hit_count", 2)
    )
    min_age_days = (
        args.min_age_days if args.min_age_days is not None
        else mem_cfg.get("stale_min_age_days", 7.0)
    )

    def _report(store) -> list[dict]:
        return [
            {**vars(g)}
            for g in find_stale_guidelines(
                store,
                max_hit_count=max_hit_count,
                min_age_days=min_age_days,
                kinds=kinds,
                limit=args.limit,
            )
        ]

    store = _open_store(mem_cfg)
    results = [{"tier": "project", **d} for d in _report(store)]

    rcfg = RetrievalConfig.from_mem_cfg(mem_cfg)
    if rcfg.global_collection:
        global_store = _open_global_store(rcfg)
        if global_store is not None:
            results.extend({"tier": "global", **d} for d in _report(global_store))

    print(json.dumps(results, indent=2))


def cmd_replay(args: argparse.Namespace) -> None:
    """G5: re-run the task from a prior session against the *current* memory
    state. Prints a before/after table so the user can see whether the memory
    loop actually changed the agent's behaviour.

    Caveats: the LLM is non-deterministic. Outcome / step_count / cost can
    differ even with no memory change. Read the comparison as a directional
    signal, not proof; pair with `fabri.benchmarks.session_delta` for a
    statistically meaningful answer.
    """
    from fabri.orchestrator.traces import read_trace

    original_events = read_trace(args.session_id)
    if not original_events:
        print(f"no trace at .fabri/traces/{args.session_id}.jsonl", file=sys.stderr)
        sys.exit(1)
    start = next((e for e in original_events if e.get("type") == "start"), None)
    if start is None or not start.get("task"):
        print("trace has no start event with a task", file=sys.stderr)
        sys.exit(1)
    task = start["task"]
    final = next((e for e in original_events if e.get("type") == "final"), None)
    usage_evt = next((e for e in original_events if e.get("type") == "usage"), None)
    original_summary = {
        "outcome": (final or {}).get("outcome", "?"),
        "cost_usd": (usage_evt or {}).get("cost_usd"),
        "step_count": (usage_evt or {}).get("step_count", "?"),
    }

    config = load_config(args.config)
    _require_role_api_keys(config)
    new_session_id = str(uuid.uuid4())
    configure_logging(new_session_id, verbose=args.verbose)
    mem_cfg = config["memory"]
    store = _open_store(mem_cfg)
    tools_cfg = config["tools"]
    tools = build_tools(tools_cfg)
    decompose_cfg = tools_cfg["decompose"]
    llms = build_run_llms(config, build_tool_defs(tools, decompose_cfg))
    # Same AgentRunConfig as `run` so replay holds orchestration constant and
    # varies only the memory state — the whole point of the command.
    run_cfg = AgentRunConfig.from_config(config)

    print(f"replay task: {task!r}", file=sys.stderr)
    print(f"  original session: {args.session_id[:8]} "
          f"outcome={original_summary['outcome']} "
          f"cost={_fmt_usd_or_dash(original_summary['cost_usd'])} "
          f"steps={original_summary['step_count']}", file=sys.stderr)

    result = run_agent(
        task, llms["llm"], tools, store,
        session_id=new_session_id,
        decompose_llm=llms["decompose_llm"],
        planner_llm=llms["planner_llm"],
        narrator_llm=llms["narrator_llm"],
        **run_cfg.as_kwargs(),
    )
    new_summary = {
        "outcome": result.get("outcome", "?"),
        "cost_usd": (result.get("usage") or {}).get("cost_usd"),
        "step_count": (result.get("usage") or {}).get("step_count", "?"),
    }
    print(f"  replay session:   {new_session_id[:8]} "
          f"outcome={new_summary['outcome']} "
          f"cost={_fmt_usd_or_dash(new_summary['cost_usd'])} "
          f"steps={new_summary['step_count']}", file=sys.stderr)

    if (original_summary["cost_usd"] is not None
            and new_summary["cost_usd"] is not None):
        delta = new_summary["cost_usd"] - original_summary["cost_usd"]
        pct = (delta / original_summary["cost_usd"] * 100.0) if original_summary["cost_usd"] else 0.0
        arrow = "↓" if delta < 0 else "↑" if delta > 0 else "→"
        print(f"\ncost delta: {arrow}{abs(pct):.0f}%", file=sys.stderr)

    print(json.dumps({
        "task": task,
        "original": {**original_summary, "session_id": args.session_id},
        "replay": {**new_summary, "session_id": new_session_id},
    }, indent=2))


def _fmt_usd_or_dash(v) -> str:
    return f"${v:.4f}" if isinstance(v, (int, float)) else "—"


def cmd_tool_init(args: argparse.Namespace) -> None:
    """G14: scaffold a new tool (manifest + executable stub) in the picked
    language. Default target dir: tools/agent_tools next to the cwd."""
    from pathlib import Path as _P
    target = _P(args.dir)
    try:
        result = scaffold_tool(args.language, args.name, target, force=args.force)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    if result["created"]:
        print(f"Scaffolded a {args.language!r} tool {args.name!r} in {target}:")
        for f in result["created"]:
            print(f"  + {f}")
    if result["skipped"]:
        print("\nLeft existing files untouched (pass --force to overwrite):")
        for f in result["skipped"]:
            print(f"  . {f}")
    print(f"\nNext: edit {args.name}.* to your liking, then list this dir in "
          f"`tools.manifest_dir` of your agent.yaml.")


def cmd_new_agency(args: argparse.Namespace) -> None:
    """Scaffold a multi-agent agency from a bundled template or registry source."""
    try:
        if args.from_source:
            files, readme, entry = resolve_source(args.from_source)
            if not args.name or Path(args.name).name != args.name or args.name in {".", ".."}:
                raise ValueError("agency name must be a single directory name")
            agency_dir = Path(args.dest) / args.name
            if agency_dir.exists():
                raise FileExistsError(f"destination already exists: {agency_dir}")
            created = write_template(
                agency_dir,
                files,
                readme,
                run_from=str(Path.cwd()),
                slug=_slug(args.name),
            )
        else:
            created = scaffold_agency(args.name, args.template, args.dest)
            entry = "agent.openai.yaml"
    except (OSError, ValueError) as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    source = args.from_source or args.template
    print(f"Scaffolded {source!r} agency {args.name!r}:")
    for path in created:
        print(f"  + {path}")
    print("\n" + agency_next_steps(args.name, args.dest, entry))


def cmd_company_compile(args: argparse.Namespace) -> None:
    """Compile a company.toml into nested manager and agency configs."""
    try:
        run_from = Path(args.run_from) if getattr(args, "run_from", None) else None
        root_config = compile_company(args.company_toml, args.dest, run_from=run_from)
    except (OSError, ValueError) as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    company_dir = root_config.parent
    print(f"Compiled company into {company_dir}:")
    for path in sorted(company_dir.rglob("*")):
        if path.is_file():
            print(f"  + {path}")
    print("\n" + company_next_steps(root_config))


def _build_enrichment_llm():
    """B2: build an LLM backend for `tool new --from` schema enrichment, but
    ONLY when the default config's main-role API key is set. Returns None
    otherwise so the tool-writer degrades to its deterministic fallback. Any
    construction error -> None (never block the scaffold on the network)."""
    from fabri.config import load_config
    from fabri.runtime import find_missing_role_api_keys

    try:
        config = load_config(None)
        if find_missing_role_api_keys(config):
            return None
        return build_llm(config, [])
    except Exception:
        return None


def cmd_tool_new(args: argparse.Namespace) -> None:
    """B2: scaffold a schema-tightened tool from a Python signature, a
    description, or (default) the tightened starter scaffold."""
    from fabri.builder import new_tool

    llm = None
    if args.from_desc is not None and not args.no_llm:
        llm = _build_enrichment_llm()
    try:
        result = new_tool(
            args.name,
            lang=args.lang,
            from_signature=args.from_signature,
            from_desc=args.from_desc,
            target_dir=args.dir,
            force=args.force,
            llm=llm,
        )
    except (ValueError, OSError) as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    if result["created"]:
        print(f"Scaffolded a {result['language']!r} tool {args.name!r} "
              f"({result['mode']}) in {args.dir}:")
        for f in result["created"]:
            print(f"  + {f}")
    if result["skipped"]:
        print("\nLeft existing files untouched (pass --force to overwrite):")
        for f in result["skipped"]:
            print(f"  . {f}")
    print(f"\nNext: `fabri tool validate {args.dir}/{args.name}.json` "
          f"then `fabri tool test {args.name} --dir {args.dir}`.")


def cmd_tool_validate(args: argparse.Namespace) -> None:
    """B2: validate a manifest's shape, its schemas, and that its script
    resolves. Exits nonzero on failure."""
    from fabri.builder import validate_manifest

    ok, lines = validate_manifest(args.manifest)
    for line in lines:
        print(line)
    if not ok:
        sys.exit(1)


def _invoke_tool_and_print(name: str, raw_json: str | None, target_dir: str,
                           *, arg_label: str) -> None:
    """Shared body of `tool test` and `tool run`: parse the JSON args, run the
    named tool through the existing registry/sandbox, print the normalized
    {ok, result?, error?} envelope, and exit non-zero on failure. `arg_label`
    is the user-facing name of the args source (`--args` vs `<json-args>`) so
    each command reports errors in its own terms."""
    from fabri.builder import test_tool

    parsed_args: dict = {}
    if raw_json:
        try:
            parsed_args = json.loads(raw_json)
        except json.JSONDecodeError as e:
            print(f"{arg_label}: not valid JSON: {e}", file=sys.stderr)
            sys.exit(1)
        if not isinstance(parsed_args, dict):
            print(f"{arg_label}: must be a JSON object", file=sys.stderr)
            sys.exit(1)
    try:
        envelope = test_tool(name, parsed_args, target_dir=target_dir)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    print(json.dumps(envelope, indent=2))
    if not envelope.get("ok"):
        sys.exit(1)


def cmd_tool_test(args: argparse.Namespace) -> None:
    """B2: run a tool locally through the existing registry/sandbox and print
    the normalized {ok, result?, error?} envelope."""
    _invoke_tool_and_print(args.name, args.args, args.dir, arg_label="--args")


def cmd_tool_run(args: argparse.Namespace) -> None:
    """B3: direct invoke of a tool via the runner for debugging — the positional
    cousin of `tool test` (`fabri tool run <name> '<json>'`). Shares
    `_invoke_tool_and_print` so the two never drift on resolution/normalization."""
    _invoke_tool_and_print(args.name, args.json_args, args.dir, arg_label="<json-args>")


def cmd_prompt_new(args: argparse.Namespace) -> None:
    """B5: write a starter agent prompt from the proven prompt-kit skeleton,
    so a new prompt begins from a fill-in template rather than a blank file."""
    from fabri.builder import new_prompt

    try:
        result = new_prompt(
            args.name,
            role=args.role,
            output=args.output,
            force=args.force,
        )
    except OSError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    if not result["created"]:
        print(
            f"{result['path']} already exists; pass --force to overwrite.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"Wrote starter prompt to {result['path']}.")
    print("Next: fill the CHARTER, WHAT YOU OWN, and TOOL ROUTING sections, then "
          "reference it from your agent.yaml `agent.system_prompt`.")


def cmd_ideate(args: argparse.Namespace) -> None:
    """B1: turn a one-sentence product idea into a reviewable agent scaffold
    (agent.yaml + prompts + tool stubs). Needs an LLM to draft the spec; emits
    files for review only -- it never auto-applies or touches a running agent."""
    from fabri.builder import IdeatorError, ideate

    llm = _build_enrichment_llm()
    if llm is None:
        print(
            "`fabri ideate` needs a model to draft the spec, but no provider API "
            "key is set. Export it first, e.g.: export ANTHROPIC_API_KEY=<key>",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        summary = ideate(args.idea, llm, out_dir=args.out, force=args.force)
    except IdeatorError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    print(f"Drafted agent {summary['spec'].get('agent_name')!r} into {summary['root']}:")
    for path in [summary["agent_yaml"], *summary["prompts"], *summary["tools"]]:
        print(f"  + {path}")
    if summary["skipped"]:
        print("\nLeft existing files untouched (pass --force to overwrite):")
        for path in summary["skipped"]:
            print(f"  . {path}")
    print(
        "\nReview and edit the scaffold, then run it:\n"
        f"  {summary['next_command']}"
    )


def cmd_skills_list(args: argparse.Namespace) -> None:
    """B4: list discoverable skills -- the bundled examples plus a project-local
    skills dir (default ./skills)."""
    from fabri.builder import discover_skills, render_skills_listing

    skills = discover_skills(args.dir)
    print(render_skills_listing(skills))


def cmd_skills_install(args: argparse.Namespace) -> None:
    """B4: install a skill into a project -- copy its tools + prompts and merge
    its config snippet into agent.yaml (additive; existing keys preserved)."""
    from fabri.builder import SkillError, install_skill

    try:
        summary = install_skill(
            args.skill, args.into, skills_dir=args.dir, force=args.force
        )
    except SkillError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    print(f"Installed skill {summary['skill']!r} into {args.into}:")
    for path in [*summary["tools"], *summary["prompts"]]:
        print(f"  + {path}")
    if summary["config"]:
        keys = ", ".join(summary["config_keys"]) or "(no new top-level keys)"
        print(f"  ~ {summary['config']} (merged; added: {keys})")
    if summary["skipped"]:
        print("\nLeft existing files untouched (pass --force to overwrite):")
        for path in summary["skipped"]:
            print(f"  . {path}")
    if summary["conflicts"]:
        print("\nConfig conflicts (project values kept, skill's ignored):")
        for c in summary["conflicts"]:
            print(f"  ! {c}")


def cmd_skills_add(args: argparse.Namespace) -> None:
    """B4: scaffold a fresh skill skeleton (skill.yaml + config snippet + empty
    prompts/ and tools/) to author a new reusable capability."""
    from fabri.builder import SkillError, new_skill

    try:
        result = new_skill(args.name, target_dir=args.dir, force=args.force)
    except SkillError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    if result["created"]:
        print(f"Scaffolded skill {args.name!r} in {result['root']}:")
        for path in result["created"]:
            print(f"  + {path}")
    if result["skipped"]:
        print("\nLeft existing files untouched (pass --force to overwrite):")
        for path in result["skipped"]:
            print(f"  . {path}")
    print("\nNext: add tool manifest+executable pairs under tools/, prompt "
          "templates under prompts/, and edit config.yaml, then "
          f"`fabri skills install {args.name}`.")


def cmd_report(args: argparse.Namespace) -> None:
    """G6/G7/G8/G20: aggregate JSONL traces into a usage report. Output in
    markdown (default), json, or self-contained HTML."""
    from fabri.reports import (
        aggregate,
        collect_sessions,
        compute_memory_health,
        render_html,
        render_json,
        render_markdown,
    )

    since_seconds = None
    if args.since:
        # Accept "7d", "24h", "30m" — humane shorthand.
        unit = args.since[-1].lower()
        try:
            n = float(args.since[:-1])
        except ValueError:
            print(f"--since: expected like '7d', '24h', '30m', got {args.since!r}",
                  file=sys.stderr)
            sys.exit(1)
        multipliers = {"d": 86400, "h": 3600, "m": 60, "s": 1}
        if unit not in multipliers:
            print(f"--since: unknown unit {unit!r} (expected d/h/m/s)", file=sys.stderr)
            sys.exit(1)
        since_seconds = n * multipliers[unit]

    sessions = collect_sessions(since_seconds=since_seconds, limit=args.limit)
    report = aggregate(sessions)

    # M6: attach a memory-health snapshot when a store is reachable. Best-effort
    # and OFFLINE-SAFE — build_memory_store directly (NOT _open_store, which
    # sys.exit(1)s on an unreachable backend and would kill the trace-only
    # report). Any failure leaves report.memory_health = None and the report
    # renders exactly as before.
    try:
        config = load_config(args.config)
        store = build_memory_store(config["memory"])
        report.memory_health = compute_memory_health(store)
    except Exception as e:  # noqa: BLE001 -- memory health is a best-effort add-on
        import logging
        logging.getLogger("fabri").debug("memory-health snapshot skipped: %s", e)

    if args.format == "json":
        output = render_json(report)
    elif args.format == "html":
        output = render_html(report)
    else:
        output = render_markdown(report)

    if args.output:
        from pathlib import Path
        Path(args.output).write_text(output)
        print(f"wrote {args.output} ({len(output)} bytes, {report.session_count} sessions)",
              file=sys.stderr)
    else:
        print(output)


def cmd_admin_config(args: argparse.Namespace) -> None:
    try:
        require_admin(args.admin_token)
    except AdminAuthError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    config = load_config(args.config)
    tools = build_tools(config["tools"])
    print(json.dumps(describe_config(config, tools), indent=2))


def _maybe_export_trace(session_id: str, config: dict) -> None:
    """Best-effort end-of-run OTLP export. No-op unless
    `observability.otlp_endpoint` is set. Swallows every error (including a
    missing `fabri[otel]` extra or an unreachable collector) with a warning, so
    a misconfigured exporter can never fail an otherwise-successful run — use
    `fabri traces export` to see those errors loudly instead."""
    import logging

    from fabri.observability import OtelConfig, export_trace

    otel_cfg = OtelConfig.from_config(config)
    if not otel_cfg.enabled:
        return
    log = logging.getLogger("fabri")
    try:
        if export_trace(session_id, otel_cfg):
            log.info("exported trace %s to %s", session_id, otel_cfg.endpoint)
    except Exception as e:  # best-effort: never fail the run over telemetry
        log.warning("OTLP trace export failed (continuing): %s", e)


def cmd_traces_export(args: argparse.Namespace) -> None:
    """Export a finished session's trace to the configured OTLP backend
    (`observability.otlp_endpoint`; Langfuse / Honeycomb / Tempo / Jaeger / …).
    Unlike the best-effort end-of-run auto-export, this surfaces errors loudly:
    a missing `fabri[otel]` extra, an unreachable endpoint, or an empty/unknown
    session all exit non-zero."""
    from fabri.observability import OtelConfig, export_trace

    config = load_config(args.config)
    otel_cfg = OtelConfig.from_config(config)
    if not otel_cfg.enabled:
        print(
            "observability.otlp_endpoint is not set; nothing to export. Set it in "
            "your config or via FABRI_OTLP_ENDPOINT.",
            file=sys.stderr,
        )
        sys.exit(1)
    if export_trace(args.session_id, otel_cfg):
        print(f"exported trace {args.session_id} to {otel_cfg.endpoint}")
    else:
        print(f"no trace to export for {args.session_id}", file=sys.stderr)
        sys.exit(1)


def cmd_traces_show(args: argparse.Namespace) -> None:
    """Pretty-print a session's JSONL trace. The framework already writes
    every step (start / tool_call / thought / final / failed) to
    .fabri/traces/<sid>.jsonl; this is the human-readable reader."""
    from fabri.orchestrator.trace_render import render_event
    from fabri.orchestrator.traces import read_trace, trace_path

    events = read_trace(args.session_id)
    if not events:
        print(f"no events at {trace_path(args.session_id)}", file=sys.stderr)
        sys.exit(1)
    t0 = events[0].get("ts", 0.0)
    for ev in events:
        print(render_event(ev, t0))


def cmd_traces_tail(args: argparse.Namespace) -> None:
    """Follow a trace file (like `tail -f`), pretty-printing new events as
    they arrive. Useful when the agent is running in another shell and you
    want a live view of its tool calls."""
    import time as _time
    from fabri.orchestrator.trace_render import render_event
    from fabri.orchestrator.traces import trace_path

    path = trace_path(args.session_id)
    if not path.exists():
        path.touch()
    with path.open("r") as f:
        # Seek to end so we only see new events, not historical.
        f.seek(0, 2)
        t_start = _time.time()
        try:
            while True:
                line = f.readline()
                if not line:
                    _time.sleep(0.2)
                    continue
                try:
                    ev = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                print(render_event(ev, t_start), flush=True)
        except KeyboardInterrupt:
            pass


def cmd_traces_list(args: argparse.Namespace) -> None:
    """List recent traces under $FABRI_HOME/traces (or .fabri/traces) so the
    user can find a session_id without remembering the UUID."""
    from fabri.paths import traces_dir

    d = traces_dir()
    if not d.exists():
        print(f"no traces dir at {d}", file=sys.stderr)
        return
    entries = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in entries[: args.limit]:
        size = p.stat().st_size
        print(f"  {p.stem}  ({size} B)")


def cmd_admin_dashboard(args: argparse.Namespace) -> None:
    try:
        require_admin(args.admin_token)
    except AdminAuthError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    config = load_config(args.config)
    tools = build_tools(config["tools"])
    mem_cfg = config["memory"]
    store = _open_store(mem_cfg)
    print(render_dashboard(config, tools, store))


def cmd_serve(args: argparse.Namespace) -> None:
    """B7: start the embeddable HTTP service. A non-Python host POSTs a task and
    streams events (SSE) without importing fabri. Blocks until Ctrl-C."""
    from fabri.service.http_server import serve_http
    from fabri.service.service import FabriService

    service = FabriService(template_config=args.config, home_root=args.home_root)
    server = serve_http(service, host=args.host, port=args.port)
    bound_host, bound_port = server.server_address[0], server.server_address[1]
    print(
        f"fabri serve listening on http://{bound_host}:{bound_port} "
        f"(POST /runs, GET /runs/<id>/events, GET /runs/<id>/result)",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        service.close()


def cmd_studio(args: argparse.Namespace) -> None:
    """Start the HTTP service with the bundled Studio UI enabled."""
    from fabri.service.http_server import serve_http, studio_assets_available
    from fabri.service.service import FabriService

    if not studio_assets_available():
        print(
            "Studio assets aren't bundled in this build — run "
            "`npm --prefix examples/studio run build && python "
            "scripts/sync_studio_assets.py`, or install a released wheel",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.catalog:
        from fabri.catalog import load_catalog

        if args.config or args.company:
            print("--catalog supplied; ignoring --config and --company", file=sys.stderr)
        try:
            if args.home_root:
                Path(args.home_root).mkdir(parents=True, exist_ok=True)
            work_dir = Path(
                tempfile.mkdtemp(prefix="fabri-catalog-", dir=args.home_root or None)
            )
            catalog = load_catalog(args.catalog, work_dir)
        except (OSError, ValueError) as exc:
            print(f"catalog error: {exc}", file=sys.stderr)
            sys.exit(1)
        service = FabriService(catalog=catalog, home_root=args.home_root)
        server = serve_http(
            service, host=args.host, port=args.port, serve_studio=True, catalog=catalog
        )
    else:
        company = None
        template_config = args.config
        if args.company:
            if args.config:
                print("--company supplied; ignoring --config", file=sys.stderr)
            try:
                root = compile_company(
                    args.company,
                    tempfile.mkdtemp(prefix="fabri-company-"),
                    run_from=Path.cwd(),
                )
                company = company_org(args.company)
            except (OSError, ValueError) as exc:
                print(f"company error: {exc}", file=sys.stderr)
                sys.exit(1)
            template_config = str(root)

        service = FabriService(template_config=template_config, home_root=args.home_root)
        server = serve_http(
            service,
            host=args.host,
            port=args.port,
            serve_studio=True,
            company=company,
        )
    bound_host, bound_port = server.server_address[0], server.server_address[1]
    print(f"Open Fabri Studio at http://{bound_host}:{bound_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        service.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="fabri")
    parser.add_argument(
        "--version",
        action="version",
        version=f"fabri {importlib.metadata.version('fabri')}",
    )
    parser.add_argument("--verbose", action="store_true", help="Log at DEBUG level to the console")
    parser.add_argument("--config", dest="config", default=None, help="Path to an agent.yaml config")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Scaffold a starter fabri project (agent.yaml, tools, docker-compose)")
    p_init.add_argument("dir", nargs="?", default=".", help="Target directory (default: current)")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing files")
    p_init.add_argument(
        "--template",
        choices=sorted(SCAFFOLD_TEMPLATES.keys()),
        default="default",
        help="Starter pack: default (hello), research, code-review, data-cleanup. "
             "Non-default templates use the sqlite-vec backend (no docker required).",
    )
    p_init.set_defaults(func=cmd_init)

    p_examples = sub.add_parser(
        "examples", help="List bundled example agencies or copy them into a directory")
    p_examples.add_argument(
        "--copy", metavar="DIR", default=None,
        help="Copy every bundled example agency into DIR")
    p_examples.set_defaults(func=cmd_examples)

    p_run = sub.add_parser("run", help="Run the agent on a task")
    p_run.add_argument("task")
    p_run.add_argument("--session-id", dest="session_id", default=None)
    p_run.add_argument("--ask-user-socket", dest="ask_user_socket", default=None,
                       help="Path to a Unix socket the ask_user tool routes questions to (A1).")
    p_run.add_argument("--dry-run", dest="dry_run", action="store_true",
                       help="Print the resolved config + the tool defs that would be "
                            "sent to the model, then exit without any LLM call (B3). "
                            "Requires no API key.")
    p_run.set_defaults(func=cmd_run)

    p_ingest = sub.add_parser("ingest-traces", help="Synthesize guidelines from a session's trace")
    p_ingest.add_argument("session_id")
    p_ingest.set_defaults(func=cmd_ingest_traces)

    p_ing = sub.add_parser(
        "ingest",
        help="Mine an EXTERNAL log into memory via a plug-and-play adapter (the Improver)",
    )
    p_ing.add_argument("source", nargs="?", default=None,
                       help="Log file, directory, or '-' for stdin. Omit with --list-adapters.")
    p_ing.add_argument("--adapter", default="auto",
                       help="Adapter name (jsonl|regex|otel|openai|<plugin>) or 'auto' to sniff.")
    p_ing.add_argument("--synthesize", action="store_true",
                       help="Run LLM guideline compression (costs tokens). Default is deterministic ($0).")
    p_ing.add_argument("--option", action="append", metavar="KEY=VALUE",
                       help="Adapter option (repeatable), e.g. --option pattern='tool=(?P<tool>\\S+)'.")
    p_ing.add_argument("--session-id", dest="session_id", default=None, help="Log session id label.")
    p_ing.add_argument("--dry-run", dest="dry_run", action="store_true",
                       help="Parse + count without writing to the memory store.")
    p_ing.add_argument("--json", action="store_true", help="Emit the summary as JSON.")
    p_ing.add_argument("--list-adapters", dest="list_adapters", action="store_true",
                       help="List available ingest adapters and exit.")
    p_ing.set_defaults(func=cmd_ingest)

    p_inspect = sub.add_parser("inspect-memory", help="Inspect stored memory, optionally querying it")
    p_inspect.add_argument("query", nargs="?", default=None)
    p_inspect.add_argument("--top-k", dest="top_k", type=int, default=5)
    p_inspect.set_defaults(func=cmd_inspect_memory)

    p_mem = sub.add_parser("memory", help="Show or list stored guidelines")
    mem_sub = p_mem.add_subparsers(dest="memory_command", required=True)

    p_mem_show = mem_sub.add_parser("show", help="Human-readable listing of guidelines")
    p_mem_show.add_argument("--strategic", action="store_true", help="Only strategic guidelines")
    p_mem_show.add_argument("--tactical", action="store_true", help="Only tactical guidelines")
    p_mem_show.add_argument("--limit", type=int, default=50)
    p_mem_show.add_argument("--markdown", action="store_true", help="Render as markdown (paste into a deck)")
    p_mem_show.set_defaults(func=cmd_memory_show)

    p_mem_list = mem_sub.add_parser("list", help="JSONL listing of guidelines (pipeable)")
    p_mem_list.add_argument("--strategic", action="store_true")
    p_mem_list.add_argument("--tactical", action="store_true")
    p_mem_list.add_argument("--limit", type=int, default=None)
    p_mem_list.set_defaults(func=cmd_memory_list)

    p_mem_diff = mem_sub.add_parser("diff", help="Compare guidelines between two sessions")
    p_mem_diff.add_argument("session_a", help="The earlier session id")
    p_mem_diff.add_argument("session_b", help="The later session id")
    p_mem_diff.add_argument("--markdown", action="store_true", help="Render as markdown")
    p_mem_diff.set_defaults(func=cmd_memory_diff)

    p_mem_stale = mem_sub.add_parser(
        "stale", help="Report guidelines with low hit_count relative to age (JSON, pipeable)")
    p_mem_stale.add_argument("--max-hit-count", dest="max_hit_count", type=int, default=None,
                             help="Default: memory.stale_max_hit_count (2)")
    p_mem_stale.add_argument("--min-age-days", dest="min_age_days", type=float, default=None,
                             help="Default: memory.stale_min_age_days (7.0)")
    p_mem_stale.add_argument("--kind", dest="kind", action="append", default=None,
                             help="Restrict to this kind (repeatable; default: tactical + strategic)")
    p_mem_stale.add_argument("--limit", type=int, default=None)
    p_mem_stale.set_defaults(func=cmd_memory_stale)

    p_repo = sub.add_parser("repo", help="File issues and draft PRs from a fabri run")
    repo_sub = p_repo.add_subparsers(dest="repo_command", required=True)

    p_repo_issue = repo_sub.add_parser("issue", help="Create or update a deduplicated tracking issue")
    p_repo_issue.add_argument("--repo", default=None, help="Repository (owner/name)")
    p_repo_issue.add_argument("--token", default=None, help="Provider token")
    p_repo_issue.add_argument("--provider", choices=("auto", "github", "gitlab", "bitbucket"), default="auto")
    p_repo_issue.add_argument("--title", required=True)
    body_group = p_repo_issue.add_mutually_exclusive_group(required=True)
    body_group.add_argument("--body", default=None)
    body_group.add_argument("--body-file", default=None)
    p_repo_issue.add_argument("--label", action="append", default=["fabri"])
    p_repo_issue.add_argument("--key", required=True, help="Stable deduplication key")
    p_repo_issue.set_defaults(func=cmd_repo_issue)

    p_repo_suggest = repo_sub.add_parser(
        "suggest-prompt", help="Open a deduplicated issue proposing promoted guidelines for the prompt")
    p_repo_suggest.add_argument("--config", required=True, help="Path to agent.yaml")
    p_repo_suggest.add_argument("--repo", default=None, help="Repository (owner/name)")
    p_repo_suggest.add_argument("--token", default=None, help="Provider token")
    p_repo_suggest.add_argument("--provider", choices=("auto", "github", "gitlab", "bitbucket"), default="auto")
    p_repo_suggest.add_argument("--kind", default="strategic")
    p_repo_suggest.add_argument("--limit", type=int, default=10)
    p_repo_suggest.add_argument("--label", action="append", default=["fabri", "self-improving"])
    p_repo_suggest.set_defaults(func=cmd_repo_suggest_prompt)

    p_repo_open_pr = repo_sub.add_parser(
        "open-pr", help="Open or update a draft PR that applies promoted prompt guidelines")
    p_repo_open_pr.add_argument("--config", required=True, help="Path to agent.yaml inside the repository")
    p_repo_open_pr.add_argument("--repo", default=None, help="Repository (owner/name)")
    p_repo_open_pr.add_argument("--token", default=None, help="Provider token")
    p_repo_open_pr.add_argument("--provider", choices=("auto", "github", "gitlab", "bitbucket"), default="auto")
    p_repo_open_pr.add_argument("--kind", default="strategic")
    p_repo_open_pr.add_argument("--limit", type=int, default=10)
    p_repo_open_pr.add_argument("--base", default="main")
    p_repo_open_pr.add_argument("--branch", default="fabri/self-improve")
    p_repo_open_pr.set_defaults(func=cmd_repo_open_pr)

    p_repo_run = repo_sub.add_parser(
        "run", help="Run a Linear issue end-to-end into a verified GitHub PR (10 fail-closed gates)")
    p_repo_run.add_argument("--from-linear", required=True, help="Linear issue id or identifier")
    p_repo_run.add_argument("--repo", required=True, help="Target repository (owner/name)")
    p_repo_run.add_argument("--base", default="main", help="Base branch")
    p_repo_run.add_argument("--config", default=None, help="Path to the agency entry yaml")
    p_repo_run.add_argument(
        "--test-cmd", default=None,
        help="Authoritative verify command (overrides env/agency.toml default)")
    p_repo_run.add_argument(
        "--setup-cmd", default=None,
        help="Optional setup command run in the clone before the crew")
    p_repo_run.set_defaults(func=cmd_repo_run)

    p_replay = sub.add_parser("replay", help="Re-run a past session's task with current memory state")
    p_replay.add_argument("session_id")
    p_replay.set_defaults(func=cmd_replay)

    # B3: discovery — list the registry's tools without grepping examples/.
    p_tools = sub.add_parser("tools", help="List available tools (builtin + configured manifest dirs)")
    p_tools.add_argument("--search", default=None,
                         help="Filter by case-insensitive substring over name+description")
    p_tools.add_argument("--manifest-dir", dest="manifest_dir", action="append", default=None,
                         metavar="DIR",
                         help="Resolve tools from this dir instead of tools.manifest_dir "
                              "(repeatable; use 'builtin' for the bundled tools)")
    p_tools.set_defaults(func=cmd_tools)

    p_tool = sub.add_parser("tool", help="Tool-related helpers (scaffold a new tool)")
    tool_sub = p_tool.add_subparsers(dest="tool_command", required=True)

    p_tool_init = tool_sub.add_parser("init", help="Scaffold a new tool")
    p_tool_init.add_argument("language", choices=SUPPORTED_LANGUAGES,
                             help="Language of the new tool's executable")
    p_tool_init.add_argument("name", help="Tool name (alphanumeric + underscore)")
    p_tool_init.add_argument("--dir", default="tools/agent_tools",
                             help="Where to write the manifest + executable (default: tools/agent_tools)")
    p_tool_init.add_argument("--force", action="store_true",
                             help="Overwrite existing files")
    p_tool_init.set_defaults(func=cmd_tool_init)

    # B2: tool-writer — scaffold a schema-tightened tool, validate it, test it.
    p_tool_new = tool_sub.add_parser(
        "new", help="Scaffold a schema-tightened tool from a signature or description")
    p_tool_new.add_argument("name", help="Tool name (alphanumeric + underscore)")
    p_tool_new.add_argument("--lang", choices=SUPPORTED_LANGUAGES, default="python",
                            help="Language of the executable stub (default: python)")
    p_tool_new.add_argument("--from-signature", dest="from_signature", default=None,
                            metavar="FILE.py",
                            help="Derive input/output schema + stub from the first "
                                 "top-level function in this Python file (no LLM)")
    p_tool_new.add_argument("--from", dest="from_desc", default=None, metavar="DESC",
                            help="Use this description; optionally enrich the schema "
                                 "via an LLM when an API key is set")
    p_tool_new.add_argument("--no-llm", action="store_true",
                            help="With --from, skip LLM enrichment (deterministic only)")
    p_tool_new.add_argument("--dir", default="tools/agent_tools",
                            help="Where to write the manifest + executable (default: tools/agent_tools)")
    p_tool_new.add_argument("--force", action="store_true", help="Overwrite existing files")
    p_tool_new.set_defaults(func=cmd_tool_new)

    p_tool_validate = tool_sub.add_parser(
        "validate", help="Validate a tool manifest (shape, schemas, script path)")
    p_tool_validate.add_argument("manifest", help="Path to the tool's <name>.json manifest")
    p_tool_validate.set_defaults(func=cmd_tool_validate)

    p_tool_test = tool_sub.add_parser(
        "test", help="Run a tool locally through the runner and print {ok, result?, error?}")
    p_tool_test.add_argument("name", help="Tool name (matches <name>.json in --dir)")
    p_tool_test.add_argument("--args", default=None,
                             help="JSON object of args to send on stdin (default: {})")
    p_tool_test.add_argument("--dir", default="tools/agent_tools",
                             help="Directory holding the tool manifest (default: tools/agent_tools)")
    p_tool_test.set_defaults(func=cmd_tool_test)

    # B3: tool run — positional-args cousin of `tool test`, for quick debugging.
    p_tool_run = tool_sub.add_parser(
        "run", help="Directly invoke a tool through the runner (debugging) and print {ok, result?, error?}")
    p_tool_run.add_argument("name", help="Tool name (matches <name>.json in --dir)")
    p_tool_run.add_argument("json_args", nargs="?", default=None, metavar="json-args",
                            help="JSON object of args to send on stdin (default: {})")
    p_tool_run.add_argument("--dir", default="tools/agent_tools",
                            help="Directory holding the tool manifest (default: tools/agent_tools)")
    p_tool_run.set_defaults(func=cmd_tool_run)

    p_new = sub.add_parser("new", help="Scaffold a new resource")
    new_sub = p_new.add_subparsers(dest="new_command", required=True)

    p_new_agency = new_sub.add_parser(
        "agency", help="Scaffold a working multi-agent agency")
    p_new_agency.add_argument("name", help="Directory name for the agency")
    p_new_agency.add_argument(
        "--template",
        choices=sorted(AGENCY_TEMPLATES),
        default="bug-crew",
        help="Agency template (default: bug-crew)",
    )
    p_new_agency.add_argument(
        "--from",
        dest="from_source",
        default=None,
        help="Registry source: a local path or gh:owner/repo/subpath[@ref]",
    )
    p_new_agency.add_argument(
        "--dest", default=".", help="Parent directory (default: current directory)")
    p_new_agency.set_defaults(func=cmd_new_agency)

    p_company = sub.add_parser("company", help="Compile a declarative multi-level company")
    company_sub = p_company.add_subparsers(dest="company_command", required=True)
    p_company_compile = company_sub.add_parser(
        "compile", help="Compile company.toml into nested agent configs")
    p_company_compile.add_argument("company_toml", help="Path to company.toml")
    p_company_compile.add_argument("--dest", default=".", help="Output parent directory (default: current directory)")
    p_company_compile.add_argument(
        "--run-from", dest="run_from", default=None,
        help="Anchor directory for durable memory DB paths (<run-from>/.fabri/; default: current directory)")
    p_company_compile.set_defaults(func=cmd_company_compile)

    # B5: prompt-kit — scaffold a new agent prompt from the proven skeleton.
    p_prompt = sub.add_parser("prompt", help="Prompt-related helpers (scaffold a new agent prompt)")
    prompt_sub = p_prompt.add_subparsers(dest="prompt_command", required=True)

    p_prompt_new = prompt_sub.add_parser(
        "new", help="Write a starter agent prompt from the prompt-kit skeleton")
    p_prompt_new.add_argument("name", help="Prompt/agent name (used for the role and default filename)")
    p_prompt_new.add_argument("--role", default=None,
                              help="Agent role for the CHARTER section (default: name)")
    p_prompt_new.add_argument("--output", "-o", default=None,
                              help="Output file (default: <name>.prompt.md)")
    p_prompt_new.add_argument("--force", action="store_true", help="Overwrite an existing file")
    p_prompt_new.set_defaults(func=cmd_prompt_new)

    # B1: ideator — one-sentence idea -> reviewable agent scaffold dir.
    p_ideate = sub.add_parser(
        "ideate", help="Draft a reviewable agent scaffold (agent.yaml + prompts + tool stubs) from a one-sentence idea")
    p_ideate.add_argument("idea", help="A one-sentence product idea, in quotes")
    p_ideate.add_argument("--out", default=None,
                          help="Output directory (default: ./<agent_name>-agent)")
    p_ideate.add_argument("--force", action="store_true",
                          help="Overwrite existing files / write into a non-empty dir")
    p_ideate.set_defaults(func=cmd_ideate)

    # B4: skills — install a reusable bundle (prompt + tools + config) into a project.
    p_skills = sub.add_parser(
        "skills", help="Discover, install, or author reusable skill bundles (prompt + tools + config)")
    skills_sub = p_skills.add_subparsers(dest="skills_command", required=True)

    p_skills_list = skills_sub.add_parser(
        "list", help="List discoverable skills (bundled examples + a project skills/ dir)")
    p_skills_list.add_argument("--dir", default=None,
                               help="Project skills directory to scan (default: ./skills)")
    p_skills_list.set_defaults(func=cmd_skills_list)

    p_skills_install = skills_sub.add_parser(
        "install", help="Install a skill: copy its tools + prompts and merge its config into agent.yaml")
    p_skills_install.add_argument("skill", help="Skill name (from `skills list`) or a path to a skill directory")
    p_skills_install.add_argument("--into", default=".",
                                  help="Project directory to install into (default: .)")
    p_skills_install.add_argument("--dir", default=None,
                                  help="Project skills directory to resolve a name from (default: ./skills)")
    p_skills_install.add_argument("--force", action="store_true",
                                  help="Overwrite existing tool/prompt files")
    p_skills_install.set_defaults(func=cmd_skills_install)

    p_skills_add = skills_sub.add_parser(
        "add", help="Scaffold a fresh skill skeleton to author")
    p_skills_add.add_argument("name", help="Skill name (alphanumeric + - or _)")
    p_skills_add.add_argument("--dir", default="skills",
                              help="Where to create the skill dir (default: skills)")
    p_skills_add.add_argument("--force", action="store_true", help="Overwrite existing files")
    p_skills_add.set_defaults(func=cmd_skills_add)

    p_report = sub.add_parser("report", help="Aggregate cost/outcome across recent sessions")
    p_report.add_argument("--since", default=None,
                          help="Only sessions in the last X (e.g. 7d, 24h, 30m). Default: all.")
    p_report.add_argument("--limit", type=int, default=None,
                          help="Cap to the N most recent sessions after --since.")
    p_report.add_argument("--format", choices=["md", "json", "html"], default="md",
                          help="Output format. md (default) is human-readable; html is self-contained.")
    p_report.add_argument("--output", "-o", default=None,
                          help="Write to this file instead of stdout (always used with --format html).")
    p_report.set_defaults(func=cmd_report)

    # Gated by require_admin() — stub shared-secret check (FABRI_ADMIN_TOKEN),
    # not real auth. See admin.py.
    p_admin = sub.add_parser("admin", help="Admin-only: inspect a config and its resolved tools/memory")
    p_admin.add_argument("--admin-token", dest="admin_token", default=None)
    admin_sub = p_admin.add_subparsers(dest="admin_command", required=True)

    p_admin_config = admin_sub.add_parser("config", help="Print the merged config + resolved tool registry as JSON")
    p_admin_config.set_defaults(func=cmd_admin_config)

    p_admin_dash = admin_sub.add_parser("dashboard", help="Human-readable summary: agent, tools, memory counts")
    p_admin_dash.set_defaults(func=cmd_admin_dashboard)

    p_traces = sub.add_parser("traces", help="Inspect JSONL traces under $FABRI_HOME/traces (homegrown observability spine)")
    traces_sub = p_traces.add_subparsers(dest="traces_command", required=True)

    p_traces_show = traces_sub.add_parser("show", help="Pretty-print a session's trace")
    p_traces_show.add_argument("session_id")
    p_traces_show.set_defaults(func=cmd_traces_show)

    p_traces_tail = traces_sub.add_parser("tail", help="Follow a session's trace as it's written")
    p_traces_tail.add_argument("session_id")
    p_traces_tail.set_defaults(func=cmd_traces_tail)

    p_traces_list = traces_sub.add_parser("list", help="List recent session traces")
    p_traces_list.add_argument("--limit", type=int, default=20)
    p_traces_list.set_defaults(func=cmd_traces_list)

    p_traces_export = traces_sub.add_parser(
        "export", help="Export a session's trace to the configured OTLP backend (Langfuse/Honeycomb/…)")
    p_traces_export.add_argument("session_id")
    p_traces_export.set_defaults(func=cmd_traces_export)

    # B7: embeddable service — a non-Python host submits a task over HTTP and
    # streams events without importing fabri.
    p_serve = sub.add_parser(
        "serve", help="Start the embeddable HTTP service (POST a task, stream events + cost over SSE)")
    p_serve.add_argument("--config", default=None,
                         help="Template agent.yaml; per-run overrides deep-merge onto it.")
    p_serve.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=8080, help="Bind port (default: 8080; 0 = OS-assigned)")
    p_serve.add_argument("--home-root", dest="home_root", default=None,
                         help="Parent dir for per-run FABRI_HOME workspaces (default: $FABRI_HOME/serve or ~/.fabri/serve)")
    p_serve.set_defaults(func=cmd_serve)

    p_studio = sub.add_parser(
        "studio",
        help="Start Fabri Studio and its run API (default port 8080; collides with `fabri serve`)",
    )
    p_studio.add_argument(
        "--config",
        default=None,
        help="Template agent.yaml; per-run overrides deep-merge onto it.",
    )
    p_studio.add_argument(
        "--company",
        default=None,
        help="Company TOML to compile and serve as the Studio run template.",
    )
    p_studio.add_argument(
        "--catalog",
        default=None,
        help="Roster directory to pre-install and serve; takes precedence over --config/--company.",
    )
    p_studio.add_argument("--host", default="127.0.0.1",
                          help="Bind host (default: 127.0.0.1)")
    p_studio.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Bind port (default: 8080; collides with `fabri serve`; 0 = OS-assigned)",
    )
    p_studio.add_argument(
        "--home-root",
        dest="home_root",
        default=None,
        help="Parent dir for per-run FABRI_HOME workspaces (default: $FABRI_HOME/serve or ~/.fabri/serve)",
    )
    p_studio.set_defaults(func=cmd_studio)

    args = parser.parse_args()
    try:
        args.func(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
