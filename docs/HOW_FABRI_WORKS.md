# How fabri works — architecture deep-dive

## The one-sentence version

fabri is a self-improving agent engine: every run writes a trace, the trace
is mined for failures (and successes), each finding is compressed into a
short guideline and stored in a vector memory store, and the next run
retrieves the most relevant guidelines and injects them into the system
prompt — so agents get cheaper and more reliable over time without anyone
editing the prompt by hand.

---

## Layer map

```
┌──────────────────────────────────────────────────────────────┐
│  CLI / service  (cli.py, service/)                           │
│  Entry points: fabri run, fabri replay, fabri serve          │
└──────────────────────────┬───────────────────────────────────┘
                           │ load_config → build_* helpers
                           ▼
┌──────────────────────────────────────────────────────────────┐
│  Runtime assembly  (runtime.py)                              │
│  build_tools()  build_llm()  build_memory_store()            │
│  build_run_llms() → main / decompose / planner / narrator    │
└──────┬───────────────────────────┬───────────────────────────┘
       │                           │
       ▼                           ▼
┌─────────────┐         ┌──────────────────────────┐
│ ToolRegistry│         │  Memory store             │
│ (registry.py│         │  QdrantMemoryStore        │
│  + sandbox) │         │  SqliteMemoryStore        │
└──────┬──────┘         └──────────────┬────────────┘
       │                               │
       │         ┌─────────────────────┘
       ▼         ▼
┌──────────────────────────────────────────────────────────────┐
│  Agent loop  (core/agent.py → run_agent)                     │
│   1. retrieve guidelines from memory                          │
│   2. build system prompt (identity + tools + guidelines)      │
│   3. ReAct step loop: llm.step → tool dispatch → repeat      │
│   4. planner / decompose / narrator (optional roles)         │
│   5. repair loop (optional: verify → re-run on failure)      │
└──────────────────────────┬───────────────────────────────────┘
                           │ JSONL trace written each step
                           ▼
┌──────────────────────────────────────────────────────────────┐
│  Memory pipeline  (orchestrator/pipeline.py)                 │
│  process_trace():                                            │
│   • mine failures → synthesize_guideline (LLM, ≤30 tokens)  │
│   • mine successes → synthesize_success_pattern              │
│   • record postmortem (deterministic, no LLM)                │
│   • ingest_guideline → dedup / merge / promote               │
└──────────────────────────────────────────────────────────────┘
```

---

## Phase 1 — Task arrives → retrieval

`run_agent(task, llm, tools, store)` is the main entry point.

**Retrieval** (`orchestrator/retrieval.py`):

```
embed(task) → vector
    │
    ├── dense query (cosine / Qdrant or sqlite-vec)
    ├── sparse query (BM25 — SQLite FTS5 or client-side rank_bm25)
    │       → RRF fusion when strategy = "hybrid"
    ├── temporal decay  (score *= exp(-age / half_life))
    ├── importance boost (score *= 1 + w * hit_count_signal)
    ├── domain routing  (1.15× boost when entry.domain == query domain)
    ├── tag-filter  (guaranteed slot for tool-named entries above floor)
    └── MMR diversification (strategy = "hybrid+mmr")
                │
                ▼
        top-k MemoryEntry list
```

The result is formatted as a fenced block:

```xml
<retrieved_guidelines note="Hints mined from past runs. Reference only …">
- [strategic] Always pass line_start/line_end when reading large files.
- [tactical] edit_file requires the old_string to be unique in the file.
</retrieved_guidelines>
```

The fence + `note=` attribute are a prompt-injection countermeasure: the
model reads it as reference data, not as operator commands.

---

## Phase 2 — System prompt construction

`build_system_prompt()` assembles these sections (only those that apply):

| Section | When included |
|---|---|
| `system_prompt_prefix` | always (custom preamble) |
| identity / `system_prompt` | always |
| tool list | when any tools are registered |
| `FILE_EDIT_POLICY` | when both `edit_file` and `write_file` are visible |
| `FRUGALITY_POLICY` | always |
| `DELEGATION_POLICY` | when `spawn_subagent` is visible |
| `CODE_ACTION_POLICY` | when `python_exec` or `batch` is visible |
| `TOON_RESULT_NOTE` | when `result_format = "toon"` |
| retrieved guidelines block | when the store has any matching entries |

The guidelines block is always last so it stays close to the model's working
memory and the prompt cache prefix covers the static sections.

---

## Phase 3 — The ReAct step loop

```
messages = [{"role": "user", "content": task}]

for step in range(max_steps):
    if budget_breached():
        → BUDGET_EXCEEDED

    response = llm.step(system, messages)

    if response.tool_calls:
        _dispatch_tool_calls(...)   # appends assistant + user turns
        continue

    if response.final_text:
        → SUCCESS (possibly after structured-output validation)

    raise AgentProtocolError   # no tools AND no text = malformed
```

**Tool dispatch** (`_dispatch_tool_calls`):

- Serial by default; `spawn_subagent` calls sharing a `parallel_group` arg
  fan out via `ThreadPoolExecutor(max_workers ≤ max_parallel_spawns)`.
- Each call returns `{ok, result?, error?}`. A raising future is normalized
  to `tool_error(...)` — an unpaired `tool_use` block would 400 the API.
- Results are encoded in **TOON** (compact key:value / table format) before
  entering the model context to save input tokens; the trace keeps raw JSON.
- The assistant turn echoes all `tool_use` blocks; the user turn has all
  `tool_result` blocks — both required by the Anthropic API protocol.

**Special tool names**:

| Name | Behaviour |
|---|---|
| `decompose` | Inline meta-call: breaks the task into sub-questions via a separate LLM call (decompose role), returns a list |
| `spawn_subagent` | Forks a new fabri subprocess with its own session, tools, and budget; cost rolls up to parent |
| `ask_user` | Blocks on a Unix socket read with a timeout; default answer used if silent |
| `batch` | Dispatches a list of `{name, args}` calls in one turn (no nested batch / spawn_subagent / ask_user) |

---

## Phase 4 — Optional orchestration layers

### Planner (`core/planner.py`)

Engaged when `planner_mode = "force"` or `"auto"` (task length ≥ threshold).
One LLM call ahead of the loop emits:

```json
{"items": [
  {"goal": "...", "artifacts": ["file.py"], "depends_on": [], "tool_hints": ["write_file"]},
  {"goal": "...", "artifacts": ["test.py"], "depends_on": [0], "tool_hints": []}
]}
```

The executor runs `_run_step_loop` once per item in topological order, with
a per-item step budget (`remaining_steps / items_left`). Each item gets a
minimal context (no prior item's full tool_result history), cutting token
cost on multi-domain tasks.

### Decompose (`core/decompose.py`)

A flat alternative to the planner: the `decompose` tool breaks one task into
N sub-questions, each answered by a separate LLM call (or mini-agent run).
Used inline; the model decides when to invoke it.

### Narrator (`core/agent.py`)

A cheap model (e.g. Gemini Flash-Lite) that emits one short sentence per
step describing what the agent is doing. Optional; never breaks the run on
failure.

### Repair loop (`core/agent.py → _run_with_repair`)

After the initial run, if `agent.repair.enabled`, a verify → repair → rerun
loop runs:

```
initial run → verdict (verify_command or outcome) → if fail:
    inject verifier output into a repair_task → re-run (fresh step budget)
    → repeat until ok / max_attempts / error_signature unchanged
```

All attempts share one `session_id` → one trace.

---

## Phase 5 — Memory pipeline (the learning loop)

After a run, `process_trace(session_id, store, llm)` mines the JSONL trace:

### What gets mined

| Signal | Source | LLM call? |
|---|---|---|
| Failure guideline | Each `tool_call` event where `result.ok == False` | Yes — `synthesize_guideline` (≤30 tokens) |
| Success pattern | Final event + at least one successful tool call | Yes — `synthesize_success_pattern` |
| Postmortem | Whole run (deterministic: task + outcome + counts + error sigs) | No |
| Discrepancy | `discrepancy` events (write then read mismatch) | No — fixed phrasing |

### Dedup / merge / promote (`memory/pruning.py`)

`ingest_guideline(store, text, session_id, kind)`:

1. `store.find_similar(text, threshold=0.85)` — cosine search.
2. **Hit**: bump `hit_count`, union `tools`, add `session_id`.
   - If `len(distinct session_ids) >= 3` → **promote to strategic**.
   - A strategic entry is never demoted.
3. **Miss**: insert new `MemoryEntry` with `kind="tactical"`.

`success_pattern` and `postmortem` entries dedup against their own kind
only, so a "what worked" entry never merges into a failure guideline.

The critical section (find → update → upsert) is protected by a per-collection
`fcntl.flock` so concurrent parent + sub-agent ingests don't lose updates.

### Memory entry schema (`memory/schema.py`)

```python
@dataclass
class MemoryEntry:
    id: str            # deterministic SHA-256 of text (idempotent inserts)
    text: str
    kind: str          # tactical / strategic / success_pattern / postmortem
    session_ids: list[str]
    hit_count: int
    tools: list[str]
    tags: list[str]
    domain: str        # auto-classified: code / search / planning / api / generic
    outcome: str       # success / failure / unknown
    created_at: float
    agent_id: str | None
    task_embedding_hash: str | None
```

---

## Memory backends

| Backend | File | Use case |
|---|---|---|
| `qdrant` | `memory/store.py` | Networked, multi-process safe, production default |
| `sqlite` | `memory/embedded_store.py` | Single-file, zero infrastructure, FTS5 BM25 built-in |

Both expose the same interface (`upsert`, `find_similar`, `query_by_vector`,
`count`). The retrieval layer routes to `query_bm25` on the SQLite path when
present.

---

## Tool system

A tool is a **JSON manifest** + an **executable** in any language:

```
tools/
  read_file.json       # manifest: name, description, input_schema, command
  read_file.py         # executable: reads stdin JSON, prints stdout JSON
```

`manifest_schema.py` validates the manifest. The runner (`tools/runner.py`):

1. Writes `json.dumps(args)` to stdin of the tool subprocess.
2. Reads stdout, capped at 1 MiB (`RUNNER_OUTPUT_CAP_BYTES`); truncated
   results carry `"truncated": true`.
3. Kills the child's **process group** on timeout (`os.killpg(SIGKILL)`).
4. Returns `{ok, result?, error?, truncated?}`.

`FABRI_SANDBOX_ROOT` is passed per-invocation via `env=` (not `os.environ`),
so two concurrent registries with different sandbox roots don't clobber each
other.

**MCP tools** are registered as in-process callables backed by a long-lived
`MCPStdioClient` subprocess. The registry holds a reference to the client
so it isn't GC'd.

---

## LLM backends (`core/llm.py`)

| Provider | Backend class | Auth |
|---|---|---|
| Anthropic | `AnthropicLLMBackend` | `ANTHROPIC_API_KEY` |
| OpenAI | `OpenAILLMBackend` | `OPENAI_API_KEY` |
| OpenRouter | `OpenAILLMBackend` + `base_url` | `OPENROUTER_API_KEY` |
| Google Gemini | `GeminiLLMBackend` | `GEMINI_API_KEY` |
| AWS Bedrock | `BedrockLLMBackend` | boto3 chain (env / profile / IAM) |

All backends implement the same `LLMBackend.step(system, messages) → LLMResponse`
protocol. The Anthropic backend collects **all** `tool_use` content blocks
from a response (parallel tool calls); the OpenAI backend translates the
Anthropic-shaped history into `assistant.tool_calls` + `role:"tool"` messages
before each call.

Transient errors retry with exponential backoff; unrecoverable errors become
`LLMError` → `Outcome.FAILED`. `max_tokens` truncation raises `LLMError`
immediately rather than treating a cut-off response as a final answer.

---

## Outcomes

```python
class Outcome(StrEnum):
    SUCCESS                    = "success"
    SUCCESS_WITH_RECOVERY      = "success_with_recovery"   # had a tool failure but still finished
    INCOMPLETE                 = "incomplete"               # ran out of steps cleanly
    INCOMPLETE_WITH_TOOL_FAILURE = "incomplete_with_tool_failure"
    FAILED                     = "failed"                  # LLMError / budget / verifier
    BUDGET_EXCEEDED            = "budget_exceeded"
    INVALID_OUTPUT             = "invalid_output"          # response_schema not satisfied
```

---

## Cost accounting

Every `llm.step` call returns `LLMUsage(input, output, cache_creation,
cache_read, model)`. Usage accumulates per-model so `cost_for(bucket)` prices
a mixed-model run (Sonnet orchestrator + Haiku decompose) at each model's own
rate. Delegated child costs bubble up through both static agent-tool and
`spawn_subagent` result usage; `total_cost_usd = own_cost +
subagent_cost_total` is the true end-to-end COGS.

---

## Data flow summary

```
task
  │
  ├─ embed(task) ──► memory store ──► top-k guidelines
  │                                        │
  ▼                                        ▼
system prompt = identity + tools + policies + guidelines
  │
  ▼
step loop:
  llm.step → tool_calls → tools.invoke → tool results → llm.step → …
                                                                │
  ◄─────────────────────────────────── final_text ─────────────┘
  │
  ▼
JSONL trace (.fabri/traces/<session_id>.jsonl)
  │
  ▼
process_trace():
  failures → synthesize_guideline → ingest (dedup/promote)
  successes → synthesize_success_pattern → ingest
  postmortem → ingest (deterministic, no LLM)
  │
  ▼
memory store (retrievable in the next run)
```

---

## Open items (as of v0.9.1)

**Retrieval:**
- Query expansion — `memory.query_expansion` reserved but not implemented.
- Cross-encoder reranking — deferred until user demand is clear.
- Agent-scoped memory namespacing — `agent_id` stored but not used for routing.

**Security (deferred):**
- MCP remote tool descriptions flow into the system prompt verbatim (injection risk).
- MCP server mode unauthenticated + unbounded (`mcp_server.py`).
- Tool subprocesses inherit full `os.environ` incl. provider keys.
- MCP stdio `_read` has no timeout.
- `grep_dir` recipe reads any path (no sandbox jail).

**Test coverage:**
- Wheel-packaging guard for `builtin` tools after a real `pip install`.
- Planner item-budget division e2e test.
- `max_tokens` truncation with partial `tool_use` args.
