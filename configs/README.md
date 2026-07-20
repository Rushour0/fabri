# configs/

Two canonical configs ship in this directory:

| file | purpose | guarantees |
|---|---|---|
| `example.yaml` | A runnable starter for a fresh checkout. Tweak freely. | None — allowed to drift between releases. |
| `benchmark.yaml` | The locked config for `session_delta`, LongMemEval, and related agent-memory measurements. Dynamic roster experiments use their dataset and pinned roster revision instead. | Any value change requires a minor version bump AND a [BENCHMARKS.md](../BENCHMARKS.md) note. |

Every published chart or number must name its config or dataset. Do not assume
`benchmark.yaml` for a dynamic roster-company result.

## Quickstart

```bash
pip install 'fabri[sqlite]'
export ANTHROPIC_API_KEY=sk-ant-...

# Try the example
fabri --config configs/example.yaml run "list every README in src/"

# Reproduce a benchmark
python -m fabri.benchmarks.session_delta \
  --config configs/benchmark.yaml \
  --task "your fixed task" \
  --runs 10
```

Neither config requires docker. Both use the sqlite-vec embedded memory
backend so a single `pip install` is enough.
