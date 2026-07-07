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

### Offline retrieval eval (recall@k / MRR)

Fast, deterministic, credit-free eval of the retrieval layer itself (fabri's
real `_retrieve_inner` over a labeled fixture — 40 guidelines, 24 queries,
top_k=5). Runs in CI as a regression gate on the shipped `dense` default.
Reproduce: `python -m fabri.benchmarks.retrieval_eval`.

Numbers below are **post the BM25 FTS5 fix** (2026-07-07 — see finding 1).

| strategy | recall@1 | recall@3 | recall@5 | MRR |
|---|---|---|---|---|
| dense (shipped default) | 0.125 | 0.688 | 0.792 | 0.442 |
| sparse (BM25) | 0.167 | 0.750 | 0.875 | 0.533 |
| hybrid (RRF) | 0.125 | 0.604 | **0.938** | 0.451 |
| hybrid+mmr | 0.479 | 0.542 | 0.646 | 0.697 |

**Findings** (this is what the gate is _for_):
1. **BM25 was a silent no-op on the SQLite backend — found and fixed.** Before
   the fix, `sparse` and `hybrid` were byte-identical to `dense` (all recall@5 =
   0.792) because `SqliteMemoryStore.query_bm25` returned `[]` for any
   multi-word query: `_fts5_query` space-joined tokens, and FTS5 reads a space
   as implicit **AND**, so a guideline had to contain _every_ query word to
   match. The fix (OR-join + split tool-name underscores) lifts **hybrid
   recall@5 0.792 → 0.938** and sparse to 0.875. A regression guard
   (`test_hybrid_bm25_is_alive`) now asserts hybrid beats dense so BM25 can't
   silently die again. This is the evidence base for the Track-M `M5`/`D3`
   default-strategy flip (still deferred — a separate reviewed decision).
2. **MMR massively improves early rank.** `hybrid+mmr` lifts recall@1 0.125→0.479
   and MRR 0.44→0.70 (trading recall@5) — it surfaces the single most relevant
   guideline first far more often, which is what matters when only the top few
   entries fit the prompt budget.

_Gate floors (dense − 0.05) live in `tests/test_retrieval_eval_gate.py`; bump
them only with a new row here._

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
