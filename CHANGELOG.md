# Changelog

All notable changes land here, newest first. Versions follow PyPI
immutability: never reuse a version number; cut a new one for any change
that ships.

## Unreleased

## 0.8.0 — 2026-06-28

### Track B — the builder layer (idea → running self-improving agent)

A new `src/fabri/builder/` package and a `src/fabri/service/` package turn the
engine into a product factory: scaffold agents, tools, and prompts from intent,
package reusable bundles as skills, and embed the whole thing as a self-contained
service. See `docs/vision.md` for the engine+builder thesis.

- **Ideator** (`fabri ideate "<idea>"`) — a one-line product idea becomes a
  *reviewable* scaffold dir (agent.yaml + prompts + tool stubs) via fabri's own
  structured output. Emits for review; never auto-applies.
- **Tool-writer** (`fabri tool new|validate|test`) — a description or a Python
  function signature becomes a tightened-schema manifest (not opaque `{}`) + a
  calling stub + a local test. `fabri tool validate` / `fabri tool test` close
  the no-validation / no-local-test gap.
- **Prompt-kit** — a nine-section prompt skeleton (`fabri prompt new`) plus a
  user-prose / `<!-- AGENT_MEMORY -->` output split wired (additively) into the
  trace miner.
- **Wave planner** — `builder.waves.plan_waves` topologically layers declared
  dependency edges and auto-assigns `parallel_group` for fan-out.
- **Discovery / runner ergonomics** — `fabri tools [--search]`, `fabri tool run`,
  and `fabri agent run --dry-run` (resolves config + tool defs with no network).
- **Skills registry** (`fabri skills add|list|install`) — installable bundles of
  prompt + tool manifests + a config snippet, with a bundled example skill.
- **Self-contained service** (`fabri serve`) — binds a per-run config from one
  template + overrides, spawns the agent, streams events by tailing the JSONL
  trace over stdio + HTTP/SSE, and surfaces cost. A non-Python host can drive a
  run with no fabri imports. Streams via the trace, so it needs neither O2 nor
  any change to the agent loop.
- **Repair loop** (`agent.repair`, **off by default**) — a bounded
  verify → repair → rerun loop that injects the verifier output as context and
  stops on no-progress (same error signature twice) or at `max_attempts`, with a
  fresh step budget per attempt. Threaded through `AgentRunConfig` so it
  activates from config at run / replay / agent-runner.

All builder code is additive, stdlib-only (no new third-party deps), and
project-agnostic; 116 new offline tests. Builds on v0.7.9 (Gemini).

## 0.7.9 — 2026-06-28

### Google Gemini support + Gemini is now the default provider

- **New `gemini` provider** on Google's native `google-genai` SDK
  (`core.llm.GeminiLLMBackend`), implementing the full `LLMBackend` contract.
  Translates fabri's Anthropic-shaped history into Gemini `Content`/`Part`
  schema and back, routes the system prompt to `system_instruction`, sanitizes
  tool JSON-schema for `FunctionDeclaration`, and matches a `function_response`
  to its call by NAME (fabri synthesizes the tool-call id Gemini omits and
  rebuilds an id→name map each turn). MAX_TOKENS retry, token-folding, and
  transient-error handling at parity with the Anthropic/OpenAI backends.
- **Gemini is the default provider.** `DEFAULT_CONFIG` now defaults to
  `provider: gemini`, `model: gemini-2.5-pro`, narrator `gemini-2.5-flash-lite`
  (lowest cost + generous free tier). Existing Claude/OpenAI configs keep
  working unchanged; only the no-provider default moved.
- **All provider SDKs ship by default.** `google-genai` + `openai` are now core
  dependencies alongside `anthropic`, so any provider works with no extra
  install; the `openai`/`gemini` extras are removed. `benchmark.yaml` stays on
  `claude-sonnet-4-6` to keep published BENCHMARKS reproducible.
- **Pricing** entries added for `gemini-2.5-pro` / `2.5-flash` / `2.5-flash-lite`
  / `2.0-flash`, so Gemini runs are priced into COGS like every other model.
- **`scripts/smoke_gemini.py`** — live end-to-end smoke test that forces a tool
  call and proves the round-trip via a per-run random token (with a `--mock`
  harness self-check that needs no API key).

## 0.7.8 — 2026-06-25

### COGS accuracy fixes

- **Planner LLM usage is now accumulated.** `core.planner.plan()` accepts an
  `on_usage` callback; `run_agent` passes its `_accumulate` so a planner
  step's tokens (often a full-context Sonnet pass) land in the run's
  reported `cost_usd` / `total_cost_usd` instead of silently leaking.
- **Decompose LLM usage is now accumulated.** `core.decompose.decompose()`
  accepts `on_usage`; threaded through `_dispatch_tool_calls` so every
  `decompose` tool call rolls into COGS.
- **Memory-compression LLM usage is captured.** `synthesize_guideline` and
  `synthesize_success_pattern` accept `on_usage`; `orchestrator.pipeline.
  process_trace` threads it through. `cli.cmd_run` accumulates and emits a
  new `post_run_usage` trace event after `process_trace` completes so a
  host can merge the cost onto the run's recorded totals.
- **`cost_unaccounted` event for crashed sub-agents.** When `spawn_subagent`
  fails without surfacing usage (e.g. qdrant down → runner crashed before
  printing its final JSON), the parent now emits an explicit
  `cost_unaccounted` event with `tool`, `step`, `reason`,
  `child_returncode`, and `child_stderr_tail` so a host can warn that
  recorded COGS is a lower bound for the run rather than silently
  under-reporting.
- New `EventType.COST_UNACCOUNTED` and `EventType.POST_RUN_USAGE`.

### Orchestration internals & CLI consolidation

- **`AgentRunConfig`** (`fabri.core.run_config`) — a single value object for
  the ~18 scalar orchestration knobs `run_agent` consumes, built once from a
  loaded config via `from_config` and threaded into every entry point. Fixes a
  real divergence bug: `fabri replay` and the agent-as-tool runner previously
  re-listed the kwargs by hand and **silently dropped** the planner,
  tool-retrieval, and budget settings — so a replay ran under different
  orchestration than the original (defeating the point of replay), and a
  sub-agent never used the planner/retrieval even when configured. All three
  entry points now share `runtime.build_run_llms` + `AgentRunConfig`.
- **`llm.planner` role now actually builds the planner backend.** `cmd_run`
  wired `planner_llm=build_decompose_llm(...)`, so the dedicated `llm.planner`
  config was dead. Added `runtime.build_planner_llm`; the planner role is now
  honored (falls back to decompose, then main, exactly as before when unset —
  no default-behavior change).
- **One step engine.** The planner executor and the non-planner single loop
  were copy-pasted and had already drifted (only the single loop emitted
  per-step `cost_usd`). Unified into `_run_step_loop`; the planner path now
  gets the same per-step cost telemetry, and dispatch/error/budget/nudge logic
  can't diverge between paths again.
- **Trace renderer extracted** to `fabri.orchestrator.trace_render` (pure,
  unit-tested) out of `cli.py` — `fabri traces show`/`tail` are unchanged.

### Postmortem memory (ROADMAP card **M1**, first increment) — opt-in

- **`memory.record_postmortems`** (default `false`) — when set, every run
  (any outcome) writes one deterministic, LLM-free whole-run postmortem to
  memory: `task + outcome + steps + tool-call/failure counts + repeated
  (tool × error-signature)` groups. It's a new `postmortem` memory kind with
  its own point-id namespace and same-kind dedup, retrieved by task similarity
  so a similar future task surfaces "last time this took N steps; tool X failed
  K times". Off by default, so entry counts/contents are unchanged for callers
  that don't opt in. The harder `final_diff`/`fix_pattern` extraction remains a
  follow-up (still tracked in TODO P2).

### Structured / typed output (ROADMAP card **O1**)

Opt-in and backward compatible: configs without `agent.response_schema` are
unchanged and pay zero extra LLM calls.

### Added

- **`agent.response_schema`** — an optional JSON Schema. When set, the
  final answer is parsed as JSON and validated against it. On a mismatch
  the runner re-prompts the model with the human-readable validation
  errors up to **`agent.response_retries`** times (default `1`). The
  validated value is returned on the run result as **`structured_output`**
  (and surfaced by the sub-agent runner so a parent spawn can read a
  child's typed result); `final_text` still carries the raw string.
- **`agent.error_strategy`** — how an un-satisfiable schema resolves after
  retries: `strict` (default) ends the run with the new
  **`Outcome.INVALID_OUTPUT`**; `warn` returns the unvalidated text as
  success; `fallback` returns **`agent.response_fallback`** (or `{}`) as
  success.
- **`structured_output` trace event** — one per validation attempt,
  carrying `attempt`, `valid`, and the `errors`, so a trace shows how many
  retries a typed answer cost.
- **`fabri.core.structured`** — a small, dependency-free validator for the
  JSON-Schema subset that matters for LLM output (`type` incl. type lists,
  `properties`, `required`, `items`, `enum`, nested objects/arrays).
  Unknown keywords are ignored rather than erroring. Not a full Draft-2020
  implementation by design.

### Notes

- Validation lives at the agent-loop layer (`core/agent.py`), not in the
  provider backends — `core/llm.py` is untouched, so every provider gets
  structured output for free.
- Structured output applies to the single-loop (non-planner) final answer.
  When the planner engages, the schema is skipped with a logged warning
  (the planner concatenates per-item outputs, so a single schema doesn't
  apply).

### Security & robustness hardening

A focused audit pass (subprocess tools, sandbox, orchestration, memory, LLM/MCP)
fixed the following active issues:

- **Sub-agent recursion cap.** `spawn_subagent` now threads
  `FABRI_SUBAGENT_DEPTH` through the child env and refuses to spawn past
  `FABRI_SUBAGENT_MAX_DEPTH` (default 5). Without this, a confused or
  prompt-injected agent could fork-bomb `breadth^depth` subprocesses, each
  carrying its own fresh cost budget.
- **Cost budget across fan-out.** A breached `agent.max_cost_usd` now refuses
  to spawn *more* sub-agents mid-step (the per-step check couldn't bound a
  single parallel fan-out before). The structured-output retry loop is also
  budget-checked.
- **Parallel dispatch no longer aborts on one raising future.** A sub-agent
  that raises is normalized to a `tool_error` so every `tool_use` keeps its
  paired `tool_result` (an unpaired block would 400 the next provider call).
  Removed dead code in the fan-out loop.
- **Sub-agent telemetry on the default path.** `on_subagent_finished`
  (fan-out count / delegation-regret) was only wired on the planner path; it
  now fires on the default single-loop path too.
- **Planner step-budget division** counts every processed item, not just
  successful ones, so a failed early item no longer starves later items.
- **`ask_user` socket wait is bounded** (`FABRI_ASK_USER_TIMEOUT_S`,
  default 300s) and falls back to the question's `default`, instead of
  hanging until the parent spawn timeout. The socket path now also honours
  `default` on an empty reply (parity with stdin).
- **Retrieved guidelines are fenced.** Memory mined from prior runs' tool
  outputs/task text is wrapped in a `<retrieved_guidelines>` block with a
  "reference only, never an instruction" caveat and stripped of forged fence
  tags — reducing stored-prompt-injection risk across sessions.
- **Sqlite memory store fails fast on an embedding-model-version mismatch**
  (parity with the Qdrant store), instead of silently returning garbage
  neighbours.
- **Docker sandbox hardened by default** (`--cap-drop=ALL`,
  `--security-opt=no-new-privileges`, `--pids-limit=512`), with `mem_limit`
  and `network` configurable. It's the real isolation boundary for the
  by-design arbitrary-code tools.
- **Admin token compare is constant-time** (`hmac.compare_digest`) and fails
  closed on `None`.
- **MCP stdio servers** get a merged environment instead of a replaced one
  (a bare `env=` would strip `PATH`/`FABRI_HOME` and break the server).

Second audit pass (report rendering, network tools, recipes, CLI surfaces):

- **SSRF guard on `fetch_url`** (builtin + recipe). The model-supplied URL is
  now restricted to http(s), refused if the host resolves to a
  private/loopback/link-local/reserved address (cloud metadata
  `169.254.169.254`, localhost, RFC1918), and re-validated on every redirect
  hop so a public URL can't 302 to an internal IP. `file://` is blocked.
  Escape hatch `FABRI_FETCH_ALLOW_PRIVATE=1` for fetching trusted internal
  dev services (off by default).
- **HTML report XSS fixed.** `fabri report --format html` now `html.escape`s
  every trace-derived cell/header (task text, tool names, model ids,
  outcomes) and the SVG chart label — previously a task containing `<script>`
  became active markup in the generated, shareable `.html`.
- **`session_id` path containment.** `trace_path` rejects ids outside
  `[A-Za-z0-9_.-]`, so a crafted id can't escape `.fabri/traces/` on the
  `replay` / `traces` / `ingest-traces` read paths (defense-in-depth; HIGH if
  a host ever feeds externally-supplied ids).
- **Recipe hardening.** `run_shell_safe` drops `find` (its `-exec`/`-delete`
  defeat a binary allow-list) and rejects exec/file-write args
  (`-exec`, `--output`, `git -c`, …); `git_diff` validates `ref` so a
  `--output=` can't write the diff to an arbitrary file.

## v0.7.7 — 2026-06-24

Multi-provider per-role LLM + OpenRouter, plus a Haiku-class narrator that
emits short user-facing status updates between tool steps. Backward
compatible: existing v0.7.x `agent.yaml` files keep working unchanged.

### Added

- **Per-role LLM provider/model selection.** `llm.decompose`, `llm.planner`,
  and `llm.narrator` accept either a model-id string (legacy shorthand) or
  a full dict `{provider, model, api_key_env, max_tokens, base_url,
  cache_messages}`. Each role bills against its own API key; the four
  roles can run on three different providers simultaneously. Inherits any
  missing field from the parent `llm.*` defaults.
- **New provider keyword: `openrouter`.** OpenAI-API-compatible; the
  backend pins `base_url=https://openrouter.ai/api/v1` automatically.
  Model ids are namespaced (e.g. `anthropic/claude-haiku-4-5`).
- **`OpenAILLMBackend(base_url=...)` kwarg.** Optional; lets the same
  backend talk to any OpenAI-compatible endpoint. Pure addition — old
  callers see no signature change.
- **Haiku narrator emits `narration` trace events between tool steps.**
  Configured via `llm.narrator` (defaults to `claude-haiku-4-5`,
  effectively free per run); set to `null` to silence. Failures are
  swallowed so narration never breaks a run; usage rolls into the run's
  `total_cost_usd`. New `run_agent(narrator_llm=...)` parameter.
- **`runtime.build_role_llm(config, role, tool_defs=None)`** — single
  resolver that powers `build_llm` / `build_decompose_llm` /
  `build_narrator_llm` (now one-line shims). Adding a new provider means
  one branch in `runtime._instantiate`.
- **`runtime.find_missing_role_api_keys(config)`** — walks every
  configured role and returns `{env_var: [roles]}` for the env vars that
  aren't set. CLI + benchmark pre-flight now reports ALL missing keys in
  one error instead of failing on the first.
- **Pricing entries for common OpenRouter model ids** —
  `anthropic/claude-{haiku-4-5,sonnet-4-6,opus-4-8}`,
  `openai/{gpt-4o,gpt-4o-mini}` — match the underlying provider's list
  price; reconciled to the OpenRouter invoice on adoption.

### Changed

- **`config._normalize_llm_roles`** runs inside `load_config` (and lazily
  inside `runtime._resolve_role_cfg` for callers that bypass
  `load_config`). Lifts legacy flat keys (`decompose_model`,
  `narrator_model`, `narrator_max_tokens`) into the new role shape with
  no warning. If both legacy and new exist for the same role, the new
  dict wins — clean incremental migration.
- **Memory store now fail-fasts on embedding-model mismatch.**
  `_ensure_collection` scrolls one existing point and raises with a
  clear "recreate-or-rename" message when its `model_version` differs
  from the running embedding model. Previously this would silently mix
  embedding spaces.
- **Tool manifest arg-rewriting tightened.** A token like `grep.py` in
  `bash -c "ls grep.py"` no longer gets rewritten to an absolute path
  just because a sibling file named `grep.py` exists. Only path-shaped
  tokens (script extension or containing `/`) qualify.

### Removed

- **Dead `evict_stale` in `memory/pruning.py`.** No callers, and the
  gate could never fire given how promotion grows `hit_count`.
- **Narrator provider-mismatch heuristic (`_NARRATOR_PROVIDER_DEFAULTS`,
  `_is_anthropic_model_id`, `_is_openai_model_id`)** in `runtime.py` —
  ~30 lines. The per-role `provider` keyword replaces it; the heuristic
  was guesswork the user can now state explicitly.

### Compatibility

- No DB / disk format change. No on-wire trace event change (new
  `narration` event is purely additive).
- `LLMBackend`, `run_agent`, `build_llm`, `build_decompose_llm`,
  `build_narrator_llm`, `AnthropicLLMBackend`, `OpenAILLMBackend` all
  preserve their existing signatures. `OpenAILLMBackend.__init__` gains
  one optional kwarg.
- A v0.7.6-shape `agent.yaml` produces the same backend selection it
  always did. A pin-test (`test_legacy_config_unchanged_backend_selection`)
  guards against drift in the lift logic.

### Tests

490 passed (469 without the optional `openai` extra). New:
`test_unit_role_resolution.py` (15 cases incl. legacy-config pin),
`test_unit_openrouter_backend.py` (3 cases), 3 OpenRouter pricing cases,
narrator dedup/empty-drop/multi-step/usage-rollup coverage in
`test_unit_narrator.py`.

## v0.7.6 — 2026-06-23

The "public source release" pass. No agent-loop semantics change; no
config-shape change; no memory schema migration. A host service that
uses fabri as a library needs zero changes.

### Added

- **`SqliteMemoryStore` re-exported from `fabri`** — `from fabri import
  SqliteMemoryStore` now works alongside `QdrantMemoryStore`, matching
  the in-process backend that `pip install 'fabri[sqlite]'` promotes.
  The library example in the README and `docs/creating-an-agent.md`
  picks it up; existing code keeps working unchanged.
- **`ToolHandler` type alias** in `tools/registry.py` for callable-backed
  tools; tightens the type hints around `register_callable()` (was
  previously `dict[str, "callable"]`, a stringified built-in).

### Changed — docs & READMEs

- **README licence + PyPI links rewritten as absolute GitHub URLs** so
  they resolve on the rendered PyPI page (previously relative paths
  404'd outside the repo). Added BUSL-1.1 / PyPI / Python-version
  badges.
- **MCP-servers documented in the config-schema section** of the README.
  The feature has shipped since v0.7.2; it was previously discoverable
  only by reading `config.py`.
- **TODO.md P3 backlog cleaned up** — the items marked open in v0.7.1's
  hardening pass (read_file/edit_file byte cap, decompose fence strip,
  admin gate warning, reserved decompose name, embed() empty/whitespace
  reject, OpenAI parallel-tool-call) are now correctly checked off, each
  with a `_(v0.7.1)_` annotation.

### Changed — code comments

- **Internal ticket-prefix comments (`# G9`, `# A1`, `# S2`, `# P3`,
  `# F2`, ...) stripped or rewritten as plain English** across
  `config.py`, `core/agent.py`, `cli.py`, `runtime.py`,
  `orchestrator/retrieval.py`, `tools/registry.py`,
  `tools/agent_runner_tool.py`, `tools/examples/read_file.py`,
  `memory/store.py`, `memory/compress.py`. Internal-tracker shorthand
  meant nothing to anyone outside the project. The few comments that
  carried genuine WHY (tool-filter invariants, A4 dedup semantics,
  tokenizer-approximation note, provider-quirk fallbacks) survive
  verbatim.
- **Narrative WHAT-blocks collapsed** in `core/agent.py` (system-prompt
  policy block, cost-rollup, sub-agent telemetry) and `config.py`
  (DEFAULT_CONFIG inline narration). Behaviour unchanged; the modules
  read in one screen instead of three.

### Tests

- Suite stays green at 449 passing. Comment-only edits and the
  `SqliteMemoryStore` re-export don't touch observable behaviour.

## v0.7.5 — 2026-06-23

The "host-integration ergonomics" release. Three host-integration pain
points surfaced from a long fan-out orchestrator run: the host did the
work, narrated nothing on the last step, and was reported as a failure.

### Added

- **Terminal `incomplete` / `failed` trace events now carry `text`** — the
  model's last assistant utterance (last non-empty `final_text` or
  `thinking_text` from the run). Hosts that surface a recap after a
  max-steps termination no longer have to scrape `thought` events
  heuristically. The `final` event keeps its existing `text` (the
  model's `final_text`) unchanged; `outcome` and `reason` are unchanged
  on the other two. Strictly additive.
- **Final-step nudge** — on the LAST allowed step, the agent loop appends
  a one-shot "this is your FINAL step; stop calling tools and answer
  now" instruction to the last user message. Converts the common
  "did-the-work-ran-out-of-narration-budget" case into a clean
  `success` with real `final_text` instead of an `incomplete`
  termination. Gated on `max_steps > 1` so single-step runs are not
  perturbed. Active in both the legacy and planner-item loops.
- **`agent.subagent.{max_steps, max_cost_usd}`** — independent budget for
  spawned sub-agents. A host that raises the orchestrator's `max_steps`
  to give a fan-out room no longer inflates every child's budget too.
  Each field falls back independently to the parent's value when unset
  (default `null` for both → identical pre-v0.7.5 behaviour).
  `agent_runner_tool.py` (the spawn entry point) now also forwards
  `max_cost_usd` to `run_agent`, which it didn't before.
- **Design note: `docs/design/repair-loop.md`** — proposed
  verify → repair → bounded-rerun loop primitive (Item 3 from the host
  integration triage). Not implemented in this release; the note maps
  the config shape, loop semantics, cost-budget composition, and the
  open questions to resolve before coding. Targeted for v0.8.

### Tests

- `tests/test_unit_v075_features.py` — seven new unit tests covering:
  the `text` field on terminal `incomplete` and `failed` events, the
  final-step nudge converting `incomplete` → `success` (and the gate
  that suppresses it at `max_steps=1`), and the three subagent-budget
  combinations (full override, no override, partial override).

### Notes

- No change to the `Outcome` enum values, the `final` event shape, or
  `fabri.cli`'s exit-code mapping. The `text` addition to `incomplete` /
  `failed` is the only on-wire change, and it's a new optional field —
  existing trace readers ignore it.

## v0.7.4 — 2026-06-23

### Fixed

- **`SqliteMemoryStore` was missing a `collection` attribute** that
  `memory/pruning.py` reads to derive its per-collection ingest-lock file
  name. As a result, any sqlite-backed agent run that produced a
  `success_pattern` (or any other guideline) crashed during post-run trace
  mining with `AttributeError: 'SqliteMemoryStore' object has no attribute
  'collection'`. Found by the first real `session_delta` benchmark run
  against `configs/benchmark.yaml`. The end-to-end fabri × sqlite path was
  green at the store-API layer (the dedicated tests for the backend pass)
  but the pipeline integration had never been exercised.
- `SqliteMemoryStore.__init__` now takes a `collection: str = "fabri"`
  argument and stores it on the instance.
- `runtime.build_memory_store` passes `mem_cfg.get("collection", "fabri")`
  through to it, matching what it already does for `QdrantMemoryStore`.

### Tests

- The fix is covered by the existing per-store test pass plus a smoke check
  from the failing benchmark run; a dedicated pipeline-integration test for
  the sqlite backend lands in v0.7.5 (it requires a scripted-LLM end-to-end
  fixture for trace mining and is a follow-up).

## v0.7.3 — 2026-06-23

The "benchmark methodology lockdown" release. Ships two canonical configs +
a methodology doc so every future benchmark number is reproducible against
a specific fabri version.

### Added

- **`configs/example.yaml`** — runnable starter config (sqlite-vec memory,
  Sonnet 4.6, minimal tool surface). Allowed to drift across releases;
  it's a teaching artifact, not a contract.
- **`configs/benchmark.yaml`** — the LOAD-BEARING config every published
  fabri benchmark runs against. Locked per minor version: any value
  change requires a minor version bump AND a results-table note in
  `BENCHMARKS.md`. Each field carries an inline comment explaining the
  strategic call.
- **`configs/README.md`** — short pointer at the two files + quickstart.
- **`BENCHMARKS.md`** at the repo root — methodology, reproduction
  commands for both `session_delta` and LongMemEval, and empty results
  tables ready to accept rows as real runs land. Cites the comparison
  numbers (Mastra 94.87% on LongMemEval, Letta, Mem0, Zep) inline so the
  comparison is honest when the first fabri row gets filled in.
- **README hero block** updated with the no-docker `pip install
  'fabri[sqlite]'` path + a pointer at `configs/` and `BENCHMARKS.md`.

### Why this lands before any real benchmark number

The number you publish is only worth what you'd let someone re-run it.
Locking `configs/benchmark.yaml` first means every future "fabri got X% on
Y" can be reproduced against a specific fabri version, not just "whatever
config the demo happened to use." This is the spine the benchmark platform
work hangs off of.

### Operational

- No code changes; pure docs + configs.
- `configs/*` is included via setuptools defaults (no `package-data`
  change needed — these live at the repo root, not in the wheel; they're
  cloned/forked from GitHub).
- Tests: suite stays at 442 (no new tests; configs are validated by
  `load_config()` round-tripping in `__main__`, see release notes).

## v0.7.2 — 2026-06-23

The "clear the deferred backlog" release. All eight deferred items from
v0.7.1's CHANGELOG land here. Two are opt-in (G9 budget, G21 caching) so
ludexel keeps current behaviour unless it sets the new config keys.

### Added — opt-in features (default off; zero impact unless configured)

- **G9 cost-budget enforcement.** New `agent.max_cost_usd` config knob. When
  set, the run breaks out cleanly with `Outcome.BUDGET_EXCEEDED` before
  issuing an LLM call whose result would push total COGS (own + sub-agent
  subtree) past the threshold. Default: `null` (no budget; current behavior).
  Emits a `budget_exceeded` trace event with the step + threshold for
  observability.
- **G21 extended prompt caching.** New `llm.cache_messages` config knob
  (Anthropic-only). When true, marks the LAST message's tail content block
  with `cache_control: ephemeral`, so the conversation history prefix reads
  from Anthropic's 5-min cache (~0.1× input bill on the cached prefix) on
  subsequent turns. Default: `false`. Mutates a shallow copy — caller's
  messages list is untouched. Anthropic's 4-breakpoint limit is respected
  (system + tools + last-message uses 3 of 4).

### Added — CLI (G5)

- **`fabri replay <session_id>`.** Re-runs the original task from a recorded
  trace against the *current* memory state. Prints a before/after summary
  (outcome, cost, steps) plus a JSON dump. Useful for "did the memory loop
  actually change behavior?" — but the LLM is non-deterministic, so read it
  as a directional signal and pair with `session_delta` for statistical
  weight.

### Added — reports (G7)

- **Per-step cost attribution.** `step_finished` events now carry a
  `cost_usd` field (priced from the step's `response.usage` alone), and
  `reports.aggregate` walks the trace step-by-step to split each step's LLM
  cost across the tools dispatched that step. v0.7.0's proportional-by-
  total-call-count split is the fallback for legacy traces without
  per-step cost.

### Added — MCP (HTTP transport, server side)

- **`MCPHttpClient`.** JSON-RPC over HTTP POST. Same surface as
  `MCPStdioClient` (`initialize` / `list_tools` / `call_tool` / `close`).
  No SSE streaming yet — that's the next follow-up. Server config gains
  `url` + optional `headers` fields:

      tools:
        mcp_servers:
          - name: fs
            url: "https://mcp.example.com/jsonrpc"
            headers: {Authorization: "Bearer ..."}

  `build_mcp_tools` picks transport by which field is set (errors loudly
  on both / neither).

- **`fabri.tools.mcp_server`** — expose a fabri agent as an MCP server over
  stdio. Run as `python -m fabri.tools.mcp_server --config agent.yaml
  [--tool-name fabri_agent]`. Exposes ONE tool whose input is `{task:
  string}`; the call invokes `run_agent` and returns the agent's final text
  in the standard MCP `content[]` shape with `isError` set by the run's
  `success` field. Lazy-inits the tool registry + store on first call so
  list-tools-only clients don't pay setup cost.

### Added — LongMemEval benchmark (G1 follow-up)

- **`fabri.benchmarks.longmemeval`** — full end-to-end runner.
  - HuggingFace dataset downloader (lazy, cached at `~/.cache/fabri/
    longmemeval/`). Falls back to a clear install hint if `datasets` isn't
    installed.
  - Per-case isolated memory collection so cross-case leakage doesn't
    inflate scores.
  - Exact-match scorer (case + whitespace normalized) shipped; LLM-judge
    scorer scaffolded behind `--judge`.
  - Per-category aggregation.
  - CLI: `python -m fabri.benchmarks.longmemeval --config agent.yaml
    --limit 10` (full eval is ~10k cases, several hours).

  **Status:** runner end-to-end + scoring helpers under test; the publish-
  worthy ~10k-case number needs a user-side run with real API credits.
  Single highest-leverage marketing artifact once it lands.

### Changed — memory/compress.py hardening (TODO.md)

- **Model-aware tokenizer.** `count_tokens` and `enforce_token_cap` now
  pick a tiktoken encoding per model (`o200k_base` for Claude 4.x and
  gpt-4o; `cl100k_base` fallback for unknown). The historical hard-coded
  `cl100k_base` could mis-count by ~10-15% on Claude.
- **Word-boundary truncation.** `enforce_token_cap` no longer slices a
  guideline mid-token — it backs up to the previous whitespace before
  appending `…`. Stops guidelines that end in a meaningless half-syllable.

### Tests

- **+19 tests** in `test_unit_v072_features.py` covering G7 per-step
  attribution + legacy fallback, G9 budget outcome + default, G21 message
  cache marking + non-mutation, tokenizer word-boundary + model-aware,
  MCP HTTP serialization, build_mcp_tools transport-picking rejection,
  MCP server initialize / list / unknown-method / notification handling,
  LongMemEval scoring + by-category aggregation. Suite 423 → 442.

### Deferred (need separate work)

- **MCP HTTP+SSE** — POST request/response works; streaming responses
  (the SSE variant) is the next follow-up.
- **`model_version` enforcement** in `memory/schema.py` — would invalidate
  existing collections; needs a migration story (rename collection on
  mismatch? raise + ask user to recreate?) before shipping.
- **Concurrent-ingest + wheel-packaging + multi-block tests** — TODO.md
  test-coverage holes. Tests-only changes; tracked for v0.7.3.
- **manifest_schema over-eager path rewriting** (`tools/manifest_schema.py
  :23`). Could affect ludexel's tool configs; needs a targeted test before
  changing.

## v0.7.1 — 2026-06-23

The "close the gaps that don't touch ludexel" release. P3 hardening pass +
six additive features from the P2 backlog. No agent-loop semantics changed.
No memory schema migration. No config-shape breaks. A host service that
uses fabri as a library (e.g. ludexel) needs zero changes.

### Added — fan-out telemetry & regret detection (G10/G11)

- **`subagent_*` fields in every `usage` event**: `subagent_count`,
  `subagent_successful_count`, `subagent_failed_count`,
  `subagent_max_subtree_cost_usd`, `subagent_regret_count`. Tells you
  whether the agent stayed single-threaded or fanned out, and what it cost.
- **`delegation_regret` trace event.** Fires when a `spawn_subagent`
  succeeded but the child ran ≤1 step *and* cost > $0 — i.e. the spawn was
  almost certainly inlinable. The event carries `tool`, `child_step_count`,
  `child_cost_usd`, and a `reason` string. The strategic "single-threaded by
  default" claim is now empirically falsifiable per-run.
- **`on_subagent_finished(call, ok, child_usage)` callback** in
  `_dispatch_tool_calls` — invoked once per spawn regardless of ok/failure.
  Optional + keyword-only so existing direct callers (the F2 timing tests)
  are unaffected.

### Added — CLI (G3, G14)

- **`fabri memory diff <session_a> <session_b>`** — partitions every
  guideline into `new in B`, `shared`, `only in A`. Demo-friendly: show what
  the agent *learned* in a 30-minute run.
- **`fabri tool init <lang> <name>`** — scaffold a new tool's manifest +
  executable stub in `python | go | node | bash`. Lands the pair under
  `--dir` (default `tools/agent_tools/`). Bash stubs are chmod 755.

### Added — polyglot examples & recipes (G12/G13/G15)

- **Rust example (`example_rust_tool/`)** — `regex_lines` tool: greps a file
  for a regex, returns matching lines. Cargo + serde + regex.
- **Node example (`example_node_tool/`)** — `file_stats` tool: bytes/lines/
  words + a language guess from the extension.
- **Tool recipes (`fabri.tools.recipes/`)** — copy-paste-ready patterns:
  `fetch_url`, `git_diff`, `grep_dir`, `run_shell_safe`, `python_eval`. Each
  ships with output caps + deny-lists where relevant.

### Changed — P3 hardening

- **`read_file` / `edit_file`** now refuse files > 1 MB with a clear error
  message pointing the agent at `outline_only` / line windowing. Stops a
  single tool call from blowing up the agent's context.
- **`decompose` parser strips ```json``` / ```toon``` / bare ``` fences**
  before json.loads / toon.decode — a fenced-but-otherwise-fine response is
  no longer misclassified as malformed.
- **`embed()` rejects empty/whitespace text** with a ValueError. Silent
  near-zero-vector dedup poisoning is gone.
- **Admin gate logs a WARNING when `FABRI_ADMIN_TOKEN` is unset** so an
  operator can grep their logs after deploy to verify auth is wired.
- **`build_tools` refuses a registry containing a tool named `decompose`**
  — that name is reserved for the framework meta-tool.

### Removed from the backlog (already fixed in an earlier release)

- OpenAI parallel-tool-call truncation. The OpenAI backend already collects
  every `message.tool_calls[]` and emits all of them on `LLMResponse`
  (`core/llm.py:473`). The TODO item is stale.

### Tests

- **+27 tests** across `test_unit_p3_hardening.py` (fence strip, empty embed
  reject, admin warning, reserved decompose, read_file cap),
  `test_unit_subagent_telemetry.py` (G10/G11 callback + regret event),
  `test_unit_tool_scaffold.py` (G14 scaffolder, all 4 languages),
  `test_unit_memory_diff.py` (G3 partitions). Suite 396 → 423.

### Ludexel compatibility

This release deliberately defers:
- G9 cost-budget enforcement (`agent.max_cost_usd`) — needs a UX design.
- G21 extended prompt caching — needs an opt-in flag and careful Anthropic
  testing.
- G5 trace replay — semantics are non-trivial (re-run against memory at
  point-in-time?).
- `model_version` enforcement — would invalidate existing collections.

## v0.7.0 — 2026-06-23

**The "make the claim true" release.** A strategic positioning review (see
`decks/internal/code-gaps.md`) identified ten gaps between fabri's pitch
("self-improving agent runtime with honest COGS") and the codebase. This
release ships all ten, end-to-end, with tests. The pitch is now demonstrable
in `fabri report` — not just instrumented under the hood.

### Added — observability (G6/G7/G8/G20)

- **`fabri report` CLI.** Aggregates `.fabri/traces/*.jsonl` into a usage
  report: total / by-model / by-tool cost, outcome distribution, per-session
  detail. `--since 7d/24h/30m` time filter, `--limit N`, `--format md|json|html`,
  `-o <file>` for write-to-file. Backed by a new `fabri.reports` module
  (`aggregate`, `render`, `chart`).
- **Cost-by-tool attribution (G7).** Proportional split of each session's
  `cost_usd` across its tool calls. Surfaced in markdown + HTML reports under
  "cost by tool" and via `SessionSummary.cost_by_tool`. A per-step attribution
  (LLM cost of step N → tools dispatched at step N) is a follow-up; this is a
  good-enough first cut.
- **COGS trendline chart (G8).** ASCII sparkline (`reports.chart.ascii_sparkline`)
  for the terminal output of `fabri report --since 30d`; self-contained SVG
  trendline (`reports.chart.svg_trendline`) embedded in the HTML report.
- **Static HTML report (G20).** `fabri report --format html -o report.html`
  writes a self-contained HTML file — no external CSS/JS, no fetches. Pastable
  into a deck/blog; seed of the eventual hosted dashboard.

### Added — memory observability (G2/G4)

- **`fabri memory show` / `fabri memory list`.** `show` is a human-readable
  listing of guidelines (filter by `--strategic` / `--tactical`, `--limit N`,
  `--markdown` output suitable for pasting into a deck). `list` is the
  pipeable JSONL counterpart. Backed by a new `QdrantMemoryStore.iterate()`
  (paginates Qdrant's scroll API) and a matching method on the new
  `SqliteMemoryStore`.
- **Guideline reuse rate metric (G4).** `retrieve_context_with_meta()` returns
  (text, meta) with `retrieved` / `from_prior_sessions` / `strategic` counts;
  the agent loop emits these in the `usage` event next to `cost_usd` as
  `guideline_reuse_rate`, `guidelines_retrieved`,
  `guidelines_from_prior_sessions`. "From prior sessions" =
  `hit_count >= 2 OR len(session_ids) >= 2` — the cross-session-learning
  signal, not just "memory had data."

### Added — embedded vector store (G16)

- **sqlite-vec memory backend.** New `fabri.memory.embedded_store.SqliteMemoryStore`
  with the same interface as `QdrantMemoryStore`. Selected via
  `memory.backend: sqlite` + `memory.sqlite_path: .fabri/memory.db`. Demos /
  CI / single-process deployments no longer require docker. Install via
  `pip install 'fabri[sqlite]'`. Production users keep Qdrant.
- **`fabri.runtime.build_memory_store(mem_cfg)` factory.** The agent loop, the
  CLI, and the benchmark harness all build their store through this factory;
  switching backends is a one-line config change.

### Added — benchmark harness (G1)

- **`fabri.benchmarks.session_delta` runner.** Runs the same task N times,
  records per-run cost / outcome / step count / guideline reuse rate, computes
  the cost delta between the first run and the median of the last three.
  CLI: `python -m fabri.benchmarks.session_delta --config agent.yaml
  --task "..." --runs 5`. Emits markdown + JSON under
  `.fabri/benchmarks/<ts>/`.
- **LongMemEval scaffold.** `fabri.benchmarks.longmemeval` directory in place
  with a porting plan in `README.md`. Dataset port is a follow-up
  (decks/internal/code-gaps.md G1).

### Added — MCP client (G19)

- **Minimal MCP stdio client.** `fabri.tools.mcp_client.MCPStdioClient` speaks
  JSON-RPC 2.0 over NDJSON (line-delimited). One server per process, server
  banner tolerance (skips up to 10 non-JSON lines), JSON-RPC error → tool_error
  conversion. `build_mcp_tools(server_cfg)` connects, lists, and wraps each
  remote tool as a `ToolManifest`. Config: `tools.mcp_servers: [{name, command, env?}]`.
- **`ToolRegistry.register_callable(manifest, handler, owns=...)`.** New hook
  for non-subprocess tools. MCP tools go through this path; agent-as-tool and
  manifest-discovered tools are unchanged. The `owns` reference keeps the
  backing MCP client alive for the registry's lifetime.

### Added — starter templates (G18)

- **`fabri init --template research|code-review|data-cleanup`.** Three vetted
  starter packs, each with a tailored `agent.yaml` (right max_steps, right
  planner mode, sqlite backend so no docker required) and 1–2 example tools
  (fetch_url for research, run_shell for code-review).
- **`fabri.scaffold.SCAFFOLD_TEMPLATES` registry** so future templates land
  by adding one dict.

### Changed

- **Default config gained `memory.backend` / `memory.sqlite_path` keys** —
  back-compat: omitted keys default to `qdrant` + the existing URL.
- **Default config gained `tools.mcp_servers: []`** — empty by default, MCP
  disabled.
- **`fabri init` accepts `--template`.** Default behavior (no flag) is the
  same as before — the existing hello-tool scaffold.

### Tests

- **+37 tests** across `test_unit_reports.py` (reports module: aggregation,
  markdown/json/html rendering, sparkline + SVG chart), `test_unit_mcp_client.py`
  (JSON-RPC framing, error handling, EOF detection, sanitization),
  `test_unit_scaffold_templates.py` (every template scaffolds + parses).
  Suite 359 → 396.
- Fixed `test_cmd_run_prints_synthesized_guideline_summary` (added in v0.6.1) —
  its helper was overriding `process_trace` after the test set it. The helper
  now accepts an `entries=` keyword.

## v0.6.1 — 2026-06-23

### Fixed

- **CLI no longer exits non-zero on `success_with_recovery` outcome.** The
  `fabri run` exit-code check compared `result["outcome"]` against the literal
  `"succeeded"`, which is not a value of the `Outcome` enum. Any run that
  recovered from a transient tool failure ended with `outcome="success_with_recovery"`
  and `success=True`, but the CLI still exited 1 — so host services that
  dispatch on the return code (e.g. ludexel's `runs` collection) mislabeled
  successful runs as failures and surfaced the agent's success summary as the
  error body. The check now positively matches `Outcome.SUCCESS` and
  `Outcome.SUCCESS_WITH_RECOVERY`.

## v0.6.0 — 2026-06-23

**License change: Apache-2.0 → Business Source License 1.1.** v0.6.0 and every
later release is BSL-licensed; free for individuals and for organizations with
≤ US $1M annual gross revenue, with a commercial license required above that
or when embedding fabri into a hosted/distributed product. Every BSL version
auto-converts to Apache 2.0 on **2030-06-23** (the Change Date). See
[COMMERCIAL.md](COMMERCIAL.md) for who needs a license and how to get one.
Versions ≤ 0.4.6 remain Apache-2.0. v0.5.0 and v0.5.1 were withdrawn from
PyPI prior to general availability and are not supported; their functionality
is rolled into this release.

### Added (carried from withdrawn v0.5.0)

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

### Added (carried from withdrawn v0.5.1)

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

### Tests

- **+105 tests** (pricing edge cases, cost rollup across mixed/unknown models and
  sub-agent subtrees, both LLM backends incl. truncation-retry / prewarm /
  model-tagging / cache folding, the `QDRANT_URL` override, system-prompt
  frugality gating, and `spawn_subagent` command plumbing). Suite 246 → 351.

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
