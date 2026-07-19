# fabri

[![PyPI](https://img.shields.io/pypi/v/fabri.svg)](https://pypi.org/project/fabri/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](https://github.com/Rushour0/fabri/blob/main/LICENSE)
[![Python](https://img.shields.io/pypi/pyversions/fabri.svg)](https://pypi.org/project/fabri/)

**The self-improving agent engine you build products on.** Most agent
frameworks ship prompts that never change. fabri's agents mine their own run
traces into memory, so they stop repeating their mistakes — measurably fewer
steps and lower cost on tasks they can learn from
([benchmarks](https://github.com/Rushour0/fabri/blob/main/BENCHMARKS.md)).

fabri is split into two layers: an **engine** (a frugal agent loop, per-role
LLMs, polyglot tools, and a memory loop that grows the prompt from the agent's
own traces) and a **builder** that turns intent into a running agent fast.
Read [docs/vision.md](https://github.com/Rushour0/fabri/blob/main/docs/vision.md) for the full why.

fabri is **open source** under the [Apache License,
2.0](https://github.com/Rushour0/fabri/blob/main/LICENSE). Install it
from PyPI, build agents with it, embed it in a hosted product, fork it —
no revenue threshold, no commercial license required. The core (agent
loop, memory pipeline, orchestration, config surface) is open to
contribution too — see
[CONTRIBUTING.md](https://github.com/Rushour0/fabri/blob/main/CONTRIBUTING.md)
for how core changes get reviewed.

## Philosophy

An agent's prompt should not be written by hand and frozen. It should
grow from what the agent actually does.

```
                ┌──────────────────────────┐
                │      task arrives        │
                └────────────┬─────────────┘
                             │
                             ▼
        ┌───────────────────────────────────────────┐
        │  retrieve relevant guidelines from memory │
        │  (top-k by similarity, plus tool-tagged   │
        │   hits guaranteed when a tool is named)   │
        └────────────────────┬──────────────────────┘
                             │ injected into system prompt
                             ▼
                    ┌────────────────┐
                    │  agent loop    │ ── tool calls ──▶ subprocess tools
                    │  (ReAct)       │ ◀── results ────
                    └────────┬───────┘
                             │ JSONL trace
                             ▼
        ┌───────────────────────────────────────────┐
        │  analyze trace: compress each failure     │
        │  into a short, generalized guideline      │
        └────────────────────┬──────────────────────┘
                             │
                             ▼
        ┌───────────────────────────────────────────┐
        │  dedup vs existing tactical guidelines    │
        │  → near-duplicate? bump recurrence count  │
        │  → recurred across N sessions? promote    │
        │    from tactical to strategic             │
        └────────────────────┬──────────────────────┘
                             │
                             ▼
                  back into the memory store,
                  retrievable on the next task
```

A failure in session N becomes retrievable context in session N+1,
without anyone editing the prompt by hand. That loop — trace → analyze
→ compress → dedup → promote → retrieve — is the heart of the **engine**.
On top of it, a **builder** layer (ideator, tool-writer, prompt-kit, skills,
service) scaffolds a new product onto the engine so building one is faster, not
slower — see [docs/vision.md](https://github.com/Rushour0/fabri/blob/main/docs/vision.md) and Track B in
[docs/ROADMAP.md](https://github.com/Rushour0/fabri/blob/main/docs/ROADMAP.md).

Two operating principles fall out of that:

- **Context over prompt.** Keep retrieved context compact and
  just-in-time. Each tool gets one clear job. Tool results enter the
  context in a compact TOON encoding, not raw JSON.
- **Polyglot tools behind a uniform contract.** A tool is a JSON
  manifest next to an executable in any language. Stdin gets JSON args,
  stdout returns JSON, the runner normalizes errors. Agents can be
  composed as tools of other agents through the same contract.

## Frugality by default

A token spent is a token billed. The base system prompt steers every run
toward fewer, better-aimed actions, and the defaults make the cheap path
the default path:

- **Be sure before you call.** The agent states what it expects a call to
  return before making it; if it can already act, it acts instead of
  probing. One decisive call beats many exploratory ones — every
  round-trip re-sends the whole context. (TALE, arXiv:2412.18547.)
- **Single-threaded by default; delegate as the exception.** `spawn_subagent`
  re-runs the entire loop, so it's reserved for subtasks that are
  independent, parallelizable, *and* large enough to overflow the parent's
  context — never sequential steps, never "because the tool exists." A
  multi-agent run costs ~15× a single agent; coordination is a top failure
  source. (Anthropic, *Building a multi-agent research system*; Cognition,
  *Don't Build Multi-Agents*.)
- **Code as action.** When a job needs several operations, the agent does
  them in one `python_exec` script (or one `batch` call) that branches over
  the results, instead of narrating each step as its own tool call. (CodeAct,
  arXiv:2402.01030: −30% steps; smolagents: −28% tokens.)
- **Surgical edits, windowed reads, prompt caching, TOON results.** Prefer
  `edit_file` over whole-file rewrites; read only the slice you need; the
  static system+tools prefix is cached; tool results enter context in
  compact TOON.

Every run emits a `usage` trace event carrying token totals **and**
`cost_usd` (priced per model), plus `subagent_cost_usd` and `total_cost_usd`
— the end-to-end cost of the run and its whole sub-agent subtree — so a host
service can track COGS without parsing logs. See `fabri.pricing`.

## Install

```bash
pip install fabri                       # lean base install; agencies run without memory learning
pip install "fabri[self-improving]"     # ONE command: memory + hybrid retrieval so agencies learn
                                        # from their own runs and avoid repeat mistakes — local sqlite-vec store, no docker
export ANTHROPIC_API_KEY=...
```

Or, to skip docker entirely (in-process sqlite-vec memory backend):

```bash
pip install 'fabri[sqlite]'
export ANTHROPIC_API_KEY=...
fabri --config configs/example.yaml run "your task"   # or `fabri init` to scaffold
```

See [`configs/`](configs/) for the canonical example and benchmark configs,
and [`BENCHMARKS.md`](BENCHMARKS.md) for the methodology + how to reproduce
published numbers.

**Default provider is Google Gemini** (lowest cost + generous free tier): a
fresh `fabri run` needs only `GEMINI_API_KEY`. All provider SDKs ship by
default — no extra install — so switching is a one-line config change:

- **Gemini** (default): `llm.provider: gemini`, export `GEMINI_API_KEY`
- **Anthropic / Claude**: `llm.provider: anthropic`, export `ANTHROPIC_API_KEY`
- **OpenAI**: `llm.provider: openai`, export `OPENAI_API_KEY`
- **OpenRouter**: `llm.provider: openrouter`, export `OPENROUTER_API_KEY`
- **AWS Bedrock**: `llm.provider: bedrock`, set `llm.aws_region` (or
  `AWS_REGION`). Credentials come from the standard AWS chain — env keys, a
  shared profile, an IAM role, or a Bedrock API key (`AWS_BEARER_TOKEN_BEDROCK`)
  — so there's no `api_key_env`. One Bedrock backend serves every
  Converse-capable model: Claude (`us.anthropic.claude-…`), OpenAI `gpt-oss`
  (`openai.gpt-oss-…`), Llama, Mistral, etc. (Gemini is not on Bedrock — use
  the `gemini` provider for that.)

Provider can be set per-role too (e.g. a Gemini Pro orchestrator with a
Flash-Lite narrator), so a run can mix vendors. Note that switching the parent
`llm.provider` repoints every model-only role (one that sets just a model id)
onto the new provider — give such roles their own `provider`, or set them to
`null`, when the model id isn't valid on the new provider.

Embeddings run locally via `sentence-transformers/all-MiniLM-L6-v2` —
no embedding API calls.

## Quickstart

Scaffold a multi-agent **agency**, run it on a task, and watch it learn — no docker:

```bash
pip install "fabri[self-improving]"        # memory loop on; local sqlite-vec, no docker
export OPENAI_API_KEY=...                   # the bug-crew template ships OpenAI configs

fabri new agency demo                       # scaffolds a bug-triage crew: triager → fixer → tester
fabri --config demo/agent.openai.yaml run "triage and fix the failing test in workspace/"

# or watch it live in the browser:
fabri serve --config demo/agent.openai.yaml
fabri studio
```

Each run mines guidelines from its own trace, so the crew avoids its own repeat
mistakes on the next run. Prefer a single agent? `fabri init demo` scaffolds one
(an `agent.yaml`, an example tool under `tools/agent_tools/`, and a
`docker-compose.yml`) — you edit those, not the library.

## Commands

```bash
fabri run "some task description"
fabri --config agent.yaml run "..."        # config-driven agent
fabri --verbose run "..."                  # DEBUG logging to console
fabri inspect-memory "a query"             # test retrieval
fabri ingest-traces <session-id>           # re-mine a past trace
fabri ingest app.log --adapter regex       # mine an EXTERNAL log (the Improver)
fabri ingest --list-adapters               # available log adapters
```

Each `run` returns an outcome: `success`, `success_with_recovery`
(finished but a tool call failed along the way), or `incomplete` (hit
the step limit).

Every run writes two records keyed by `session_id`:

- `.fabri/traces/<session_id>.jsonl` — machine-readable trace used by
  the memory pipeline.
- `.fabri/logs/<session_id>.log` — always DEBUG-level, with LLM call
  latency/token usage, tool dispatch latency, and every dedup /
  promotion decision.

Both land under `.fabri/` in the directory you run from (override with
`$FABRI_HOME`). Add `.fabri/` to your project's `.gitignore`.

> **Getting the most out of memory:** see
> [docs/using-fabri-well.md](https://github.com/Rushour0/fabri/blob/main/docs/using-fabri-well.md)
> for the cross-run learning loop, backend choice, reading the `retrieval`
> trace, and memory hygiene. For how fabri's memory compares to other
> agent-memory projects (Hermes Agent, OpenClaw) and where it's headed, see
> [docs/design/external-memory-patterns.md](https://github.com/Rushour0/fabri/blob/main/docs/design/external-memory-patterns.md).

## Configuring an agent

Every field has a default, so you only override what you need:

```yaml
agent:
  name: my-agent
  max_steps: 10                  # loop budget; raise for multi-tool tasks
  output_format: json            # what the model is asked to emit (decompose):
                                 # json (reliable) or toon (fewer output tokens)
  response_schema: null          # O1: a JSON Schema for typed output. When set,
                                 # the final answer is validated against it and
                                 # the validated value is returned as
                                 # `structured_output`; a mismatch re-prompts the
                                 # model (response_retries), then error_strategy
                                 # (strict | warn | fallback) resolves it.

llm:
  provider: gemini               # gemini / anthropic / openai / openrouter / bedrock
  model: gemini-2.5-pro
  max_tokens: 1024
  api_key_env: GEMINI_API_KEY
  # AWS Bedrock only (provider: bedrock): no api_key_env — creds come from the
  # AWS chain; just set the region. See configs/bedrock.yaml for a full example.
  # aws_region: us-east-1

tools:
  manifest_dir:                  # one path or a list, merged into one registry
    - builtin                    # bundled tools (read_file/write_file/...)
    - tools/agent_tools          # your project's own tools, relative to cwd
  enabled: [read_file, write_file]   # null = every discovered tool
  sandbox_root: project          # read_file/write_file refuse paths outside
  result_format: toon            # how tool results enter the model's context:
                                 # toon (fewer input tokens) or json
  decompose:
    enabled: false               # turn on for research-shaped tasks
    max_subquestions: 5
  mcp_servers:                   # optional: pull tools from MCP servers
    - name: fs                   # stdio transport
      command: ["npx", "@modelcontextprotocol/server-filesystem", "/srv/data"]
    - name: web                  # http transport
      url: "https://mcp.example.com/jsonrpc"
      headers: {Authorization: "Bearer ..."}
  # Remote tools are wrapped as `mcp_<server>_<remote_tool>`. A server
  # that fails to start is logged and skipped, not fatal.

memory:
  collection: my_fabri           # separate Qdrant collection per agent
  qdrant_url: http://localhost:6333
  top_k: 5
  similarity_threshold: 0.85     # dedup threshold for guideline merging
  promotion_threshold_sessions: 3
  guideline_max_tokens: 30
```

Paths in `manifest_dir` and `sandbox_root` resolve relative to **the
directory you run the command from**, not the config file's location —
run from your project root. `builtin` resolves to the framework's
bundled tools wherever the package is installed.

## Writing a tool

A tool is a JSON manifest next to an executable in any language. The
manifest is auto-discovered by globbing `*.json` in each `manifest_dir`.

```json
{
  "name": "hello",
  "description": "One sentence the LLM uses to decide when to call this.",
  "command": ["python3", "hello.py"],
  "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}},
  "output_schema": {"type": "object"},
  "timeout_s": 10
}
```

The executable reads one JSON object from stdin, prints one JSON object
to stdout, and uses its exit code to signal success/failure:

```python
import json, sys
args = json.loads(sys.stdin.read())
print(json.dumps({"greeting": f"hello, {args['name']}"}))
# exit 0 -> ok=true,  wrapped as {"ok": true,  "result": ...}
# exit != 0 -> ok=false, wrapped as {"ok": false, "error": ..., "result": ...}
```

The runner normalizes timeouts, nonzero exits, and malformed-JSON
output into the same `{ok, error?, result?, stderr?}` shape — your
script never needs to worry about how the agent loop reports failure.

**Sandboxing.** `read_file` / `write_file` resolve every path against
`$FABRI_SANDBOX_ROOT` (set from `tools.sandbox_root`) and reject
anything that escapes it. If you write your own file-touching tool,
follow the same pattern.

## Orchestration

fabri runs **one agent loop at a time** by default. There is no global
planner, no message bus, and no coordinator process. The model in the
loop *is* the orchestrator: at every step it sees the retrieved
guidelines, the running tool-result tape, and decides the next call.

### The loop

```
        retrieve top-k guidelines for the task ──► system prompt
                                                       │
        ┌──────────────────────────────────────────────┘
        ▼
   ┌─────────────┐   tool_use   ┌──────────────┐
   │ LLM step    │ ───────────► │ tool runner  │ stdin JSON ─► subprocess
   │ (ReAct)     │ ◄─────────── │              │ ◄── stdout JSON
   └──────┬──────┘  tool_result └──────────────┘
          │ writes JSONL trace event per step
          ▼
     stop on `final` / max_steps / hard error
                                                       │
        ┌──────────────────────────────────────────────┘
        ▼
   analyze trace → compress failures → dedup → promote → memory store
```

The two cost-shaped knobs are the **system prefix** (cached;
guidelines + tool defs) and the **rolling tape** of tool results
(re-sent every step). Tool results enter the tape in TOON, not raw
JSON. Every step emits a `usage` event with token totals and
`cost_usd` so a host service can attribute COGS without parsing logs.

### Picking a composition primitive

Three ways to do more work per LLM round-trip. **Try them in this
order** — the cheap path is the default path.

| Primitive       | Use when                                          | Cost     |
|-----------------|---------------------------------------------------|----------|
| `batch`         | N known calls, no branching between them          | 1×       |
| `python_exec`   | N calls with branching, loops, or aggregation     | 1×       |
| `spawn_subagent`| Independent subtask that would overflow context   | ~15×     |

Rule of thumb: if `batch` or `python_exec` can do it, do not reach for
`spawn_subagent`.

### Agents as tools

A `tools.agents` entry in `agent.yaml` exposes another agent as a tool
of this one. Each sub-agent is just another tool call in the parent's
normal loop. A sub-agent entry may carry `model` / `max_tokens`
overrides, so a parent on Sonnet can call a Haiku classifier without
duplicating the full config:

```yaml
tools:
  agents:
    - name: classify
      description: Classify a snippet into one of N labels.
      config: tools/agent_tools/classifier.yaml
      model: claude-haiku-4-5
      max_tokens: 256
```

A child invoked this way inherits the parent's memory collection by
default, so guidelines learned by either side accumulate in the same
store. Use `memory_collection_suffix` on the call (or a separate
`memory.collection` in the child config) when you want isolation —
e.g. a generic `classify` child shared by many parents.

### Parallel fan-out

`spawn_subagent` calls that share a `parallel_group` tag are
dispatched concurrently by the parent loop, instead of sequentially.
The parent step that decides to fan out emits one tool_use block per
child; each `parallel_group` event in the trace marks the wall-clock
boundary. Cost is additive across children (token usage rolls up
through `usage.total_cost_usd`), but wall-clock collapses to the
slowest child.

The cheap path is always: do it inline. Reach for `parallel_group`
when (a) each child has its own large context to chew through, and
(b) the children genuinely don't need each other's outputs.

## Multi-agent examples

> **Runnable versions of these shapes live in
> [`examples/`](https://github.com/Rushour0/fabri/tree/main/examples)** —
> custom tools (`01`), parallel sub-agent fan-out (`02`), a draft→verify
> pipeline (`03`), and sandboxing (`04`), each annotated with the optimization
> methodology it demonstrates
> ([`docs/optimization-methodologies.md`](https://github.com/Rushour0/fabri/blob/main/docs/optimization-methodologies.md)).
> The YAML in this section is illustrative; the `examples/` folders run as-is.

Three shapes that actually pay for the ~15× sub-agent overhead.
Anything outside these is almost always cheaper inline.

| Shape                | Use when                                              | Example below |
|----------------------|-------------------------------------------------------|---------------|
| Fan-out + synthesize | One question splits into N independent legs          | §1            |
| Specialist-as-tool   | Some calls deserve a cheaper model or tighter prompt | §2            |
| Generator + verifier | One context can't hold both roles cleanly            | §3            |

### 1. Fan-out research, single synthesizer

A planner agent decomposes a question, fans out one
researcher-per-subquestion in parallel, then synthesizes. Each
researcher burns its own context window on raw pages; the planner's
context only ever sees their short summaries.

```yaml
# planner.yaml
agent: { name: planner, max_steps: 12 }
llm:   { provider: anthropic, model: claude-sonnet-4-6 }
tools:
  manifest_dir: [builtin, tools/agent_tools]
  enabled: [spawn_subagent, write_file]
  agents:
    - name: research_one
      description: Answer ONE focused subquestion using the web. Returns ≤200 words + citations.
      config: tools/agent_tools/researcher.yaml
      model: claude-haiku-4-5   # cheap per-leg; planner stays on Sonnet
```

Planner's natural action becomes:

```
spawn_subagent(name=research_one, task="...subq A...", parallel_group="fanout-1")
spawn_subagent(name=research_one, task="...subq B...", parallel_group="fanout-1")
spawn_subagent(name=research_one, task="...subq C...", parallel_group="fanout-1")
→ synthesize from the three short returns
```

Wall-clock = slowest leg. Cost = sum of legs + planner. Crucially, the
planner never loads raw pages into *its* context.

### 2. Specialist behind a uniform tool contract

A parent agent on Sonnet, two specialists exposed as tools:

```yaml
# parent.yaml
tools:
  agents:
    - name: classify_intent     # tiny Haiku classifier, 256-token cap
      description: Classify a user message into {bug, feature, billing, other}.
      config: tools/agent_tools/classifier.yaml
      model: claude-haiku-4-5
      max_tokens: 256
    - name: sql_writer          # SQL-only Sonnet, schema-aware
      description: Turn a natural-language metric request into one SELECT.
      config: tools/agent_tools/sqlwriter.yaml
```

The parent learns *when* to call each child through normal memory
guidelines ("for billing-shaped questions, call `classify_intent`
first"). Each child has its own tight prompt and its own memory
collection, so its guidelines don't pollute the parent's retrieval.

### 3. Pipeline with a verifier

Generator → verifier, two agents wired by the parent. Use when a
single agent confuses itself by holding both roles in one context:

```yaml
tools:
  agents:
    - name: draft
      description: Produce a candidate answer. May be wrong.
      config: tools/agent_tools/drafter.yaml
    - name: verify
      description: Check a candidate answer against the source. Returns {ok, reasons[]}.
      config: tools/agent_tools/verifier.yaml
```

Parent loop: `draft` → `verify` → if not ok, `draft` again with the
verifier's reasons appended. The verifier never sees the drafter's
chain-of-thought, only its output — which is the point.

## Designing tools for low token cost

Tools shape the bill more than prompts do. The system prefix is
cached; tool *results* are not, and they ride in the context every
step. The short version, as a table:

| Rule                       | What it means                                                            |
|----------------------------|--------------------------------------------------------------------------|
| One job, one tool          | A `mode` enum is two tools badly fused. Split them.                      |
| Description is the contract| Manifest `description` is the only thing the LLM reads. 1–3 sentences.   |
| Return the minimum         | Slice, truncate, summarize. Every byte rides every subsequent step.      |
| TOON-friendly shapes       | Flat arrays of records with consistent keys encode much smaller.         |
| Compose at the tool layer  | If 3 calls always happen together, ship 1 tool.                          |
| Idempotent or explicit     | Side-effecting tools must be safe to retry, or fail loudly.              |
| Paths, not payloads        | Write big results to `.fabri/scratch/<id>.json`, return `{path, size}`.  |
| Code-as-action for loops   | No `for_each` tool. `python_exec` covers it in one LLM step.             |
| Cap your manifests         | Keep `tools.enabled` tight — every entry sits in the cached prefix.      |

Smell test: if "what would the agent do with the result of this
tool?" has a one-sentence answer, the tool is probably shaped right.
If the answer is "it depends what mode you called it in", split it.

## The Improver — learn from logs you already have

The memory loop doesn't only learn from fabri's own runs. Point it at any log —
app logs, CI output, OTel/OpenAI traces — and the failures and successes in it
mine into the *same* memory the agent retrieves from. Feeding logs is
plug-and-play, and the default path is deterministic and **$0** (no LLM call);
add `synthesize=True` for LLM-compressed guidelines.

```python
import fabri

# Deterministic ($0): mine an external log straight into the agent's memory.
summary = fabri.readlogs("prod/app.jsonl", adapter="jsonl")
print(summary.sessions, summary.by_kind, f"${summary.llm_cost_usd:.4f}")

# The knowledge is now retrievable — the next run starts ahead of where the
# logs left off. Omit store/config and it resolves them from agent.yaml, so the
# guideline lands in the collection the agent already reads.
```

Teach fabri a new log format three ways — pick per format:

```python
# 1) a native Python adapter (decorator)
from fabri.ingest import Session, tool_event, start_event, final_event

@fabri.adapter("mytool")
def parse(source, options):
    for trace_id, records in source.group_by("trace_id"):
        events = [start_event(records[0]["prompt"])]
        for r in records:
            events.append(tool_event(r["cmd"], ok=r["exit"] == 0, error=r.get("stderr")))
        events.append(final_event("success" if records[-1]["exit"] == 0 else "failed"))
        yield Session(f"mytool-{trace_id}", events)
```

```yaml
# 2) a declarative field-map in agent.yaml — zero code
ingest:
  adapters:
    - name: prodlogs
      kind: configmap
      mapping: {session_key: trace_id, task_field: prompt,
                tool_field: tool.name, ok_field: tool.ok, error_field: tool.error}
```

```
# 3) a polyglot executable (any language) via the tool contract, shippable as a
#    skill — see the bundled `syslog-adapter` skill.
fabri skills install syslog-adapter
fabri ingest app.log --adapter syslog        # $0; --synthesize for LLM guidelines
tail -f app.log | fabri ingest - --adapter syslog   # or stream it live
```

Built-in adapters: `jsonl` (native), `regex` (plaintext), `otel`/`openai`
(structured traces), and `auto` to sniff. Third-party adapters install as pip
packages via the `fabri.adapters` entry-point group. `fabri ingest
--list-adapters` shows what's available.

## Skills — scaffold an agency from a bounded deliverable

> The links and marketplace commands below resolve once this is merged to
> `main`; on a branch or unmerged fork, use the local install/validate
> commands in
> [`skills/agency-builder/README.md`](skills/agency-builder/README.md) instead.

[`skills/agency-builder`](skills/agency-builder)
turns one bounded deliverable into a small, reviewable fabri agency — an
orchestrator plus a fixed set of specialists, each an ordinary agents-as-tools
config, with a deterministic verifier standing in for "trust me, it worked."
See [`docs/agency-kernel.md`](docs/agency-kernel.md)
for what stays fixed versus what varies per agency, and
[`examples/agencies/changelog-release-notes`](examples/agencies/changelog-release-notes)
for a worked, runnable example.

Install for Claude Code:

```text
/plugin marketplace add Rushour0/fabri
/plugin install agency-builder@fabri-skills
```

Install for Codex CLI:

```bash
codex plugin marketplace add Rushour0/fabri
codex plugin add agency-builder@fabri-skills
```

The skill asks for the target persona, the one deliverable, specialist roles,
proof-bar metric, and approval gate before it builds anything — a bare prompt
like "build an AI agency for X" gets you a clarifying question back, not an
immediate scaffold. See
[`skills/agency-builder/README.md`](skills/agency-builder/README.md)
for local (pre-publish) install/validate commands and common usage cases.

## Using it as a library

Everything the CLI does is composition over the public API:

```python
from fabri import (
    run_agent, QdrantMemoryStore, build_llm, build_tool_defs, build_tools,
)
from fabri.config import load_config

config = load_config("agent.yaml")
store = QdrantMemoryStore(
    url=config["memory"]["qdrant_url"],
    collection=config["memory"]["collection"],
)
tools = build_tools(config["tools"])
llm = build_llm(config, build_tool_defs(tools, config["tools"]["decompose"]))

result = run_agent(
    "do the task", llm, tools, store, max_steps=config["agent"]["max_steps"],
)
```

To skip Qdrant entirely, swap the store for the in-process sqlite-vec
backend — same interface, no other code changes:

```python
from fabri import SqliteMemoryStore

store = SqliteMemoryStore(
    path=".fabri/memory.db",
    collection=config["memory"]["collection"],
)
```

## License

[Apache License, 2.0](https://github.com/Rushour0/fabri/blob/main/LICENSE)
© Rushikesh Patade. Free for any use, including commercial and
hosted/embedded redistribution. Contributions welcome — see
[CONTRIBUTING.md](https://github.com/Rushour0/fabri/blob/main/CONTRIBUTING.md).
