# fabri benchmarks

The honest-numbers story for fabri's strategic claim:
*"The self-improving agent runtime with honest COGS."*

A claim is only worth as much as the experiment you'd run to falsify it.
Fabri keeps setup qualification, retrieval quality, live memory behavior, and
public memory accuracy as separate evidence tracks so one cannot stand in for
another.

| benchmark | what it measures | status |
|---|---|---|
| **company setup qualification** | Whether a compiled roster completes its required delegation tree, satisfies a frozen deterministic rubric, and stays inside its company budget across fresh replicas. | Support HQ baseline qualified 3/3; Reliability Labs and Revenue Ops setup probes pending |
| **session-N+1 cost delta** | The "agent gets cheaper per session" claim — cost per task drop across N runs of the same task with the memory loop active. fabri's own metric. | first result landed (gpt-4o-mini, failure-recovery task): ↓7.8% cost, steps 5→4, reuse 0→67%; canonical sonnet number still pending |
| **offline retrieval eval** | Whether retrieval finds hand-labeled relevant guidelines without spending model credits. | hybrid: recall@5 0.938, MRR 0.844; CI-gated |
| **LongMemEval** | The "memory loop is real" claim — exact-match accuracy on the [LongMemEval](https://github.com/xiaowu0162/LongMemEval) public dataset. Apples-to-apples with Mastra (94.87%), Letta, Zep. | runner shipped, results pending |

The memory runners use [`configs/benchmark.yaml`](configs/benchmark.yaml) as
their locked source of truth. Dynamic company experiments instead use
[`benchmarks/datasets/company_memory_experiments.yaml`](benchmarks/datasets/company_memory_experiments.yaml)
plus a pinned `FABRI_ROSTERS_ROOT` revision. Every published result must name
the applicable config or dataset, Fabri version, and external roster revision.

## Reproducing

The base recipe for every fabri benchmark:

```bash
pip install 'fabri[sqlite]'
export ANTHROPIC_API_KEY=sk-ant-...
```

Then pick a benchmark:

### Company setup qualification

Qualify a roster before spending on its memory/control study. The probe compiles
a fresh company and isolates `FABRI_HOME` for every replica; raw prompts, traces,
and model output stay private while reviewed aggregates can be published.

```bash
export FABRI_ROSTERS_ROOT=/path/to/fabri-rosters
python -m fabri.benchmarks.company_setup_probe \
  --dataset benchmarks/datasets/company_memory_experiments.yaml \
  --case support_hq_safe_incident_response \
  --output-dir benchmarks/runs/support-hq-setup-probe
```

Pin and report the roster Git revision before running. See the
[dataset protocol](benchmarks/datasets/README.md) for isolation and scoring
rules.

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

### Company setup qualification

| date | company / task | completed | rubric given completion | end-to-end | median cost | decision | fabri |
|---|---|---:|---:|---:|---:|---|---|
| 2026-07-20 | Support HQ / safe incident response | 3/3 | 3/3 | 3/3 | $0.020200 | baseline qualified | 0.18.5 |

The released three-run gate cost **$0.060496**. A proposed 256-token floor for
delegated artifact roles received three fresh preflights but **zero model
runs**: every applicable role already met the floor, so the candidate was
rejected as `candidate_noop`. Earlier pilots and classifier validation brought
total research spend to **$0.272837**.

This result qualifies the existing Support HQ setup; it does **not** establish
that memory improves it. The isolated training/holdout versus fresh-control
study remains pending. Read the reviewed
[result and lessons](benchmarks/results/support-hq-setup-qualification-2026-07-20.md)
or the [machine-readable aggregate](benchmarks/results/support-hq-setup-qualification-2026-07-20.json).

### session-N+1 cost delta

| date | task | runs | first $ | median-of-last-3 $ | delta | fabri |
|---|---|---|---|---|---|---|
| 2026-07-19 | read wrong-ext file → recover + summarize | 6 | $0.0006 | $0.0005 | ↓7.8% | 0.16.4* |

Beyond cost: **steps 5 → 4** (run 1 recovered from a failed read via `list_dir`;
runs 2–6 skipped the failed call and went straight to `list_dir`), **outcome
success_with_recovery → success**, **guideline-reuse 0% → 67%**. The agent learned
to avoid its own first-run mistake — the self-improving loop working end to end.

_\*Caveats (read before citing): run on **`gpt-4o-mini`** via
`configs/benchmark.openai-recovery.yaml`, **not** the canonical anthropic-sonnet
`configs/benchmark.yaml` (Bedrock Anthropic is account-gated here and no
`ANTHROPIC_API_KEY` was set). It is a **constructed failure-recovery task** (the
workspace deliberately hides the file under a different extension) and depends on
the **guideline-dedup fix** (deterministic `dedup_key`) — without that fix
cross-session reuse is structurally stuck at 0% and this delta does not appear.
Full run: `.fabri/benchmarks/1784483*/results.md`. The canonical sonnet number is
still pending._

### Self-improvement integration contracts

These are deterministic, offline integration checks for the two ways fabri
uses a learned lesson. They are deliberately **not** a replacement for the
live-model session-N+1 result above: a scripted backend follows the retrieved
lesson so that CI can catch a wiring or accounting regression without claiming
that a particular model will generalize the same way.

| date | path | baseline | learned path | measured delta | fabri |
|---|---|---|---|---|---|
| 2026-07-20 | dynamic retrieved guideline | 2 turns, 2,000 input + 200 output tokens, $0.00042 | 1 turn, 1,000 input + 100 output tokens, $0.00021 | **↓50.0% priced COGS**, one unnecessary tool turn removed; both complete successfully | 0.18.2 |
| 2026-07-20 | `fabri repo open-pr` | no durable projection | promoted strategic lesson is written only to a proposed branch and surfaced in a draft PR | source config unchanged; **1/1** learned block and **1/1** draft-PR payload asserted | 0.18.2 |

The dynamic row uses the pricing table for `gpt-4o-mini` and fixed per-turn
usage emitted by the test backend. Reproduce with:

```bash
pytest -q tests/test_integration_self_improvement.py
```

The GitHub path uses a fake provider: it verifies the exact branch content and
draft-PR request without opening a real pull request from CI. The existing
provider adapter tests cover GitHub's request shape and deduplication.

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

Numbers below are **post the BM25 FTS5 fix, the RRF-k retune, and the
success-slot back-load** (2026-07-07 — see findings).

| strategy | recall@1 | recall@3 | recall@5 | MRR |
|---|---|---|---|---|
| dense (pre-v0.9.x default, now fallback) | 0.583 | 0.688 | 0.792 | 0.790 |
| sparse (BM25) | 0.500 | 0.792 | 0.875 | 0.772 |
| **hybrid (RRF) — shipped default** | **0.583** | **0.896** | **0.938** | **0.844** |
| hybrid+mmr | 0.583 | 0.625 | 0.729 | 0.804 |

**hybrid is now the best strategy on every metric**, not just recall@5 — the
default flip is unambiguous. It also degrades gracefully to dense wherever BM25
is unavailable (Qdrant without `fabri[bm25]`), so it is never worse than the old
default. MMR stays opt-in — on this fixture it trades recall for diversity with
no rank-1 gain left to capture, so it is not the default.

**Findings** (this is what the gate is _for_):
1. **BM25 was a silent no-op on the SQLite backend — found and fixed.** Before
   the fix, `sparse` and `hybrid` were byte-identical to `dense` because
   `SqliteMemoryStore.query_bm25` returned `[]` for any multi-word query:
   `_fts5_query` space-joined tokens, and FTS5 reads a space as implicit **AND**,
   so a guideline had to contain _every_ query word to match. Fix: OR-join +
   split tool-name underscores. Guarded by `test_hybrid_bm25_is_alive`.
2. **RRF `k` was mistuned for the pool size (60 → 20).** RRF scores
   `Σ 1/(k+rank)`; the web-scale default `k=60` flattens the rank term over
   fabri's short two-pool fusion, so "appears in both lists" outranks "is the
   single best match in either." That lifts recall@5 but sinks recall@3. Retuning
   to `k=20` (configurable via `memory.rrf_k`) lifted **hybrid recall@3
   0.60 → 0.90** with recall@5 unchanged.
3. **`success_pattern` guaranteed slots were front-loaded — the big one.** The
   merge reserved the **top** `top_k//2` slots for success patterns, injected
   ahead of the most relevant guideline. Only ~1/5 of queries actually want a
   success pattern, so for the rest ranks 1–2 were spent on non-relevant entries,
   capping recall@1. Back-loading the guarantee (relevance owns the head; success
   patterns fill reserved *tail* slots, never rank 1) lifted **recall@1
   0.13 → 0.58 and MRR 0.45 → 0.84** — and it helped dense too (recall@1 also
   0.13 → 0.58), since the front-load hurt every strategy. Guarded by
   `test_success_pattern_does_not_steal_rank_one` and the hybrid MRR/recall@3
   gate floors.

_Gate floors (measured − 0.05) live in `tests/test_retrieval_eval_gate.py`; bump
them only with a new row here. See `docs/retrieval-tuning.md` for how to tune
these knobs on your own corpus._

## Honest gaps

The biggest open question Fabri has not answered yet:

> Does the memory loop generalize across workload shapes?
>
> The session_delta runner has been tested on code-writing-shaped tasks.
> Research, classification, long-form writing, and multi-modal tasks are
> not yet covered.

Company setup qualification is also complete only for Support HQ. Reliability
Labs and Revenue Ops have frozen prompts and assertions, but no released setup
or memory/control result yet. A passing setup probe must not be reported as a
memory win.

When a benchmark gap closes, this paragraph shrinks.

## See also

- [`configs/benchmark.yaml`](configs/benchmark.yaml) — the locked config.
- [`benchmarks/README.md`](benchmarks/README.md) — benchmark artifact map and
  focused test commands.
- [`benchmarks/datasets/company_memory_experiments.yaml`](benchmarks/datasets/company_memory_experiments.yaml)
  — dynamic roster experiment definitions.
- [`benchmarks/results/`](benchmarks/results/) — reviewed, publishable
  aggregates; private runs are intentionally excluded.
- [`src/fabri/benchmarks/`](src/fabri/benchmarks/) — runner source.
- [`decks/internal/code-gaps.md`](decks/internal/code-gaps.md) — the
  internal strategic-claim ↔ codebase gap analysis (gitignored).
