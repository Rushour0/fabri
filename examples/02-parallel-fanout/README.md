# 02 · Parallel sub-agent fan-out

**What you'll learn:** how one agent spawns many independent sub-agents at
runtime and runs them in parallel, with per-child budgets and a concurrency cap.

**Optimization methodology demonstrated:** *delegation isolation* — the fabri
analog of Hermes Agent's per-profile scoping and non-blocking prefetch (see
`docs/optimization-methodologies.md`). Each sub-agent gets its **own context,
own trace, and own memory namespace**; the parent only ever sees each worker's
short final answer, never its intermediate tool output. That's what keeps a
big multi-part task from blowing up the orchestrator's context window.

## Two ways to spawn — this example uses the dynamic one

| Mechanism | Where the child is chosen | Use when |
|---|---|---|
| **`spawn_subagent`** (dynamic, this example) | at runtime, per call via `config_path` | the split isn't known until the agent sees the task |
| `tools.agents[]` (static, see `../03-pipeline-verifier/`) | at config-load time, by tool name | you know the specialist roles up front |

## The files

```
02-parallel-fanout/
├── planner.yaml    # parent: splits the task, spawns one worker per subtask
└── worker.yaml     # leaf: read-only exploration, no further spawning
```

## Run it

```bash
pip install 'fabri[sqlite]'
export GEMINI_API_KEY=...
fabri --config examples/02-parallel-fanout/planner.yaml run \
  "Summarize what each of these does, one worker per item, in parallel: \
   src/fabri/memory, src/fabri/orchestrator, src/fabri/tools. \
   Then combine the summaries into a short overview."
```

## How the parallelism actually works

The planner emits several `spawn_subagent` calls **in one turn**, each sharing
a `parallel_group`:

```text
spawn_subagent(config_path="examples/02-parallel-fanout/worker.yaml",
               task="Summarize src/fabri/memory", parallel_group="fanout-1")
spawn_subagent(config_path="examples/02-parallel-fanout/worker.yaml",
               task="Summarize src/fabri/orchestrator", parallel_group="fanout-1")
spawn_subagent(config_path="examples/02-parallel-fanout/worker.yaml",
               task="Summarize src/fabri/tools", parallel_group="fanout-1")
```

fabri groups calls that share a `parallel_group` and dispatches them through a
thread pool sized `min(len(group), max_parallel_spawns)` — so the three run
concurrently, and wall-clock is the slowest single worker, not their sum. Each
call returns `{final_text, outcome, session_id, trace_path, usage}`; the parent
reads the `final_text`s back and writes the combined overview.

> **Note:** the field is `config_path`, not `name`. (The `name=...` form you may
> see elsewhere is the *static* `tools.agents[]` mechanism — example 03 — where
> the agent is invoked by its tool name instead of a config path.)

## Cost & safety knobs (all real, all in `planner.yaml`)

- **`agent.subagent.max_steps` / `max_cost_usd`** — each child runs on its own
  budget so a wide fan-out can't silently multiply spend. Omit to inherit the
  parent's budget.
- **`tools.max_parallel_spawns`** (default 4; set to 3 here) — caps peak
  concurrent subprocesses so a wide wave can't OOM the host.
- **Budget tripwire** — fabri re-checks the run's budget immediately *before*
  each spawn dispatch and refuses to launch more children once breached, even
  mid-wave.
- **Recursion cap** — a leaf that tried to spawn again is bounded by
  `FABRI_SUBAGENT_MAX_DEPTH` (default 5). This worker doesn't spawn at all.
- **`COST_UNACCOUNTED`** — if a child crashes before reporting `usage`, the
  parent logs this marker rather than under-reporting spend. Look for it in the
  trace (`fabri traces show <session-id>`).

## Cheaper alternatives — don't spawn when you don't need to

A `spawn_subagent` call re-runs the whole agent loop (~15× the cost of a plain
tool call). Reach for it only for genuinely independent subtasks too large for
one context. For N similar calls with no branching, use the `batch` tool; for
N calls with loops/aggregation, use `python_exec`. Fan-out is the expensive
tool in the box — this example shows when it earns its cost.

## Next

- `../03-pipeline-verifier/` — the static `tools.agents[]` form: a draft→verify loop.
