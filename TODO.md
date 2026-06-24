# fabri — backlog

Prioritized from a hard correctness/security audit (2026-06-19). Each item:
`file:line — problem → fix`. P0 = ships broken; P1 = bites a real user; P2 =
integrity/robustness; P3 = hardening/nits.

## P0 — ship-blocking

- [x] **Bundled tool manifests not packaged.** A wheel install dropped every
  `tools/examples/*.json`, so the `builtin` manifest_dir token resolved to an
  *empty* registry with no error. Fixed via `[tool.setuptools.package-data]` in
  `pyproject.toml`; verified all 13 manifests ship and `builtin` resolves to
  `[read_file, write_file]` from a clean non-sibling wheel install.

## P1 — correctness bugs that bite a real user — ALL FIXED

Fixed in one pass (`core/llm.py`, `core/agent.py`, `memory/{pruning,store}.py`,
`tools/runner.py`, `tools/examples/*.py`); 4 regression tests added; suite green.

- [x] **Anthropic backend returns only the first content block.** `core/llm.py`.
  Now collects every `tool_use` block (parallel calls supported), only treats a
  turn as final when there are no tool calls, and `core/agent.py` dispatches the
  whole list, pairing each `tool_use` with a `tool_result`.
- [x] **OpenAI backend doesn't round-trip tool calls.** `core/llm.py`. New
  `_to_openai()` translates the Anthropic-shaped history into
  `assistant.tool_calls` + `role:"tool"` messages, so multi-step tool use
  round-trips.
- [x] **`max_tokens` truncation treated as a final answer.** `core/llm.py`.
  Both backends raise `LLMError` on `stop_reason`/`finish_reason` == max-tokens.
- [x] **No API-error/rate-limit handling.** `core/llm.py`. `_call_with_retry`
  retries transient provider errors with backoff; unrecoverable ones become
  `LLMError` → `Outcome.FAILED` (now a live outcome, `core/agent.py`).
- [x] **Empty LLM response counts as success.** Backends return `final_text=None`
  for an empty turn; `core/agent.py` treats falsy final text as
  `AgentProtocolError`, not SUCCESS.
- [x] **Memory dedup hardcoded to `kind="tactical"`.** `memory/store.py`
  `find_similar(kind=None)` + `memory/pruning.py` now match across both kinds and
  never demote a promoted strategic entry.
- [x] **Sandbox fails open when `FABRI_SANDBOX_ROOT` is unset.** All 7 sandbox
  tools fail closed (error + exit 1) when the var is unset.
- [x] **Subprocess timeout doesn't kill the child's process group.**
  `tools/runner.py` runs tools via `Popen(start_new_session=True)` and
  `os.killpg(...SIGKILL)` on timeout (plus `errors="replace"` decoding).

## P2 — integrity & robustness

- [~] **Postmortem-to-qdrant: failure pattern memory** (requested by ludexel
  2026-06-24). Also tracked as forward-feature card **M1** in `docs/ROADMAP.md`
  (Track M) — keep the two in sync; close both when shipped.
  **First increment SHIPPED** (`memory.record_postmortems`, opt-in): every run
  now writes one deterministic, LLM-free postmortem
  `{task, outcome, step_count, tool_calls_total, repeated (tool × error-sig)}`
  as a new `postmortem` memory kind, retrieved by task similarity — the "you
  tried X N times" single line in context. See `pipeline.build_postmortem_text`
  + `tests/test_unit_postmortem.py`.
  **Still open:** the `final_diff` / `fix_pattern` half — extracting the
  corrective pattern from the diff between the failure point and the successful
  end-state (the noisy-transcript hard part), and retrieval matching on
  *predicted error kind* (not just task text). Flip the default to on once the
  fix_pattern half lands.

- [x] **Non-atomic ingest → lost updates.** `memory/pruning.py`. The
  find_similar→update→upsert critical section now runs under a per-collection
  `fcntl` flock at `.fabri/locks/<collection>.ingest.lock` (`_collection_lock`).
- [x] **No collection dimension/distance validation.** `memory/store.py:_ensure_collection`.
  On an existing collection, asserts `VectorParams.size == EMBEDDING_DIM` and
  `distance == COSINE`, raising a clear message naming the collection on
  mismatch instead of an opaque Qdrant upsert error.
- [x] **`model_version` stored but never enforced.** `memory/store.py:_ensure_collection`
  now scrolls one existing point and raises if its payload `model_version`
  differs from `EMBEDDING_MODEL_VERSION` — fail-fast with a recreate-or-rename
  message instead of silently mixing embedding spaces.
- [x] **`build_tools` mutates global `os.environ`.** `runtime.py` +
  `tools/registry.py` + `tools/runner.py`. Sandbox root is stored on the
  `ToolRegistry` and passed via `subprocess.Popen(env=...)` per invocation;
  two concurrent registries no longer clobber each other.
- [x] **Runner robustness.** `tools/runner.py`. Broad `except Exception`
  around `communicate()`, 1 MiB stdout cap (`RUNNER_OUTPUT_CAP_BYTES`) that
  flags `truncated: true` instead of silently breaking the JSON contract,
  `extra_env` plumbing for the per-registry sandbox.
- [x] **Trace read/write robustness.** `orchestrator/traces.py`. `log_event`
  takes an exclusive `fcntl.flock` before appending; `read_trace` skip-and-logs
  malformed JSONL lines instead of raising.
- [x] **Retrieval matching & ranking.** `orchestrator/retrieval.py`.
  Word-boundary tool-name match (`\b{name}\b`); embed once and pass to a new
  `QdrantMemoryStore.query_by_vector`; tag hits gated by `TAG_HIT_SCORE_FLOOR = 0.30`.
- [x] **No config validation / fail-fast.** `config.py` + `cli.py`.
  `_deep_merge` raises `ConfigError` on a scalar-overrides-dict shape
  mismatch; `load_config` catches missing-file / malformed YAML / non-mapping
  top-level; `cli.main()` prints `config error: ...` to stderr + exit 1.
- [x] **Outcome semantics — INCOMPLETE conflates "ran out of steps cleanly"
  with "every tool failed".** `core/agent.py` + `core/outcome.py`. New
  `INCOMPLETE_WITH_TOOL_FAILURE` outcome. (SUCCESS = "produced text" is
  still the documented contract; a real completion signal remains a future
  change.)
- [x] **Token cap uses the wrong tokenizer.** `memory/compress.py` now maps each
  known model id to its tiktoken encoding (`o200k_base` for gpt-4o + Claude as
  a documented approximation), tolerates date-suffixed ids via longest-prefix
  match, warns once on unknown ids, and `enforce_token_cap` snaps to a word
  boundary instead of mid-syllable.

## P3 — hardening & nits

- [x] `read_file.py`/`edit_file.py`: cap bytes read (other tools already cap). _(v0.7.1: 1 MB cap with outline_only / line-window hint.)_
- [x] `core/decompose.py:21`: strip ```` ``` ```` fences before `json.loads`. _(v0.7.1.)_
- [x] `core/agent.py:69`: reserve/namespace the `decompose` tool name. _(v0.7.1: `build_tools` refuses a registry containing one.)_
- [x] `core/llm.py:133`: OpenAI parallel-tool-call truncation. _(Already fixed; TODO was stale — see v0.7.1 changelog.)_
- [x] `memory/pruning.py`: `evict_stale` was dead (no callers, and a strategic
  entry by definition has `hit_count ≥ promotion threshold` so the gate could
  never fire). Removed.
- [x] `memory/embeddings.py:16`: reject empty/whitespace text before embedding. _(v0.7.1.)_
- [x] `admin.py:20`: open-by-default admin gate should at least log a warning. _(v0.7.1.)_
- [x] `tools/manifest_schema.py`: command-arg rewriting now gates on
  path-shaped tokens only (script extension or contains `/`), so a `bash -c
  "ls grep.py"` data arg no longer gets rewritten just because `grep.py`
  exists next to the manifest. Bare execs and CLI flags still pass through.

## Security/orchestration audit (2026-06-24) — fixed this pass

Four-area parallel audit (tool exec/sandbox, orchestration, memory/injection,
LLM/MCP/secrets). Active issues fixed (see CHANGELOG "Security & robustness
hardening" for detail + tests in `tests/test_unit_security_hardening.py`):

- [x] **Sub-agent fork-bomb** — no recursion/spawn cap. Added
  `FABRI_SUBAGENT_DEPTH`/`FABRI_SUBAGENT_MAX_DEPTH` (default 5).
- [x] **Budget unbounded across a parallel fan-out** — breached budget now
  refuses further spawns mid-step; structured-output retries budget-checked.
- [x] **Parallel future exception aborted the whole group** → unpaired
  `tool_use` → next-call 400. Now normalized to `tool_error`; dead loop removed.
- [x] **`on_subagent_finished` dead on the default (non-planner) path.**
- [x] **Planner item-budget divisor** starved later items after an early failure.
- [x] **`ask_user` socket blocked forever** — bounded wait + default fallback.
- [x] **Retrieved guidelines un-fenced** in the system prompt (stored prompt
  injection) — now wrapped + sanitized.
- [x] **Sqlite store** missing the embedding-model-version fail-fast.
- [x] **Docker sandbox** ran with full caps/privs/no pids cap — hardened defaults.
- [x] **Admin token compare** not constant-time; **MCP stdio** env replaced not merged.

### Second audit pass (2026-06-24) — fixed

- [x] **fetch_url SSRF** (builtin + recipe) — scheme allowlist + resolve/block
  private-reserved IPs + per-redirect revalidation; `file://` blocked;
  `FABRI_FETCH_ALLOW_PRIVATE` opt-in escape hatch.
- [x] **HTML report stored XSS** — `html.escape` on all trace-derived cells +
  the SVG label.
- [x] **`session_id` path traversal** — `trace_path` charset-validates the id.
- [x] **Recipe escapes** — `run_shell_safe` drops `find` + rejects
  exec/file-write args; `git_diff` validates `ref`.

### Deferred (tracked, not fixed — config/tool-trust or larger scope)

- [ ] **MCP remote tool descriptions** flow into the system prompt verbatim —
  a malicious server can prompt-inject. Frame `mcp_*` tools as third-party /
  require per-server allowlist.
- [ ] **MCP server mode is unauthenticated + unbounded** (`mcp_server.py`):
  a client can drive the full toolset and read the shared memory store with
  no per-request cap. Acceptable for the stdio/parent-process trust model;
  document it and consider a safe-tool allowlist when served.
- [ ] **Tool subprocesses inherit the full `os.environ`** (incl. provider keys);
  `bash`/`python_exec` can `echo $ANTHROPIC_API_KEY`. Consider a minimal-env
  allowlist for non-spawn tools (spawn needs the keys).
- [ ] **MCP stdio `_read` has no timeout** — a silent server hangs agent build;
  add a select/poll read timeout mirroring the HTTP transport.
- [ ] **`grep_dir` recipe** reads any path (no sandbox jail) — confine to a root
  if promoting it from recipe to a registered tool.
- [ ] **No memory TTL/eviction** — unbounded growth (slow DoS + retrieval
  dilution). Add an LRU/least-hit cap in `ingest_guideline`.
- [ ] **128-bit deterministic point ID** — negligible accidental collision;
  revisit only if guideline text becomes attacker-grindable.
- [ ] **TOON decode** raises `IndexError`/`RecursionError` on adversarial
  model output (currently swallowed by `decompose`'s `except`); add bounds if
  any caller ever decodes without a broad catch.

## Test coverage

Added this pass (see the named tests):
- [x] Concurrent ingest (lost-update) — `test_pruning.py::test_concurrent_ingest_does_not_lose_updates`
  fires 8 parallel ingests and asserts `hit_count == 8` (flock serializes).
- [x] Strategic re-ingest doesn't demote — `test_pruning.py::test_recurrence_of_promoted_guideline_does_not_demote_or_duplicate`.
- [x] Multi-block / parallel tool_use — `test_llm_backends_thorough.py::test_anthropic_collects_all_parallel_tool_use_blocks`
  (Anthropic); OpenAI round-trip + truncation-preserves-tool-calls already covered.
- [x] Guideline fence + forged-tag sanitization — `test_retrieval.py::test_retrieved_context_is_fenced_and_strips_forged_tags`.
- [x] Docker security flags (defaults + configurable + argv order) — `test_docker_sandbox.py`.
- [x] MCP stdio env merge — `test_unit_mcp_client.py::test_stdio_start_merges_env_instead_of_replacing`.
- [x] ask_user socket default-on-empty + socket timeout — `test_ask_user.py` / `test_unit_security_hardening.py`.
- [x] SSRF refusals + escape hatch, HTML XSS escaping, trace-path containment,
  recipe shell/git blocks — `test_unit_security_hardening.py`.
- [x] Sub-agent recursion-depth cap — `test_spawn_subagent.py`.

Still open:
- [ ] Wheel-packaging guard: `builtin` resolves to non-empty tools after a real
  `pip install` of the built wheel (regression for the P0; needs an isolated
  build+install, so it's a slow/integration test — gate behind a marker).
- [ ] Planner item-budget division: e2e test that a failed early plan item does
  not starve the step budget of later items (the `processed_count` fix).
- [ ] `max_tokens` truncation that carries partial tool_use args — assert the
  run fails loud rather than dispatching a half-parsed call (both backends).

## Done this pass

`tests/test_e2e_first_run_smoke.py` drives a full scaffold → load_config →
build_tools → run_agent journey under a `ScriptedLLMBackend`, exercising the
new ToolRegistry-owned sandbox, the flock-locked trace writer, and the new
`INCOMPLETE_WITH_TOOL_FAILURE` outcome.

---
Source: two parallel review passes (deep code audit + empirical wheel-install
plug-and-play test). Items are findings, not all confirmed design intent —
triage before fixing. Strengths worth preserving: `resolve()`-based path jail,
deterministic text-derived point IDs (idempotent inserts), normalized
`{ok,error?,result?}` tool-result contract, per-tool output caps.
