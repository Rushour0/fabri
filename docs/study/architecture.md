# How fabri works: core architecture

*Study doc, written 2026-07-24 from `docs/HOW_FABRI_WORKS.md`, `docs/agency-kernel.md`,
`README.md`, and the source under `src/fabri/`. Git history: 306 commits,
first commit 2026-06-19, latest 2026-07-24 — a five-week build.*

---

## 1. The elevator pitch

fabri is an **agent engine** you build agents on top of (comparable in role
to LangGraph/CrewAI), built around one bet: **an agent's system prompt
should not be written by hand and frozen — it should grow from what the
agent actually does.**

Mechanism: every run writes a structured trace. After the run, a mining
pipeline reads the trace, compresses each failure (and separately each
success) into a short guideline via an LLM call, dedups it against what's
already stored, and if the same lesson recurs across 3+ independent
sessions, promotes it from throwaway "tactical" to permanent "strategic."
The next run retrieves the most relevant guidelines before doing anything
and stitches them into its system prompt. Nobody edits the prompt by hand.

fabri splits into two layers: the **engine** (ReAct loop, per-role LLMs,
polyglot tools, the memory loop — `src/fabri/core/`, `memory/`,
`orchestrator/`, `tools/`) and the **builder** (scaffolding that turns
intent into a running agent fast — `fabri new agency`, `fabri init`,
Fabri Studio). It ships as an installable PyPI package, Apache-2.0
licensed, embeddable in other products (ludexel uses it).

---

## 2. The core agent engine

**Where it lives:** `core/agent.py` (1,524 lines — `run_agent()`, the ReAct
loop, tool dispatch, repair loop) · `core/llm.py` (1,252 lines — the five
LLM backends) · `core/planner.py`, `decompose.py` · `runtime.py` (286
lines — turns a loaded config into live objects: `build_tools()`,
`build_llm()`, `build_memory_store()`, `build_run_llms()`; both the CLI and
the agent-as-tool adapter go through this one module) · `config.py` (665
lines — defaults + per-role LLM normalization).

### The ReAct step loop

`run_agent(task, llm, tools, store)`, per step: check budget (steps/cost) →
`llm.step(system, messages)` → if tool calls, dispatch and loop; if final
text, done; if neither, `AgentProtocolError`. Dispatch is serial by
default; `spawn_subagent` calls sharing a `parallel_group` tag fan out via
`ThreadPoolExecutor`. Every tool result normalizes to `{ok, result?,
error?}` — even a raised exception, because an unpaired `tool_use` block
would 400 the Anthropic API. Tool results enter the model's context
**TOON-encoded** (compact key:value/table format, `toon.py`) to save input
tokens; the trace keeps raw JSON.

Four tool names get special handling:

| Name | Behavior |
|---|---|
| `decompose` | Splits the task into sub-questions via a separate LLM call/role |
| `spawn_subagent` | Forks a new fabri subprocess, own session/tools/budget; cost rolls up to parent |
| `ask_user` | Blocks on a Unix socket read with timeout; falls back to a default answer |
| `batch` | Dispatches a list of `{name, args}` calls in one turn; nested `batch`/`spawn_subagent`/`ask_user` refused outright |

### Optional layers on top

- **Planner** (`core/planner.py`) — one LLM call emits topologically ordered
  goal items; the loop runs once per item with a shrinking step budget.
- **Decompose** — flatter alternative: the model calls `decompose` mid-run.
- **Narrator** — cheap model (Gemini Flash-Lite by default) narrates one
  sentence per step for a host UI; never breaks the run on failure.
- **Repair loop** (`_run_with_repair`) — verify → repair → rerun with fresh
  step budget, until pass / max_attempts / unchanged error signature.
  **Important**: repair is a retry mechanism, not an acceptance gate — after
  retries exhaust, fabri returns the parent's last result *regardless of
  whether the final verifier check passed*. Read the verifier's own output;
  CLI success is not proof.

### Frugality philosophy

The README is explicit about why the defaults are shaped this way, citing
external research (Anthropic's "Building a multi-agent research system,"
Cognition's "Don't Build Multi-Agents," CodeAct arXiv:2402.01030):
`spawn_subagent` re-runs the entire loop in a new process, costing roughly
**~15x** a single call, because coordination overhead — not capability — is
the top failure mode of multi-agent systems. It's reserved for work that's
independent, parallelizable, and large enough to overflow the parent's
context.

| Primitive | Use when | Cost |
|---|---|---|
| `batch` | N known calls, no branching | 1x |
| `python_exec` | N calls with branching/loops | 1x |
| `spawn_subagent` | Independent subtask overflowing parent context | ~15x |

### Outcomes

Seven typed outcomes (`core/outcome.py`): `SUCCESS`,
`SUCCESS_WITH_RECOVERY` (finished but a tool call failed en route),
`INCOMPLETE`, `INCOMPLETE_WITH_TOOL_FAILURE`, `FAILED`, `BUDGET_EXCEEDED`,
`INVALID_OUTPUT`. This is what the memory pipeline and `fabri report` key
off. **Documented caveat**: outcome and output quality are separate
signals — `success` only means execution completed, not that the artifact
met its rubric (`agency-kernel.md`). This distinction exists because it was
a real trust gap: an earlier version of the outcome classifier reported
`SUCCESS` even on total tool failure (fixed per the memory notes, with a
verifier gate added on top).

---

## 3. Agencies and companies — multi-agent composition

Quoting `docs/agency-kernel.md` directly: *"An agency is a small product
convention for fabri, not a new execution mode."* No new primitives.

### The kernel

```
intake -> inspect -> plan -> delegate -> execute -> verify
                                      ^              |
                                      |--- repair ---|
                                             |
                                      approve -> deliver -> learn
```

Every label maps to something that already exists: inspect/plan = the
parent's own ReAct loop (+ optional planner); delegate = `tools.agents[]`
exposing a specialist config as an ordinary tool; execute = built-in or
custom (manifest + executable) tools; verify/repair = `agent.repair` + a
deterministic verify command; learn = the existing memory pipeline.

A specialist is just another `agent.yaml`, wired in via `tools.agents[]`.
Each gets a fresh child session and its own trace; the parent sees a
concise tool result, not the child's whole conversation. Child usage rolls
up recursively into the parent's `subagent_cost_usd`/`total_cost_usd`, so a
multi-level company (root → director → crew → specialist) reports one true
end-to-end cost at the top. The doc is explicit about what fabri does
**not** ship: no first-class agency object, no durable workflow state
machine, no automatic prompt-file loader, no human-approval UI — approval
gates must be named explicitly in the agency's own contract.

### Companies (`src/fabri/company.py`, 603 lines)

A compiler: reads a `company.toml` describing a management tree
(`report_to` pointers) and compiles it into nested `agent.yaml` configs
wired via `tools.agents[]`. Validation before compiling anything: exactly
one root (`report_to == ""`); the graph is a tree (cycle/unreachable-node
detection via explicit DFS); response-schema keys legal only on root; a
leaf (`agency` field) can't also have reports; `max_cost_usd`/`timeout_s`
must be positive finite numbers, explicitly rejecting booleans (a bool is
an `int` subclass in Python — a naive `isinstance` check would silently
accept `True`/`False` as 1/0, and the code calls this out in a comment).

Three post-compile passes worth knowing:

- **`_apply_company_llm_defaults`** — scaffolded templates often omit
  `llm:` and would silently inherit fabri's Gemini default; a compiled
  company defaults every role to OpenAI `gpt-5.6-terra` so it doesn't fail
  only at delegation time because one specialist landed on the wrong
  provider.
- **`_apply_company_action_scope`** — stamps each role with an explicit
  `{company, agency, role}` scope for ActionMemory recurrence matching
  (collection-name strings alone can't be parsed back reliably).
- **`_apply_company_memory_root`** — anchors specialist SQLite memory paths
  under a durable root outside the compiled tree, so memory survives a
  re-scaffold.

Manager child-calls default to a 900s (15-minute) timeout, deliberately
generous because a manager's call blocks until its **entire subtree**
resolves, and a single agent's default (120s) is far too short once you
have root→director→crew→leaf nesting.

Companies also carry an `<AGENT_MEMORY>` block convention (a prompt
instruction asking every successful run to append a
TASK/OUTCOME/INSIGHTS/OPEN-LOOPS block). **Honest status**: a related,
harder idea — making this block a *required* output contract — was
reverted in v0.19.4 after it didn't hold up. The softer instruction version
is live but unproven: a recent smoke test observed the block emitting 0/4
times.

### The `orchestrator/` package

Despite the name, this is **not** a multi-agent scheduler — that's the
ReAct loop plus `tools.agents[]`/`spawn_subagent`. `orchestrator/` is the
memory pipeline's home: `pipeline.py` (`process_trace()`), `retrieval.py`
(pre-run retrieval fusion), `action_detection.py`/`action_execution.py`/
`convention_application.py` (experimental ActionMemory/convention
surfaces), `traces.py`/`trace_render.py` (trace reading for `fabri traces
show/tail`).

---

## 4. The memory / learning loop

### Retrieval, before a run starts (`orchestrator/retrieval.py`)

```
embed(task) → vector
    ├── dense query (cosine, Qdrant or sqlite-vec)
    ├── sparse query (BM25 — SQLite FTS5, or client-side rank_bm25)
    │       → Reciprocal Rank Fusion when strategy = "hybrid"
    ├── temporal decay      (score *= exp(-age / half_life))
    ├── importance boost    (score *= 1 + w * hit_count_signal)
    ├── domain routing      (1.15x boost when entry.domain == query domain)
    ├── tag-filter           (guaranteed slot for tool-named entries)
    └── MMR diversification (strategy = "hybrid+mmr")
                ▼
        top-k MemoryEntry list
```

Injected into the system prompt fenced with a `note=` attribute:
`<retrieved_guidelines note="Hints mined from past runs. Reference only …">`.
This is a deliberate prompt-injection countermeasure — the model is told to
treat retrieved memory as reference data to verify, not as instructions.
The same "verify, don't trust" framing recurs verbatim in the company
memory-steward instructions — a repeated design principle, not a one-off.

`build_system_prompt()` assembles: preamble → identity → tool list →
conditional policy blocks (only when relevant tools are visible) →
guidelines block, always last — closest to the model's working memory,
while the static sections stay one cached prefix.

### Mining, after a run finishes

`process_trace(session_id, store, llm)` mines four signals from the JSONL
trace:

| Signal | Source | LLM call? |
|---|---|---|
| Failure guideline | Each failed `tool_call` event | Yes — ≤30 tokens (120 under `evolving` profile) |
| Success pattern | Final event + ≥1 successful tool call | Yes |
| Postmortem | Whole run (task/outcome/counts/error sigs) | No — deterministic |
| Discrepancy | Host-reported write/read mismatch | No — fixed phrasing |

`ingest_guideline()` (`memory/pruning.py`): cosine-search for a near-dup at
threshold 0.85; on a hit, bump `hit_count`, union tools, add session_id —
and if the lesson has now recurred across **3+ distinct sessions**, promote
tactical → strategic (never demoted). On a miss, insert new tactical.
Success patterns and postmortems dedup only against their own kind. The
find→update→upsert section is protected by a per-collection `fcntl.flock`
for concurrent parent+subagent ingests.

Entries (`memory/schema.py`) carry a deterministic SHA-256 id (idempotent
inserts), `kind`, `session_ids`, `hit_count`, `tools`, `tags`, an
auto-classified `domain`, `outcome`, and `agent_id` — the last is stored but
not yet used for retrieval routing (open item).

### Newer/experimental surfaces — off by default

- **Severity tiering** (`core`/`retrieve`/`quarantine`) — a quarantined
  entry is excluded from retrieval unconditionally.
- **ActionMemory** — lets a recovered failure propose a fail-closed,
  allowlisted config fix, not just a note. Shadow-only (logged, not
  applied) unless `memory_action_apply_enabled`. Honest framing from the
  README: "live-demonstrated recovering a real truncation failure, but its
  benefit over a frozen control is not yet proven."
- **Convention mining** (merged 2026-07-24, PR #83/#84 — the newest thing
  in the repo as of this doc) — captures declared multi-branch protocols,
  applies them through a deterministic branch-selection validator.
  Convention *approval* stays exact-hash and human-gated, never
  profile-set.

`memory.profile: evolving` is a one-line shortcut turning on the whole
guarded bundle (120-token guidelines, tiering, convention mining,
postmortems, evidence-gated success patterns, shadow ActionMemory) while
keeping human-authority gates (convention approval, action-apply, core
prompt placement) un-settable by the profile.

### Two backends, one interface

`qdrant` (`memory/store.py` — networked, multi-process safe, "production
default") and `sqlite` (`memory/embedded_store.py` — single-file, FTS5 BM25
built in, what `fabri[sqlite]` gets you with no Docker). Both expose the
same `upsert`/`find_similar`/`query_by_vector`/`count` interface.
Embeddings run locally via `sentence-transformers/all-MiniLM-L6-v2` — no
embedding API calls.

### What's proven vs. still a claim — the honest scorecard

| Evidence | Result |
|---|---|
| Company setup qualification | Support HQ passed a small-sample 3/3 gate, but a 10-replica confirmation scored 9/10 (~90%) — the larger sample **overturned** the small-sample pass. Reliability Labs (2/3) and Revenue Ops (0/3 + a truncation failure) also didn't qualify. **At adequate sample size, none of the three cleared 100% reliability — explicitly not a memory-benefit result.** |
| Offline retrieval evaluation | Hybrid retrieval: 0.938 recall@5, 0.844 MRR, vs. dense-only 0.792/0.790 — real, on a labeled fixture, not live. |
| Session-N+1 recovery pilot | Steps 5→4, guideline reuse 0%→67%, cost ↓7.8%, six-run task on gpt-4o-mini — real but not a general-workload claim. |
| LongMemEval | Runner shipped; no score claimed yet. |

The retrieval *mechanism* is measurably better than naive dense search, and
there's a real small cost-reduction pilot. But the bigger claim — "a
company of agents gets meaningfully more reliable because it has memory" —
was tested at adequate sample size and **did not hold up**. That's a
negative result, documented rather than hidden.

---

## 5. The tool layer

**Contract**: a JSON manifest + an executable in any language. Stdin gets
JSON args, stdout returns one JSON object, exit code signals success/
failure. `tools/manifest_schema.py` validates manifests; `tools/runner.py`
(109 lines) drives execution: writes args to stdin, caps stdout at 1 MiB
(over-cap results get `"truncated": true`), kills the child's whole
**process group** on timeout (`os.killpg(SIGKILL)`, not just the immediate
child), normalizes everything into `{ok, error?, result?, stderr?}`.

**Registry** (`tools/registry.py`) discovers manifests by globbing
`manifest_dir`, plus `register()` for programmatic manifests (agent-as-tool
entries) and `register_callable()` for in-process handlers (MCP tools —
the registry holds a reference to the owning client so it isn't GC'd).
`invoke_batch()` backs the `batch` meta-tool and explicitly refuses to nest
`batch`/`spawn_subagent`/`ask_user` inside a batch call.

**Sandboxing**: `FABRI_SANDBOX_ROOT` (from `tools.sandbox_root`) is passed
per-invocation via subprocess `env=`, deliberately not `os.environ`, so
concurrent registries with different sandbox roots (parent vs. sub-agent)
don't clobber each other. `read_file`/`write_file` resolve every path
against that root and reject anything escaping it. `sandbox/` defines a
`Sandbox` ABC: `LocalSandbox` (default) and `DockerSandbox` (warm container
pool, `Dockerfile.base`) for stronger isolation.

**Documented, currently-deferred security gaps** (from
`HOW_FABRI_WORKS.md`'s "Open items" — real, not hidden): MCP remote tool
descriptions flow into the system prompt verbatim (injection risk);
`mcp_server.py` server mode is unauthenticated and unbounded; tool
subprocesses inherit the full `os.environ` including provider keys; MCP
stdio `_read` has no timeout; `grep_dir` reads any path with no sandbox
jail.

**MCP tools** register as in-process callables backed by a long-lived
`MCPStdioClient`; a server that fails to start is logged and skipped, not
fatal to the whole build. Remote tools namespace as `mcp_<server>_<tool>`.

---

## 6. The LLM layer

`core/llm.py` implements one shared `LLMBackend.step(system, messages) →
LLMResponse` protocol across five providers:

| Provider | Backend class | Auth |
|---|---|---|
| Anthropic | `AnthropicLLMBackend` | `ANTHROPIC_API_KEY` |
| OpenAI | `OpenAILLMBackend` | `OPENAI_API_KEY` |
| OpenRouter | `OpenAILLMBackend` + `base_url` | `OPENROUTER_API_KEY` |
| Gemini | `GeminiLLMBackend` | `GEMINI_API_KEY` |
| AWS Bedrock | `BedrockLLMBackend` | boto3's standard chain — one backend serves every Converse-capable model (Claude, gpt-oss, Llama, Mistral…) |

Anthropic backend collects all `tool_use` blocks per response (parallel
tool calls); OpenAI backend translates fabri's Anthropic-shaped history
into OpenAI's `assistant.tool_calls`/`role:"tool"` shape — this translation
is what keeps the rest of the codebase provider-agnostic. Transient errors
retry with backoff; unrecoverable → `LLMError` → `Outcome.FAILED`.
`max_tokens` truncation raises immediately rather than being accepted as a
valid final answer.

Default provider is Gemini (cheapest, generous free tier), but every SDK
ships in the base install — switching is a one-line config change. Each of
the four roles (main/decompose/planner/narrator) can point at a different
provider independently.

### Cost / COGS tracking (`pricing.py`, 212 lines)

Rates resolve **dynamically per lookup** from litellm's `model_cost` map as
the primary source; the hand-maintained `PRICING` dict is only a fallback
for models litellm doesn't know (e.g. Bedrock's Kimi ids). Lookup tolerates
date-suffixed ids via longest-prefix match. Anthropic cache economics are
modeled explicitly: cache **write** at 1.25x input rate, cache **read** at
0.10x, applied uniformly across providers.

**Load-bearing invariant, stated directly in the docstring**: `input_tokens`
must exclude cached tokens (which live in their own buckets), so the four
usage buckets sum without double-counting. This holds natively for
Anthropic/Bedrock; OpenAI's `prompt_tokens` and Gemini's
`prompt_token_count` both include cached tokens as a subset, so those two
backends explicitly subtract cached tokens to normalize. The docstring
spells out the failure mode if a new backend gets this wrong: cached tokens
billed at ~1.10x instead of 0.10x, an ~11x overcharge on the cached portion.
**This isn't hypothetical** — per the project's memory notes, exactly this
bug shipped and was fixed (PR #14, 2026-07-15); the elaborate docstring is
a scar turned into a guardrail.

`cost_for(usage)` returns `None` (with a logged warning) for an unpriced
model rather than silently reporting `$0` — an unknown cost and a genuinely
free run are different facts. `LLMUsage` accumulates per-model, so a mixed
run (Sonnet orchestrator + Haiku decompose) prices each model at its own
rate. Delegated child costs (static `tools.agents[]` and dynamic
`spawn_subagent`) bubble up so `total_cost_usd = own_cost +
subagent_cost_total` — the number Fabri Studio's COGS panel and `fabri
report` key off. `EventType.COST_UNACCOUNTED` exists for the case where a
sub-agent crashes before emitting usage — tokens hit the provider bill but
can't roll into `total_cost_usd`; the event flags "real COGS may exceed the
recorded number" instead of silently under-reporting.

---

## 7. The service / serve layer

`src/fabri/service/` turns the engine into something a separate frontend
can drive over HTTP.

- `service.py` (742 lines) — `FabriService`: launching runs, tracking
  sessions, fleets, the `ask_user` inbox.
- `http_server.py` (540 lines) — **stdlib-only** transport
  (`ThreadingHTTPServer`, no external framework, no websockets dep), built
  threaded specifically so a long-lived streaming `/events` connection
  doesn't block a concurrent `POST /runs`. Key endpoints: `POST /runs`
  (launch), `GET /runs` (history, survives restart), `GET
  /runs/<id>/events` (**Server-Sent Events** — one `data:` frame per trace
  event, ending in a terminal `event: result` frame with the result + cost
  envelope), `GET /runs/<id>/result` (blocking convenience), `POST
  /runs/<id>/answer` (reply to a mid-run `ask_user` question), `POST
  /runs/<id>/cancel`, `POST /fleets` / `GET /fleets` (batch fan-out with
  summed COGS roll-ups), `GET /company` (org chart), `GET /catalog`.
- `ask_user_listener.py` — server side of the Unix-socket `ask_user`
  protocol; what lets a browser "answer this question" button unblock a
  running agent process.
- `run_store.py` (history persistence), `binding.py`/`auth.py` (bind +
  cookie session auth), `sync.py`/`tailer.py` (live trace-tailing),
  `slack_events.py`/`slack_notify.py` (Slack integration), `studio_assets/`
  (the pre-built Fabri Studio React+Vite bundle, served same-origin by
  `fabri studio`).

The trace-event vocabulary streamed over SSE (`src/fabri/events.py`) is one
centralized `EventType(str, Enum)` — a string enum so
`EventType.THOUGHT.value == "thought"` and the wire format stays
byte-identical to what an existing consumer (ludexel) expects. Centralized
specifically because event-type strings used to be hand-duplicated across
emission/consumption sites in two repos, where a typo on either side
silently dropped an event. Notable recent additions: `DISCREPANCY` (host
detects a claimed-but-not-landed change, mined into a guideline),
`RETRIEVAL` (trace-only, zero-token record of exactly what retrieval fusion
decided — the debug surface for "why is retrieval weak"), `MINING_REPORT`
(post-run mining funnel with reason-coded skips).

---

## 8. End-to-end: how one run flows

```
1. CONFIG   load_config() reads agent.yaml, applies defaults, normalizes
            per-role LLM settings (main/decompose/planner/narrator)

2. COMPILE  runtime.py → live objects:
            build_tools() → ToolRegistry (sandboxed, MCP-aware)
            build_memory_store() → Qdrant or Sqlite store
            build_run_llms() → {llm, decompose_llm, planner_llm, narrator_llm}

3. RUN      core/agent.py: run_agent(task, llm, tools, store)
              → retrieve top-k guidelines for the task
              → build_system_prompt() (identity + tools + policies + guidelines)
              → ReAct step loop: llm.step → tool dispatch → repeat
                  (special tools: decompose / spawn_subagent / ask_user / batch)
              → optional planner/decompose pass, optional narrator commentary
              → optional repair loop (verify → repair → rerun)
              → resolves to a typed Outcome

4. TRACE    every step appends a JSONL event to .fabri/traces/<session_id>.jsonl
            (.fabri/logs/<session_id>.log carries DEBUG detail: LLM
            latency/tokens, tool dispatch latency, dedup/promotion decisions)

5. MEMORY   orchestrator/pipeline.py: process_trace(session_id, store, llm)
              → mine failures → synthesize_guideline (LLM, ≤30/120 tokens)
              → mine successes → synthesize_success_pattern
              → record postmortem (deterministic)
              → ingest_guideline(): dedup / merge / promote

6. NEXT RUN the newly stored guidelines are retrievable starting with the
            very next retrieve_context call — no prompt file was edited
```

Every `llm.step` call emits a `usage` trace event carrying token totals and
`cost_usd`, so `total_cost_usd` (own + rolled-up subagent cost) is
computable without parsing logs — the number `fabri report --since 1h`
aggregates to answer "is this actually getting cheaper per run," which is
the point of the whole exercise. `.fabri/` is meant to be gitignored —
runtime state, not source.

---

## 9. Key decisions and why

1. **Learning happens at the trace/memory layer, not by editing prompts** —
   the entire premise (§1, §4).
2. **TOON encoding for tool results, JSON for the trace** — the model only
   sees the compact form; the trace keeps full fidelity for mining (§2).
3. **`spawn_subagent` is the expensive exception; `batch`/`python_exec` are
   the cheap defaults** — backed by cited external research, not intuition
   (§2).
4. **Agencies/companies are a naming convention over existing primitives**,
   deliberately avoiding a new workflow state machine (§3).
5. **Guidelines are fenced and marked "reference only, verify"** — a
   deliberate prompt-injection countermeasure, repeated in two separate
   places in the codebase (§3, §4).
6. **Sandbox root threaded via subprocess `env=`, never `os.environ`** —
   lets concurrent registries with different sandbox scopes coexist (§5).
7. **Pricing sourced dynamically from litellm**, hand-maintained table only
   as fallback — stays current without hand-tracking every model release
   (§6).
8. **The input/cache-token invariant is enforced per-backend with an
   explicit docstring warning** — written after that exact bug shipped and
   got fixed (~11x cache-token overcharge). A scar turned into a guardrail
   (§6).
9. **Unknown model pricing returns `None`, never `$0`** — distinguishes "we
   don't know the cost" from "the cost was genuinely zero" (§6).
10. **Tactical→strategic promotion requires 3+ distinct sessions and is
    never reversed** — a deliberate bar against promoting a one-off fluke
    into a permanent guideline (§4).

---

## 10. Current state

**Solidly shipped and load-bearing**: the ReAct loop with its four special
tools, five LLM provider backends behind one protocol, the tool
manifest+runner contract with sandboxing, the retrieval/mining/dedup/
promotion memory loop on both Qdrant and SQLite, the company TOML compiler
with real validation, the SSE-based HTTP service and Fabri Studio's four
surfaces (Conversation/Company/Fleet/History), per-model per-role cost
accounting with recursive subagent rollup.

**Proven with real (modest) numbers**: hybrid retrieval measurably beats
dense-only search on a deterministic fixture (0.938 vs 0.792 recall@5); a
six-run pilot showed steps 5→4, reuse 0%→67%, cost -7.8% on gpt-4o-mini.

**Explicitly not yet proven, documented rather than hidden**: the
higher-stakes claim that a company of agents gets more reliable *because*
it has memory — tested at adequate sample size across three company setups
and none cleared 100% reliability; the Support HQ 3/3-then-9/10 result is
called out directly as a small-sample pass a larger sample overturned.
LongMemEval has a runner but no scored result yet.

**Deliberately off by default, still experimental**: severity tiering,
ActionMemory (shadow-only, benefit over a frozen control still unproven),
convention mining (merged 2026-07-24, the newest thing in the repo). The
hard `AGENT_MEMORY`-as-required-output-contract idea was tried and reverted
in v0.19.4.

**Acknowledged open security gaps**: MCP tool descriptions flow into the
prompt unescaped; MCP server mode has no auth/bound; tool subprocesses
inherit the full environment including API keys; MCP stdio has no read
timeout; `grep_dir` has no sandbox jail.

**Acknowledged open test-coverage gaps**: no wheel-packaging guard for
builtin tools after a real `pip install`; no e2e test for planner
item-budget division; no test for `max_tokens` truncation with partial
`tool_use` args.

---

*Files worth opening next, most central first: `src/fabri/core/agent.py`,
`src/fabri/runtime.py`, `src/fabri/company.py`,
`src/fabri/orchestrator/retrieval.py`, `src/fabri/orchestrator/pipeline.py`,
`src/fabri/pricing.py`, `src/fabri/tools/registry.py`,
`src/fabri/service/http_server.py`, `src/fabri/events.py`.*
