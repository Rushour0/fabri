# agent-memory — backlog

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
- [x] **Sandbox fails open when `AGENT_SANDBOX_ROOT` is unset.** All 7 sandbox
  tools fail closed (error + exit 1) when the var is unset.
- [x] **Subprocess timeout doesn't kill the child's process group.**
  `tools/runner.py` runs tools via `Popen(start_new_session=True)` and
  `os.killpg(...SIGKILL)` on timeout (plus `errors="replace"` decoding).

## P2 — integrity & robustness

- [ ] **Non-atomic ingest read-modify-write → lost updates.**
  `memory/store.py:24` + `memory/pruning.py:30-44`. Concurrent ingests (parent
  + sub-agent on one collection) both read `hit_count=N`, both write `N+1`. →
  Serialize ingest or use server-side atomic update / optimistic retry.
- [ ] **No collection dimension/distance validation.** `memory/store.py:16-22`.
  An existing collection from a different embedding model isn't detected;
  `upsert` fails deep in Qdrant or queries return garbage. → On existing
  collection, assert size/distance match and fail fast.
- [ ] **`model_version` stored but never enforced.** `memory/schema.py:18` +
  `memory/embeddings.py`. Swapping the embedding model silently mixes embedding
  spaces in one collection. → Namespace collection by model version or
  reject/migrate on mismatch.
- [ ] **`build_tools` mutates global `os.environ`.** `runtime.py:48`. Hidden,
  order-dependent side effect that the sandbox tools trust as their only jail;
  a second `build_tools` clobbers the root for already-spawned tools. → Pass
  sandbox root explicitly via the subprocess `env=`.
- [ ] **Runner robustness.** `tools/runner.py`: no `encoding="utf-8",
  errors="replace"` → uncaught `UnicodeDecodeError` (only Timeout/OSError are
  caught); requires entire stdout to be one JSON object (a stray print breaks
  it); no runner-level output cap. → Set encoding, broaden except, document/
  enforce the stdout contract, cap captured size.
- [ ] **Trace read/write not robust to corruption.** `orchestrator/traces.py:14,22`.
  One malformed JSONL line makes `read_trace` raise and kills all downstream
  processing; `log_event` appends unlocked so concurrent writers can interleave.
  → Skip/log bad lines; guard concurrent appends.
- [ ] **Retrieval matching & ranking.** `orchestrator/retrieval.py:19-22,31-40`.
  Substring tool-name match (`read` in "already"); re-embeds the task once per
  matched tool; tag hits bypass any score floor and can crowd out relevant
  vector hits. → Word-boundary match, embed once, apply a score floor to tag
  hits.
- [ ] **No config validation / fail-fast.** `config.py:42,56` + `cli.py:27`. A
  non-dict override drops a whole subtree → later `KeyError`; missing file /
  malformed YAML → raw traceback. → Validate merged shape; wrap load with a
  clear stderr message + non-zero exit.
- [ ] **Outcome semantics.** `core/agent.py:104,128-131`. SUCCESS == "produced
  text", not "task done" (a give-up message is SUCCESS); INCOMPLETE drops
  `had_tool_failure` so "every tool failed" looks like "ran out of steps". →
  Document SUCCESS meaning and/or add a completion signal; carry
  `had_tool_failure` into INCOMPLETE; detect repeated identical failing calls.
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
- [ ] Concurrent ingest (lost-update) behavior.
- [ ] Multi-block / parallel-tool-call / `max_tokens`-truncated LLM responses
  against both backends.

---
Source: two parallel review passes (deep code audit + empirical wheel-install
plug-and-play test). Items are findings, not all confirmed design intent —
triage before fixing. Strengths worth preserving: `resolve()`-based path jail,
deterministic text-derived point IDs (idempotent inserts), normalized
`{ok,error?,result?}` tool-result contract, per-tool output caps.
