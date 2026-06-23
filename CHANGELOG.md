# Changelog

All notable changes land here, newest first. Versions follow PyPI
immutability: never reuse a version number; cut a new one for any change
that ships.

## v0.5.1 — 2026-06-23

Resilience: a single oversized turn no longer nukes a whole run, and qdrant
reachability propagates across the subprocess boundary in containerized hosts.
All changes additive/non-breaking.

### Added

- **Retry once on a `max_tokens` truncation before failing the run.** A single
  content-heavy turn (e.g. writing several files at once) previously hard-failed
  the entire multi-step run via `LLMError`. Both the Anthropic and OpenAI
  backends now retry that one step once at a higher cap (`min(max_tokens * 2,
  MAX_TOKENS_RETRY_CEILING)`, where the ceiling is 16000 — a non-streaming-safe
  bound) before giving up. We still fail loud if even the retry truncates, and
  never report a truncated answer as success. The discarded truncated attempt's
  tokens are folded into the reported `LLMUsage` so per-run cost stays accurate.
- **`QDRANT_URL` env override in `load_config`.** When `QDRANT_URL` is set in the
  environment, it wins over `memory.qdrant_url` from the yaml. A containerized
  host sets it once on the service; the orchestrator, the `spawn_subagent` tool,
  and every spawned child sub-agent inherit the env, so the reachable qdrant
  address (e.g. `qdrant:6333`) propagates across the subprocess boundary without
  rewriting each on-disk config. Fixes child sub-agents dying on connect when
  spawned with a `config_path` pointing at a repo yaml that still says
  `localhost:6333` (unreachable in-container). Never mutates the shared
  `DEFAULT_CONFIG`.

### Tests

- **+105 tests** (pricing edge cases, cost rollup across mixed/unknown models and
  sub-agent subtrees, both LLM backends incl. truncation-retry / prewarm /
  model-tagging / cache folding, the `QDRANT_URL` override, system-prompt
  frugality gating, and `spawn_subagent` command plumbing). Suite 246 → 351.

## v0.5.0 — 2026-06-23

Per-run COGS (USD cost) with sub-agent rollup, a frugal-by-default base prompt,
and cache pre-warming. All changes are additive/non-breaking: the `usage` event
and `run_agent` return gained fields; existing token fields, tool contracts, and
outcome semantics are unchanged.

### Added

- **Per-run USD cost (COGS).** `LLMUsage` gained a `model` field (filled by both
  the Anthropic and OpenAI backends). New `fabri.pricing` module prices token
  usage per model (Sonnet 4.6, Haiku 4.5, Opus tier, gpt-4o; cache-write 1.25×,
  cache-read 0.10×; prefix-matches date-suffixed ids). `run_agent`'s `usage`
  trace event AND its return dict now carry `cost_usd` (this run's own tokens),
  `cost_by_model`, `subagent_cost_usd`, and `total_cost_usd`. An unknown/absent
  model prices to `None` (never a misleading 0).
- **Sub-agent cost rollup.** `agent_runner_tool` / `spawn_subagent` now return
  the child's `usage`; the parent's dispatch loop rolls each child's
  `total_cost_usd` into `subagent_cost_usd`, so a parent's `total_cost_usd` is
  the true end-to-end cost of itself **plus its whole sub-agent subtree**.
  Previously a sub-agent ran as a separate subprocess with its own trace, so its
  tokens were invisible to the parent and a fan-out run was massively
  undercounted. `_dispatch_tool_calls` gained an optional keyword-only
  `on_subagent_cost` callback (existing direct callers are unaffected).
- **Cache pre-warm.** `AnthropicLLMBackend.prewarm(system)` writes the static
  system+tools prefix into Anthropic's ephemeral cache via a `max_tokens=0`
  request and returns the call's `LLMUsage` (no-op on the scripted/OpenAI
  backends). Trims first-call latency; the cache-write itself is paid once
  either way, so fire it before a burst of same-prefix runs, not on a 24/7 loop.

### Changed

- **Frugal-by-default base prompt.** `DEFAULT_AGENT_IDENTITY` is now
  deliberation-first. A `FRUGALITY_POLICY` is appended to **every** run (even
  when a domain config replaces the identity wholesale), plus registry-gated
  `DELEGATION_POLICY` (only when `spawn_subagent` is in the registry) and
  `CODE_ACTION_POLICY` (only when `python_exec`/`batch` is present). Together
  they steer the agent toward decisive calls over exploratory probing,
  single-threaded-by-default delegation, and code-as-action — grounded in
  CodeAct (arXiv:2402.01030), ReWOO (arXiv:2305.18323), Anthropic's multi-agent
  engineering post, and Cognition's *Don't Build Multi-Agents*.
- **`spawn_subagent` tool description** rewritten to gate delegation
  ("EXPENSIVE … spawn ONLY when a subtask is independent, parallelizable, and
  too large for your own context") and to document the new `usage` return field
  whose `total_cost_usd` rolls the subtree's cost up to the parent.

## v0.4.6 — 2026-06-22

### Changed

- **README is now self-contained for PyPI.** Previous versions linked to
  `docs/creating-an-agent.md` in the repo; the README now inlines the
  full config schema, tool manifest contract, agents-as-tools snippet,
  and library-usage example so PyPI readers don't depend on repo file
  visibility.

## v0.4.5 — 2026-06-22

### Changed

- **Tool-name word-boundary regexes are cached.** `retrieval._word_mentioned`
  was compiling `re.compile(rf"\b{name}\b", IGNORECASE)` on every retrieval
  call for every registered tool; the compiled patterns are now cached
  process-wide. Pure perf, no behaviour change.
- **`run_agent` skips materialising a `range(max_steps)` list when the
  planner already ran.** The legacy single-loop iterator is now a `range`
  rather than `list(range(...))`, and is empty when `plan_engaged` is true.
- **Inline-reasoning emit is centralised.** Four near-identical copies of
  the `thought` event-log block (executor / legacy × tool_calls / final_text
  branches) collapse to one `_emit_thought()` helper inside `run_agent`.

### Added

- **Protocol-error events now carry `had_tool_failure`.** When the LLM
  returns no tool calls and no usable final text, the `error` event and
  the paired log line include whether any prior tool call in the same
  item/run failed -- distinguishing "model is broken" from "model gave up
  after a cascade of tool failures" in post-hoc trace analysis.

## v0.4.4 — 2026-06-21

### Added

- **`spawn_subagent` accepts `memory_collection_suffix`.** Multi-domain
  orchestrators can now namespace each child's qdrant collection without
  cloning the spawned sub-agent's yaml. When set, the child writes to
  `<parent_collection>_<suffix>` (parent collection is read from the
  sub-agent's `memory.collection`); omitted/empty keeps the inherited
  behavior. The suffix is sanitized to lowercase `[a-z0-9_-]` and capped at
  32 chars so a host passing `Tile/Map.v2` doesn't fail with an opaque
  qdrant error. Hosts like ludexel can spawn `<parent>_character` vs
  `<parent>_map` from the same prompt template so cross-domain guidelines
  don't crowd each child's retrieval.
- **`EventType.DISCREPANCY` + `fabri.events.emit_discrepancy(...)` helper.**
  Hosts that post-hoc detect drift between what an agent claimed it did and
  what actually landed in their store now have a first-class trace event to
  emit (`{type: "discrepancy", path, reason}`). `process_trace` mines each
  discrepancy into a tactical guideline ("After write_file/edit_file at
  `<path>`, re-read the file in the same step to confirm the write
  persisted.") that flows through the existing dedup/promotion pipeline.
  `fabri traces show`/`tail` recognize the new event so it prints as a
  readable line, not the catch-all JSON fallback.



### Fixed

- **Silenced HuggingFace / sentence-transformers chatter on every run.** The
  embedding model used by memory retrieval (`all-MiniLM-L6-v2`) was leaking
  a tqdm `Loading weights` bar and an "unauthenticated requests to the HF
  Hub" warning to stderr on every `fabri run`. `memory/embeddings.py` now
  sets `HF_HUB_DISABLE_PROGRESS_BARS` / `TRANSFORMERS_VERBOSITY=error` /
  `HF_HUB_DISABLE_TELEMETRY` / `TOKENIZERS_PARALLELISM=false` **before**
  importing `sentence_transformers`, and pins the relevant loggers to
  WARNING/ERROR. A single `fabri` info line ("loading embedding model …")
  fires only on the very first download; cached loads are silent.
- **Skip the embedder entirely on a cold memory store.**
  `orchestrator/retrieval.py::retrieve_context` short-circuits when the
  Qdrant store has zero entries, so a fresh `fabri init` + first run never
  has to load the 44MB embedding model.

### Changed

- **`fabri traces show` / `tail` rendering.** Every event now carries an
  `HH:MM:SS (+Δs)` wallclock prefix (time "just works" by default, no
  flag). `thought` events render their full body — no 120-char truncation
  — with JSON pretty-printed and code-like blocks under a `┃` gutter.
  `tool_call` prints pretty-printed `args` and `result` payloads (capped
  at 40 lines; full payload still in the JSONL). `step_started` /
  `step_finished` get `── step N ──` separators. `llm_error` / `failed`
  print the full reason (no truncation).

## v0.4.2 — 2026-06-21

### Fixed

- **`ToolRegistry` import crash on annotation introspection.** The v0.4.0
  `invoke_batch(self, calls: list[dict])` signature shadowed `list` against
  the existing `ToolRegistry.list()` method — under PEP 649 deferred
  annotations, any consumer that touched `__annotations__` /
  `inspect.signature` / `typing.get_type_hints` on the class hit
  `TypeError: 'function' object is not subscriptable`. Fix:
  `from __future__ import annotations` at the top of `tools/registry.py`
  so all annotations stay as strings and the lookup never resolves
  `list` against the method. Public API unchanged (`registry.list()`
  still works).

## v0.4.1 — 2026-06-21

PyPI metadata polish: package description rewritten to surface the A1–A5
capabilities (planner/executor, retrieved tools, batch, success-pattern
mining, usage events) alongside the v0.3.x feature set. No code changes.

## v0.4.0 — 2026-06-21

Token-optimization series A1–A5 (planner/executor split, retrieved tool
descriptions, batch tool, success-pattern mining, per-run usage event). All
changes are non-breaking: defaults preserve the v0.3.0 behaviour and the new
paths are opt-in via `agent.planner.*`, `tools.retrieval.*`, or by listing
`batch` in `tools.enabled`.

### Added

- **Per-run `usage` event (A5).** `run_agent` now accumulates per-call
  input/output/cache-creation/cache-read token totals across the loop and
  emits a `usage` trace event at run end, alongside the existing
  `final` / `failed` / `incomplete` event. The same fields are returned in
  the `run_agent` result dict under `usage` (plus `step_count` and
  `wall_time_s`) so host services can persist per-run cost without parsing
  stderr logs. `LLMResponse` gained an optional `LLMUsage` carrier; the
  Anthropic and OpenAI backends fill it; `ScriptedLLMBackend` leaves it
  `None` and totals stay zero.

- **Retrieved tool descriptions (A1).** New
  `orchestrator.retrieval.retrieve_tools(task, registry, top_k, always_include)`
  ranks a registry's tools by cosine similarity of their descriptions to the
  task. When `tools.retrieval.enabled: true` is set in the config,
  `run_agent` narrows both the `Available tools:` block in the system prompt
  AND the provider's `tools=` list (via a new `LLMBackend.set_tools()`) to
  the top-K + an always-include set (defaults: `spawn_subagent`, `ask_user`,
  `decompose`). The filtered subset is fixed for the whole run so the v0.3.0
  prompt cache still hits across steps. Per-tool description vectors are
  cached at module scope so re-runs don't re-embed every tool.

- **Planner / executor split (A2).** New `core/planner.py` exports
  `plan(task, llm, max_items)` and `PlanItem`. `run_agent` gained
  `planner_mode: "off" | "auto" | "force"` (default `off` for back-compat),
  a `planner_llm` argument (with the historical `decompose_llm` kept as a
  fallback), and `planner_max_items` / `planner_auto_token_threshold`. When
  the planner is engaged, the executor runs one step-loop per plan item in
  dependency-resolved order with a minimal per-item user message ("current
  goal + artefacts + previously completed"), so each item pays only its own
  share of the prompt instead of the full accumulated history. New trace
  events: `plan_started`, `plan_item_started`, `plan_item_finished`,
  `plan_finished`. Configurable via `agent.planner.{enabled, mode,
  max_items, auto_token_threshold}`.

- **`batch` tool (A3).** A new built-in tool that takes
  `{"calls": [{"name": "...", "args": {...}}, ...]}` and dispatches each
  inside the registry process, collapsing the common
  "validate -> schema_check -> xref_check -> generator_dryrun" verification
  ladder from N model round-trips to one. Nested `batch` calls and
  side-effecting meta-tools (`spawn_subagent`, `ask_user`) are refused with
  a clear per-entry error rather than silently dispatched. Default off;
  opt-in by listing `batch` in `tools.enabled`.

- **Success-pattern mining (A4).** `process_trace` now also mines a "what
  worked" guideline from every run that ended with a `final` event and at
  least one ok=true tool call, ingesting it under a new
  `kind: "success_pattern"`. `MemoryEntry.id` is now namespaced by kind
  (success vs failure) so a success_pattern can't collide with a textually
  similar failure-derived guideline. `retrieve_context` reserves up to
  `top_k // 2` slots for success patterns so they survive even when a flood
  of failure-derived guidelines would otherwise drown them at retrieval.

## v0.3.0 — 2026-06-21

Token-optimization for file-generating agents. All changes are non-breaking:
existing configs and tool manifests keep working unchanged; the new behaviour
is opt-out (caching) or opt-in (read_file windowing/outline).

### Added

- **Anthropic prompt caching on the static prefix.**
  `AnthropicLLMBackend` now wraps the system prompt as a `cache_control:
  ephemeral` text block and tags the last entry in the tool list with the
  same marker — Anthropic caches every block at and before the marker, so
  the system prompt + tool descriptions are billed at ~10% of full cost on
  cache hits. The constructor accepts `enable_prompt_cache: bool = True` so
  cost-sensitive or test runs can opt out. `cache_creation_input_tokens` and
  `cache_read_input_tokens` are now logged on every call so cache wins are
  visible in run traces.

- **`read_file` supports windowed reads and structural outlines.** New
  optional args `line_start` / `line_end` (1-indexed, inclusive) return a
  slice with `start_line`, `end_line`, `total_lines`, `truncated`. New
  `outline_only: true` returns the file's top-level structure (def/class/
  heading/CONSTANT lines plus line numbers) for fast navigation before a
  targeted window read. Whole-file reads (no args) keep their pre-change
  output shape so every existing consumer is unaffected.

### Changed

- **Default agent identity steers toward `edit_file` over `write_file`.**
  When both tools are present in the registry, the system prompt now
  appends a `FILE_EDIT_POLICY` block telling the model to prefer surgical
  string-replace edits over whole-file rewrites, and to read file windows
  rather than whole files. The hint is registry-aware: it's skipped when
  the agent doesn't actually have `edit_file` available. This is the
  highest-ROI output-token cut for Ludexel-style file-gen workloads.

## v0.2.3 — 2026-06-21

### Fixed

- **`fabri run` now exits non-zero on a non-succeeded outcome.** When
  the agent ran out of steps, hit a provider error (rate limit, 5xx,
  malformed response), or produced no final answer, `cmd_run` was
  still returning silently — meaning the process exited 0 even though
  the trace was full of `failed` events. Host services dispatching on
  the exit code (like ludexel's run record) wrote the run as succeeded
  and lost the failure cause. Now: `sys.exit(1)` when
  `result["success"]` is False or `result["outcome"] != "succeeded"`,
  after the trace ingestion side-effects have run.

## v0.2.2 — 2026-06-21

Non-breaking: existing trace consumers ignore the new event kind, and
LLMResponse gains an optional field that defaults to None.

### Added

- **Agent reasoning surfaces in the trace.** When Claude returns a
  response with both `text` content blocks AND one or more `tool_use`
  blocks in the same turn, the inline reasoning text was previously
  dropped on the floor — `AnthropicLLMBackend.step` only captured the
  tool_use blocks. Now the text is captured onto
  `LLMResponse.thinking_text` and the agent loop emits a
  `{"type": "thought", "text": ..., "step": N}` event in the trace
  BEFORE the matching `tool_call` events. Host UIs can render the
  thought as the "Let me check existing characters first…" reasoning
  context that precedes the tool dispatch. Pure final responses
  unchanged (text still becomes `final_text`).

## v0.2.1 — 2026-06-20

Burns down the rest of Tracks F, S, and A from `docs/ROADMAP.md`. Nothing
breaking; consuming projects that already work on v0.2.0 keep working.

### Added

- **F1 — dynamic `spawn_subagent` builtin.**
  `src/fabri/tools/examples/spawn_subagent.{py,json}`. Parent agents now
  pick the sub-agent config at runtime, rather than the static
  `tools.agents[]` form where the choice is pre-baked at config load.
  Shells out to the same `agent_runner_tool.py` the static F0 path uses,
  so the subprocess contract is identical:
  `{final_text, outcome, session_id, trace_path}`. Input schema:
  `{config_path, task, system_prompt_inline?, system_prompt_path?,
  additional_context?, parallel_group?, timeout_s?}`.
- **F1 — runner system-prompt overrides.**
  `agent_runner_tool.py` gains `--system-prompt` / `--system-prompt-file`
  (mutually exclusive). Parents can override a sub-agent's configured
  prompt per call without editing its yaml.
- **A1 — `ask_user` builtin.**
  `src/fabri/tools/examples/ask_user.{py,json}`. Blocks on a clarifying
  question routed to the host via a Unix socket (production) or stdin
  (CLI dev). Question IDs make the socket transport safe for concurrent
  sub-agents — a misrouted reply errors instead of being silently
  accepted.
- **A1 — runner `--ask-user-socket=<path>`.**
  Available on `agent_runner_tool.py` and `fabri run`. Sets
  `FABRI_ASK_USER_SOCKET` in `os.environ`; tools inherit it directly, so
  no registry plumbing was needed (unlike `FABRI_SANDBOX_ROOT`, which is
  per-registry).
- **S1 — `fabri.sandbox` package.**
  `Sandbox` ABC with `run_tool` / `sync_in` / `sync_out` / `dispose`.
  `LocalSandbox` lifts today's `$FABRI_SANDBOX_ROOT`-based behavior into
  an object. `ToolRegistry` now routes every invoke through
  `self.sandbox.run_tool`; defaults to `LocalSandbox` so configs that
  never name a sandbox see no behavior shift.
- **F2 — parallel-aware dispatch.**
  `core/agent.py` indexes `spawn_subagent` calls by `parallel_group` and
  fans them out via `ThreadPoolExecutor`. Other tool kinds, and
  ungrouped spawn calls, stay serial. Assistant/user message blocks
  preserve original call order so the Anthropic API contract holds.
  `tool_call` trace events for parallel calls carry the `parallel_group`
  field for trace-tail viewers.
- **S2 — `DockerSandbox` + `Dockerfile.base`.**
  Pooled warm-container backend. Lazy fill on first acquire. Shells out
  to the `docker` CLI rather than depending on docker-py. State
  ferrying intentionally deferred to host-injected `sync_in_hook` /
  `sync_out_hook` callbacks — the framework owns container plumbing;
  consumers own data plumbing. `Dockerfile.base` ships under
  `src/fabri/sandbox/` and is included in `package-data` so an
  installed wheel can build `fabri/sandbox:latest` directly.
- **F5a — `fabri --version`.**
  Argparse `action="version"` reads installed wheel metadata via
  `importlib.metadata.version("fabri")`. No constant to drift out of
  sync with `pyproject.toml`.
- **`fabri traces` subcommand.**
  Homegrown observability spine (no Langfuse / Agnost SDK dep).
  `traces show <session_id>` pretty-prints a JSONL trace with relative
  timestamps and `parallel_group` tags; `traces tail <session_id>`
  follows a trace file like `tail -f`; `traces list` sorts recent
  sessions under `$FABRI_HOME/traces` by mtime.

### Changed

- `ToolRegistry.invoke` routes tool subprocesses through
  `self.sandbox.run_tool` instead of calling `tools.runner.run_tool`
  directly. Default sandbox is `LocalSandbox`, so the runner-level
  behavior is unchanged for callers who don't pass a sandbox.

### Tests

- Suite grew from 156 to 191 (35 new tests across F1, A1, F2, S1, S2).
- F2 timing tests use `_dispatch_tool_calls` directly to bypass the
  embedding-model warm cost in `run_agent`, so the concurrency
  assertions don't false-fail on a cold cache.
- `S2` ships a `FakeBackend` for unit tests and one real-Docker
  integration test that auto-skips when `docker info` fails (CI without
  Docker-in-Docker).

### Backlog remaining

- **F5b** — docs: builtin list + worked `spawn_subagent` recipe in
  README + `docs/creating-an-agent.md`. The features it would document
  are all shipped; this is a quality-of-life follow-up, not a blocker.

## v0.2.0 — 2026-06-20

First PyPI release. Burns down the entire P0+P1+P2 backlog from
`TODO.md` (correctness/security audit), plus the F0 sub-agent
ergonomics. See `TODO.md` and the v0.2.0 release notes (#1) for the
full list. Highlights:

- F0: per-`tools.agents[]` overrides (`model`, `max_tokens`,
  `qdrant_url`, `memory_collection`); `llm.decompose_model` for
  cheap-model decomposition; sub-agents return `{session_id,
  trace_path}` so parent traces point straight at failing children.
- TOON-encoded tool results to cut LLM token cost.
- Anthropic + OpenAI backends fully round-trip parallel `tool_use` /
  `tool_result` blocks.
- `max_tokens` truncation, empty LLM response, and API errors all
  surface as live outcomes instead of silent SUCCESS.
- Memory dedup matches across `tactical` + `strategic` kinds.
- Sandbox tools fail closed when `FABRI_SANDBOX_ROOT` is unset.
- Bundled tool manifests packaged via
  `[tool.setuptools.package-data]`.

## v0.1.0 — pre-release

Initial scaffold under the `agent_memory` name. Renamed to `fabri`
before any external consumer existed (R1). No published artifact —
`v0.2.0` is the first wheel on PyPI.
