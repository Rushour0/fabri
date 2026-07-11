# fabri examples

Runnable, copy-and-adapt examples for the things people ask "how do I do X"
about: writing tools, spawning sub-agents, composing multi-agent pipelines, and
sandboxing. Each folder is self-contained with its own README and runs from the
**repo root**.

Every example is annotated with the **optimization methodology** it demonstrates
— the transferable ideas from other agent-memory systems (Hermes Agent,
OpenClaw), mapped to real fabri mechanisms in
[`docs/optimization-methodologies.md`](../docs/optimization-methodologies.md).

| # | Example | Teaches | Optimization methodology |
|---|---|---|---|
| 01 | [`01-custom-tool`](01-custom-tool/) | Write a tool (manifest + executable), wire it beside the builtins, validate/test it | Return-the-minimum + compact encoding |
| 02 | [`02-parallel-fanout`](02-parallel-fanout/) | Spawn independent sub-agents at runtime and run them in parallel with per-child budgets | Delegation isolation + parallel dispatch |
| 03 | [`03-pipeline-verifier`](03-pipeline-verifier/) | Compose fixed specialist agents (`tools.agents[]`) into a draft→verify loop | Adversarial verification |
| 04 | [`04-docker-sandbox`](04-docker-sandbox/) | fabri's layered isolation, from path-jail to a locked-down container | Bounded, disposable execution contexts |

## Prerequisites

```bash
pip install 'fabri[sqlite]'      # sqlite backend = no docker/qdrant needed
export GEMINI_API_KEY=...         # or set llm.provider in each config to a provider you have
```

All configs use the **sqlite** memory backend and the **gemini** provider by
default so a fresh checkout runs with one API key and no services. Switch
`llm.provider` (+ `api_key_env`) in any config to `anthropic` / `openai` /
`openrouter` — all SDKs ship with fabri.

## Two ways to compose agents (examples 02 vs 03)

- **Dynamic** (`spawn_subagent`, example 02): the parent picks children at
  runtime via `config_path`. One generic worker, invoked N times. Use when the
  split depends on the input.
- **Static** (`tools.agents[]`, example 03): specialist roles declared once and
  exposed as named tools. Use when the pipeline is known up front.

They mix freely in one agent.

## The cost ladder (when to reach for what)

A `spawn_subagent` call re-runs the whole agent loop (~15× a plain tool call).
Before fanning out, prefer the cheap primitives:

| Primitive | Use when | Rel. cost |
|---|---|---|
| `batch` | N known tool calls, no branching | 1× |
| `python_exec` | N calls with loops/branching/aggregation | 1× |
| `spawn_subagent` / `tools.agents[]` | genuinely independent subtask too large for one context | ~15× |

## Related docs

- [`docs/using-fabri-well.md`](../docs/using-fabri-well.md) — the cross-run learning loop and memory hygiene.
- [`docs/optimization-methodologies.md`](../docs/optimization-methodologies.md) — the methodology behind each example.
- [`docs/retrieval-tuning.md`](../docs/retrieval-tuning.md) — memory retrieval knobs.
- Root [`README.md`](../README.md) — philosophy, install, full command reference.
