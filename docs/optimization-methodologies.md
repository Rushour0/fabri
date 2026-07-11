# Optimization methodologies — what fabri borrows from other agent-memory systems

This doc distills the *transferable* optimization ideas from Hermes Agent and
OpenClaw (surveyed in `docs/design/external-memory-patterns.md`) and maps each
one to a real fabri mechanism and a runnable example under `examples/`. Every
fabri claim here is anchored to code; the external attributions are "per their
docs" (see the survey for sourcing caveats).

The unifying theme: **an agent's cost and quality are dominated by what sits in
its context on every step.** Each methodology below is a lever on that.

| Methodology (external origin) | fabri mechanism | Example |
|---|---|---|
| Return-the-minimum / delta compression (RetainDB, ByteRover) | Low-token tool design; `result_format: toon`; `guideline_max_tokens` | `examples/01-custom-tool` |
| Tiered / lazy context loading (OpenViking abstract→overview→full) | `read_file` `outline_only`→window→full; `tools.retrieval` narrowing the tool list | `examples/01-custom-tool` |
| Delegation isolation / per-profile scoping (Hermes profiles, non-blocking prefetch) | `spawn_subagent` fresh context/trace/namespace; `parallel_group` fan-out; per-child budgets | `examples/02-parallel-fanout` |
| Adversarial verify / staged promotion gate (OpenClaw human-review, extract-then-verify) | `tools.agents[]` draft→verify loop; memory promotion after N sessions | `examples/03-pipeline-verifier` |
| Bounded, disposable execution contexts | `Sandbox` ABC (`LocalSandbox`/`DockerSandbox`); per-tool timeout + output cap | `examples/04-docker-sandbox` |
| Context fencing (Supermemory) / context budget (Honcho) | `GUIDELINE_FENCE` around injected guidelines; `top_k`; stdout caps | (cross-cutting) |
| Swappable backend behind an interface (both; Hermes' 8 providers) | `Sandbox` ABC; pluggable memory backends; MCP/agent/ingest seams; proposed `MemoryStore` Protocol | `examples/04-docker-sandbox` |

## 1. Return the minimum (the highest-leverage lever)

A tool result is not cached and rides in context on **every** subsequent step, so
a fat result taxes the whole rest of the run. Hermes' RetainDB (delta
compression) and ByteRover (pre-compression before context limits) both attack
this at the memory layer; fabri attacks it at the **tool** layer, where the bytes
actually enter.

- Return a slice, not the corpus (`examples/01-custom-tool` returns top-N words).
- `result_format: toon` encodes flat uniform records far smaller than JSON.
- "Paths, not payloads": write big results to `.fabri/scratch/<id>.json`, return
  `{path, size}`.
- Guidelines are hard-capped (`memory/compress.py` `DEFAULT_MAX_TOKENS = 30`).

See README §"Designing tools for low token cost" for the full rule set.

## 2. Tiered / lazy loading

OpenViking loads memory in tiers (abstract ~100 tokens → overview ~2k → full).
fabri applies the same "spend context lazily" idea to files and tools:

- `read_file` supports `outline_only=true` (structure only) and
  `line_start`/`line_end` windows — locate the right slice in one cheap call
  before pulling the full text (`src/fabri/tools/examples/read_file.py`).
- **Tool retrieval** (`tools.retrieval`, off by default) embeds the task and each
  tool's description and injects only the top-K relevant tools plus a guaranteed
  `always_include` set — so a large tool catalog doesn't sit in the cached prefix
  every step (`orchestrator/retrieval.py::retrieve_tools`).

## 3. Delegation isolation

Hermes scopes each provider per profile and prefetches non-blockingly so a turn
isn't polluted by unrelated state. fabri's equivalent is the sub-agent: each
`spawn_subagent` (or `tools.agents[]`) call runs a fresh agent loop in its own
subprocess with its **own `session_id`, trace, and memory namespace**, returning
only a short `final_text` to the parent. The orchestrator's context never sees
the child's intermediate tool spam — that's the optimization.

Fan-out adds parallelism: calls sharing a `parallel_group` dispatch through a
thread pool capped by `tools.max_parallel_spawns`, so wall-clock is the slowest
child, not the sum. Per-child budgets (`agent.subagent.max_steps`/`max_cost_usd`)
and a pre-spawn budget tripwire keep a wide fan-out from multiplying cost.
`examples/02-parallel-fanout` is the worked case.

## 4. Adversarial verification

Both projects gate a commit on a second, skeptical pass — OpenClaw's human-review
promotion ("Grounded Backfill"), the general extract-then-verify loop. fabri
expresses this as a `tools.agents[]` pipeline: a cheap drafter proposes, a
separate verifier with no stake returns `{ok, reasons[]}`, and the orchestrator
revises until `ok`. Two cheap single-purpose models beat one expensive
self-checking model. `examples/03-pipeline-verifier`. (The same instinct powers
memory promotion: a tactical guideline only becomes `strategic` after it recurs
across ≥3 sessions.)

## 5. Bounded, disposable execution contexts

Isolation is an optimization, not just a safety feature: a capped, throwaway
context means untrusted tool output can't escalate and a runaway can't consume
the host. fabri layers this: `LocalSandbox` (path jail + per-call timeout +
process-group kill + 1 MiB output cap) by default, `DockerSandbox` (cap-drop,
no-new-privileges, pids-limit; opt-in mem/network caps) when wired.
`examples/04-docker-sandbox`.

## 6. Swappable backends behind an interface

The single most repeated pattern across both projects (Hermes' 8 memory
providers, OpenClaw's pluggable stores) is that the backend is an **interface**,
not a hardcode. fabri already does this for the sandbox (`Sandbox` ABC) and
memory (Qdrant/sqlite), exposes MCP servers, agents-as-tools, and ingest adapters
as seams, and the survey recommends formalizing the memory surface into a
`MemoryStore` Protocol (R1 in `docs/design/external-memory-patterns.md`).

## See also

- `docs/design/external-memory-patterns.md` — the full Hermes/OpenClaw survey and
  the four adoption recommendations.
- `docs/using-fabri-well.md` — the operational loop that makes memory compound.
- `examples/README.md` — the runnable examples index.
