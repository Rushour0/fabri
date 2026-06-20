# fabri

A local, open-source memory + orchestration layer for a custom LLM agent.
Built around two ideas:

- **SCOPE** (arXiv:2512.15374): treat the agent's prompt context as something
  that evolves automatically from execution traces, via a tactical/strategic
  memory split with conflict resolution before promotion.
- Context engineering over prompt engineering: keep retrieved context
  compact and just-in-time, give each tool a single clear job, and let tools
  be polyglot processes behind a uniform JSON contract.

## Quickstart

```bash
pip install fabri                    # the `fabri` console command lands on your PATH
docker run -p 6333:6333 qdrant/qdrant   # vector store for the agent's memory
export ANTHROPIC_API_KEY=...

fabri init demo && cd demo           # scaffold a runnable starter project
fabri --config agent.yaml run "greet Ada with the hello tool"
```

`fabri init` writes an `agent.yaml`, an example tool under `tools/agent_tools/`,
and a `docker-compose.yml` — edit those, not the library. For OpenAI models,
`pip install "fabri[openai]"` and set `llm.provider: openai`.

## Setup (from a checkout)

```bash
docker compose up -d                 # starts Qdrant on :6333
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
export ANTHROPIC_API_KEY=...         # only needed for AnthropicLLMBackend
```

Installing puts a `fabri` console command on your PATH — that's the entry point
everything below uses. Embeddings run fully locally via
`sentence-transformers/all-MiniLM-L6-v2` — no embedding API calls, no cloud
dependency beyond the LLM itself.

## Usage

```bash
fabri run "some task description"
fabri --config agent.yaml run "some task description"   # config-driven agent, see docs/creating-an-agent.md
fabri --verbose run "some task description"   # DEBUG logging to console too
fabri inspect-memory "a query to test retrieval"
fabri ingest-traces <session-id>
```

(`python cli.py ...` still works from a source checkout — it's a thin shim over
the same `fabri.cli:main` the console script points at.)

See `docs/creating-an-agent.md` for using this as a library/framework in
another project: installing it, writing an `agent.yaml`, adding tools, and a
worked example.

`run` executes the agent loop, then immediately mines the resulting trace for
failures and synthesizes any new guidelines. `ingest-traces` re-runs that
synthesis step against a past session's trace on its own. `run` and
`ingest-traces` both require `ANTHROPIC_API_KEY` (they fail fast with a clear
message if it's unset, rather than a raw stack trace) — `inspect-memory` does
not, since it only talks to Qdrant.

Every run produces two records, both keyed by `session_id`: a structured
JSONL trace (`.fabri/traces/<session_id>.jsonl`, the machine-readable
record used by the pipeline) and a human-readable log
(`.fabri/logs/<session_id>.log`, always
DEBUG-level regardless of `--verbose` — that flag only controls what's also
echoed to the console) with LLM call latency/token usage, tool dispatch
latency, and every promotion/dedup decision the pipeline makes.

Both land under `.fabri/` in the directory you run from (override with
`$FABRI_HOME`), so each consuming project keeps its own traces and logs
rather than scattering them into wherever the package happens to be installed.
Add `.fabri/` to your project's `.gitignore`.

Each run returns an `outcome`: `success` (clean), `success_with_recovery`
(finished, but at least one tool call failed along the way), or `incomplete`
(hit the step limit with no final answer).

## Architecture

```
src/fabri/        # src/ layout: repo is "fabri", package is "fabri"
  memory/        embeddings, schema, Qdrant store, compression, pruning/promotion
  orchestrator/  retrieval, trace logging, the trace -> guideline pipeline
  tools/         manifest schema, subprocess runner, example tools, agent-as-tool adapter
  core/          the ReAct agent loop, decompose, and the pluggable LLM backend
  runtime.py     build_llm/build_tools/build_tool_defs -- shared by cli and agent_runner_tool
  admin.py       admin-only config inspection + dashboard (stub auth seam, see below)
  config.py      YAML agent config loader (DEFAULT_CONFIG + load_config)
  toon.py        TOON codec: compact JSON-shaped encoding for tool results (token savings)
  paths.py       project-local .fabri/ resolution for traces + logs
  cli.py         the `fabri` console command (composition over the public API)
  __init__.py    public API: run_agent, load_config, ToolRegistry, QdrantMemoryStore, ...
cli.py           thin shim: `python cli.py ...` -> fabri.cli:main
```

### Agents as tools

A `tools.agents` entry in a config exposes a *different* agent.yaml as a tool
on this one (`fabri/tools/agent_tool.py` builds the manifest,
`agent_runner_tool.py` runs the sub-agent as an ordinary subprocess tool —
stdin `{"task": ...}`, stdout `{"final_text", "outcome"}`). This is
composition, not a new orchestrator: each sub-agent is just another tool call
in the parent's normal ReAct loop. See `ludexel/.agent/game_content_agent.yaml`
for a worked example wiring four domain agents (character/tiles/map/story)
into one top-level agent.

A `tools.agents` entry may carry optional `model` / `max_tokens` keys that
override the sub-agent's `llm.model` / `llm.max_tokens` at spawn time, so the
parent can run on Sonnet while a cheap classifier sub-agent runs on Haiku
without duplicating the full agent.yaml:

```yaml
tools:
  agents:
    - name: classify
      description: Classify a snippet into one of N labels.
      config: tools/agent_tools/classifier.yaml
      model: claude-haiku-4-5      # cheap override; sub-agent's own yaml keeps the default
      max_tokens: 256
```

### Admin CLI / dashboard

`cli.py admin config --config agent.yaml` prints the merged config plus the
resolved tool registry as JSON; `cli.py admin dashboard --config agent.yaml`
prints a human-readable summary (agent/llm/tools/memory counts). Both are
gated by `require_admin()` (`fabri/admin.py`): if `FABRI_ADMIN_TOKEN`
is unset the gate is open (no auth backend exists yet), but once it's set,
`--admin-token` must match it. This is a placeholder seam for real auth
(SSO/API gateway/etc.), not a security boundary by itself.

### Tool contract

A tool is a JSON manifest (`name`, `description`, `command`, schemas, timeout)
next to an executable in any language. `runner.py` invokes `command` as a
subprocess, writes the call's args as JSON to stdin, and parses stdout as
JSON. The result is always normalized to `{ok, error?, result?, stderr?}`
before the agent sees it — malformed output, timeouts, and tool error exits
are distinct, well-defined failure modes rather than crashes. See
`tools/examples/` for a Python tool (`echo`), a Go tool (`sum`), and a
deliberately misbehaving tool (`broken`) used only by the test suite.

### Token efficiency (TOON)

Tool results are encoded into the model's context as **TOON**
(Token-Oriented Object Notation, `toon.py`) rather than JSON by default — a
compact indentation-based encoding that drops braces and collapses uniform
arrays to a single header row plus data rows, typically ~30–40% fewer
characters on tabular results. The framework encodes this itself (no model
reliability risk); the trace/logs keep raw JSON. Pipeline: `json → toon → llm`
inbound, and JSON outbound by default (`tools.result_format`,
`agent.output_format` — see `docs/creating-an-agent.md`). Native tool-call
arguments stay provider JSON. The codec round-trips any JSON-shaped value and
`fabri.toon.encode`/`.decode` are public.

### Memory lifecycle

1. Every agent run logs a JSONL trace (`traces/<session_id>.jsonl`).
2. `orchestrator/pipeline.py` scans a trace for tool-call failures and asks
   the LLM to compress each into a short, generalized guideline
   (`memory/compress.py` enforces a hard token cap regardless of what the
   LLM returns).
3. `memory/pruning.py` checks the new guideline against existing **tactical**
   entries by cosine similarity. A near-duplicate increments that entry's
   recurrence count instead of inserting a copy; once it has recurred across
   3 distinct sessions (`PROMOTION_THRESHOLD_SESSIONS`), it's promoted to
   **strategic**.
4. `orchestrator/retrieval.py` embeds the next task, pulls the top-k most
   relevant guidelines (tactical + strategic) from Qdrant, and formats them
   as a compact bullet list injected into the agent's system prompt. If any
   available tool's name appears in the task text, a second query filters by
   that tool's tag (`MemoryEntry.tools`, populated in step 2 from whichever
   tool actually failed) and those hits are *guaranteed* inclusion — this
   surfaces tool-specific guidelines even when their wording is too
   dissimilar from the query for cosine similarity alone to rank them in the
   top-k. (A graph-DB-backed version of this was considered and rejected: the
   only capability needed was a single-hop "guidelines for this tool" lookup,
   which a payload filter gives you for free — a second database would have
   added a dependency, a migration risk, and a way for the graph and the
   vector store to drift out of sync, for no real capability gain.)

This is what closes the loop: a failure in session N becomes retrievable
context in session N+1, without a human re-writing the prompt by hand.

## Known v1 limitations (intentional, not bugs)

- **No embedding-model migration path.** Each memory point stores a
  `model_version` payload field, but swapping the embedding model means
  recreating the Qdrant collection from scratch — old vectors are not
  re-embedded automatically.
- **Single-writer assumption.** Concurrent *distinct* agent processes
  ingesting guidelines at the same time are not locked against each other.
  This is made safe-by-construction for the common case (same guideline
  text) via deterministic point IDs (hash of the compressed text), but two
  different writers racing on genuinely different new guidelines is
  untested.
- **No automatic eviction.** `memory/pruning.py:evict_stale` exists but is
  not wired into a scheduled job — run it manually if the strategic store
  grows large with low-value entries.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

Covers store round-trip/idempotency, the dedup + promotion rule, the tool
runner's normalized failure contract (Python + Go + malformed output +
timeout), the token-cap enforcement in `compress.py`, the TOON codec
(round-trip + fuzz), and the `fabri init` scaffold.

## Releasing

Tag a version (`git tag v0.1.0 && git push origin v0.1.0`) and GitHub Actions
publishes to PyPI via Trusted Publishing — see `RELEASING.md` for the one-time
setup.

## License

[Apache-2.0](LICENSE) © Rushikesh Patade.
