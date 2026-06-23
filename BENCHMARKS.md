# fabri benchmarks

The honest-numbers story for fabri's strategic claim:
*"The self-improving agent runtime with honest COGS."*

A claim is only worth as much as the experiment you'd run to falsify it.
Two benchmarks back the claim — one fabri-specific, one industry-standard.

| benchmark | what it measures | status |
|---|---|---|
| **session-N+1 cost delta** | The "agent gets cheaper per session" claim — cost per task drop across N runs of the same task with the memory loop active. fabri's own metric. | runner shipped, results pending |
| **LongMemEval** | The "memory loop is real" claim — exact-match accuracy on the [LongMemEval](https://github.com/xiaowu0162/LongMemEval) public dataset. Apples-to-apples with Mastra (94.87%), Letta, Zep. | runner shipped, results pending |

If you re-run any benchmark and get a different number, **the
[`configs/benchmark.yaml`](configs/benchmark.yaml) file** is the
single source of truth. Any change to that file requires a fabri
minor-version bump *and* a results entry below.

## Reproducing

The base recipe for every fabri benchmark:

```bash
pip install 'fabri[sqlite]'
export ANTHROPIC_API_KEY=sk-ant-...
```

Then pick a benchmark:

### session-N+1 cost delta

Run the same task N times against fresh memory. Report cost / outcome /
guideline-reuse per run; the headline is the median-of-last-3 vs first-run
delta.

```bash
python -m fabri.benchmarks.session_delta \
  --config configs/benchmark.yaml \
  --task "list every README in src/" \
  --runs 10
```

Output: stderr per-run line, stdout markdown summary, JSON + markdown
under `.fabri/benchmarks/<timestamp>/`.

### LongMemEval

Public memory benchmark. Per-case isolated memory, exact-match scoring
shipped; LLM-judge variant behind `--judge` (doubles API spend).

```bash
pip install datasets  # required only for LongMemEval
python -m fabri.benchmarks.longmemeval \
  --config configs/benchmark.yaml \
  --limit 100             # full eval is ~10k; start small
```

Output: same layout as session_delta — JSON results + markdown summary
under `.fabri/benchmarks/longmemeval_<timestamp>/`.

## Methodology

### Config

[`configs/benchmark.yaml`](configs/benchmark.yaml). Locked per minor
version. Every comment in that file is part of the methodology — it
explains *why* each strategic value was chosen.

### Hardware / runtime

- Model: claude-sonnet-4-6 (Anthropic, mid-tier, $3 input / $15 output
  per Mtok at writing).
- Embeddings: `sentence-transformers/all-MiniLM-L6-v2`, local CPU.
- Memory backend: sqlite-vec (in-process; no docker timing variance).
- Wall-clock numbers are reported alongside cost so you can spot a
  rate-limit-padded run.

### What "fair" means

- The agent has no prior task-specific tuning. The memory loop runs
  cold on run 1.
- Tasks are written without seeing fabri's failure modes — no
  retrofitted "tasks that happen to suit the loop."
- LLM nondeterminism: every chart is median ± IQR over N≥10 runs of
  the same task.
- "Cost delta" is gross USD cost from the run's `usage` event, including
  the cache write/read economics fabri prices in `fabri.pricing`.

### What we deliberately don't claim

- A drop in cost on a task fabri's memory loop *can't* learn from (e.g.
  pure single-step lookups) — the chart will go flat and we'll report
  the flat chart.
- Wins from prompt engineering. The published number uses
  `system_prompt: ""` so the score is the framework's behaviour, not
  ours.
- Vendor lock-in benefits. The benchmark config uses one provider; we'd
  expect cross-provider numbers to track within 10%.

## Results

### session-N+1 cost delta

| date | task | runs | first $ | median-of-last-3 $ | delta | fabri |
|---|---|---|---|---|---|---|
| — | — | — | — | — | — | — |

_Add a row here when a real run lands. Keep the task description short
(<60 chars) — paste the full task into the corresponding
`.fabri/benchmarks/<timestamp>/results.md` file and link if needed._

### LongMemEval

| date | cases | exact-match | judge | reference | fabri |
|---|---|---|---|---|---|
| — | — | — | — | — | — |

_Cite the reference scores side-by-side so the comparison is honest:
Mastra "Observational Memory" 94.87%, Letta, Mem0, Zep (63.8%), as
published mid-2026._

## Honest gaps

The single biggest open question fabri hasn't answered yet:

> Does the memory loop generalize across workload shapes?
>
> The session_delta runner has been tested on code-writing-shaped tasks.
> Research, classification, long-form writing, and multi-modal tasks are
> not yet covered.

When a benchmark gap closes, this paragraph shrinks.

## See also

- [`configs/benchmark.yaml`](configs/benchmark.yaml) — the locked config.
- [`src/fabri/benchmarks/`](src/fabri/benchmarks/) — runner source.
- [`decks/internal/code-gaps.md`](decks/internal/code-gaps.md) — the
  internal strategic-claim ↔ codebase gap analysis (gitignored).
