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

- [x] **Non-atomic ingest → lost updates.** `memory/pruning.py`. The
  find_similar→update→upsert critical section now runs under a per-collection
  `fcntl` flock at `.fabri/locks/<collection>.ingest.lock` (`_collection_lock`).
- [x] **No collection dimension/distance validation.** `memory/store.py:_ensure_collection`.
  On an existing collection, asserts `VectorParams.size == EMBEDDING_DIM` and
  `distance == COSINE`, raising a clear message naming the collection on
  mismatch instead of an opaque Qdrant upsert error.
- [ ] **`model_version` stored but never enforced.** `memory/schema.py:18` +
  `memory/embeddings.py`. Swapping the embedding model silently mixes embedding
  spaces in one collection. → Namespace collection by model version or
  reject/migrate on mismatch.
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
- [ ] **Token cap uses the wrong tokenizer.** `memory/compress.py:5,19`.
  `tiktoken cl100k_base` ≠ Claude/gpt-4o tokenizer; hard mid-clause truncation
  can produce a meaningless guideline. → Use the model's encoding (OpenAI) /
  document as approximate (Anthropic); prefer regenerating shorter over cutting.

## P3 — hardening & nits

- [ ] `read_file.py`/`edit_file.py`: cap bytes read (other tools already cap).
- [ ] `core/decompose.py:21`: strip ```` ``` ```` fences before `json.loads`.
- [ ] `core/agent.py:69`: reserve/namespace the `decompose` tool name.
- [ ] `core/llm.py:133`: OpenAI takes only the first tool call (parallel-call
  truncation, mirrors Anthropic).
- [ ] `memory/pruning.py`: confirm `evict_stale` is reachable/useful — it may be
  effectively dead given how `hit_count` grows with promotion.
- [ ] `memory/embeddings.py:16`: reject empty/whitespace text before embedding.
- [ ] `admin.py:20`: open-by-default admin gate should at least log a warning.
- [ ] `tools/manifest_schema.py:23`: command-arg→absolute-path rewriting is
  over-eager (rewrites any token matching a sibling filename).

## Test coverage to add

- [ ] Wheel-packaging guard: `builtin` resolves to non-empty tools after a real
  `pip install` of the built wheel (regression test for the P0).
- [ ] Strategic-dedup demotion / re-ingest of a promoted guideline.
- [ ] Concurrent ingest (lost-update) behavior — wired the flock; still need a
  test that fires two ingests at the same collection in parallel and asserts
  the final `hit_count` equals the number of ingests.
- [ ] Multi-block / parallel-tool-call / `max_tokens`-truncated LLM responses
  against both backends.

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
