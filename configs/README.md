# configs/

Two canonical configs ship in this directory:

| file | purpose | guarantees |
|---|---|---|
| `example.yaml` | A runnable starter for a fresh checkout. Tweak freely. | None — allowed to drift between releases. |
| `benchmark.yaml` | The exact config every published fabri benchmark uses. | Locked. Any value change requires a minor version bump AND a [BENCHMARKS.md](../BENCHMARKS.md) note. |

If a published chart or number doesn't say which config it used, assume
`benchmark.yaml` from the same fabri version as the chart.

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
