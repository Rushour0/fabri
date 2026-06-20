# Changelog

All notable changes land here, newest first. Versions follow PyPI
immutability: never reuse a version number; cut a new one for any change
that ships.

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
