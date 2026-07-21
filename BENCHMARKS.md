# fabri benchmarks

The honest-numbers story for fabri's strategic claim:
*"The self-improving agent runtime with honest COGS."*

A claim is only worth as much as the experiment you'd run to falsify it.
Fabri keeps setup qualification, retrieval quality, live memory behavior, and
public memory accuracy as separate evidence tracks so one cannot stand in for
another.

| benchmark | what it measures | status |
|---|---|---|
| **company setup qualification** | Whether a compiled roster completes its required delegation tree, satisfies a frozen deterministic rubric, and stays inside its company budget across fresh replicas. | None of the three companies clear the 100% bar at adequate sample size. Support HQ passed the 3-replica gate 3/3 but only 9/10 (90%, 95% CI 60-98%) at 10 replicas; Reliability Labs 2/3 (67%, 95% CI 21-94%) rubric; Revenue Ops 0/3 (0%, 95% CI 0-56%) rubric — at n=3 the Reliability Labs and Revenue Ops CIs are wide enough that these are underpowered reads, not precise measurements |
| **session-N+1 cost delta** | The "agent gets cheaper per session" claim — cost per task drop across N runs of the same task with the memory loop active. fabri's own metric. | first result landed: ↓7.8% cost, steps 5→4, reuse 0→67% — **caveat: `gpt-4o-mini` on a constructed failure-recovery task, not the canonical anthropic-sonnet config; canonical sonnet number still pending** |
| **offline retrieval eval** | Whether retrieval finds hand-labeled relevant guidelines without spending model credits. | hybrid: recall@5 0.938, MRR 0.844; CI-gated |
| **LongMemEval** | The "memory loop is real" claim — exact-match accuracy on the [LongMemEval](https://github.com/xiaowu0162/LongMemEval) public dataset. Apples-to-apples with Mastra (94.87%), Letta, Zep. | runner shipped, results pending |
| **company memory & generational evolution** | Whether fabri's memory makes a *company* better or cheaper — a 60-arm memory-vs-control study plus immutable generational accumulation (gen-001 → gen-002). OpenAI-only, 3 roster companies. | **inconclusive — the evaluator is broken, not the agent.** The forbidden-term rubric is a substring match that false-positives on negations (flags the correct "no evidence a fix was deployed"); corrected, Reliability memory is 8/8 not 5/8 and gen-002 ties gen-001 on quality, cost neutral. The self-mining control isn't a no-memory baseline (0 clean control completions on 2/3 companies). Revenue Ops was a config failure (`max_tokens: 768` truncation; 768→2048 fix verified). Net: fix the instruments (negation-aware/LLM-judge rubric + true no-memory control) before any cost claim. See [qualitative analysis + 118 per-run logs](benchmarks/results/run-logs-2026-07-22/QUALITATIVE-ANALYSIS.md) and [consolidated report](benchmarks/results/company-generation-evolution-2026-07-22.md) |

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

### Statistical conventions

- **Every k/N rubric rate is reported with its 95% Wilson score interval**
  (via `fabri.benchmarks.stats.fmt_rate`), e.g. `7/10 (70%, 95% CI 40-89%)`.
  The Wilson interval stays well-behaved at small N and at the 0%/100%
  extremes, unlike a naive normal approximation.
- **Small-n results are descriptive by default.** At n=3–10, Wilson intervals
  routinely span 30-60 points of probability. The intervals below describe
  each arm separately; interval overlap is not a hypothesis test. No paired
  significance test was run for these experiments, so we report the observed
  deltas without claiming a statistically significant difference.
- **Completed-vs-attempted denominator rule:** a rubric rate is always
  computed over runs that actually completed. If an arm's completion count
  is below the nominal replica count (e.g. 9/10 completed, not 10/10), we
  report the as-completed rate as primary (e.g. 7/9) **and** a conservative
  reading against the full nominal denominator (7/10) so a reader can't be
  misled by silently dropping the failed run from the base. Any cross-arm
  comparison uses the conservative (matched-denominator) reading.

### Gated vs. ungated evidence

Not all rows in this document carry the same evidentiary weight. Two
metrics are **CI-gated regression tests** — they run in CI, fail the build
on regression, and are re-measured on every relevant change: the
[offline retrieval eval](#offline-retrieval-eval-recallk--mrr) (recall/MRR
floors in `tests/test_retrieval_eval_gate.py`) and the
[retrieval configuration invariance check](#memory-vs-control)
(config-invariance / hybrid+mmr gates). Everything else — company setup
qualification, memory-vs-control, and the session-N+1 cost delta — is a
**one-off live measurement**: a point-in-time result from a specific run on a
specific date, not re-run automatically, and not enforced by CI. Treat the
gated pair as durable regression protection and everything else as dated
evidence that can go stale; re-read the date column before citing a number
from a live-measurement row.

## Results

### Company setup qualification

| date | company / task | completed | rubric given completion | end-to-end | median cost | decision | fabri |
|---|---|---:|---:|---:|---:|---|---|
| 2026-07-20 | Support HQ / safe incident response (3 replicas) | 3/3 | 3/3 (100%, 95% CI 44-100%) | 3/3 | $0.020200 | small-sample pass — did **not** replicate at 10 replicas (see below) | 0.18.5 |
| 2026-07-20 | Support HQ / safe incident response (10 replicas, confirmation) | 10/10 | 9/10 (90%, 95% CI 60-98%) | 9/10 | $0.021100 | **does not qualify** — below the 100% bar; one run omitted the required follow-up/further-update commitment | 0.18.5 |
| 2026-07-20 | Reliability Labs / setup qualification | 3/3 | 2/3 (67%, 95% CI 21-94%) | 2/3 | — | **does not qualify** — one run over-claimed "fix was deployed" (forbidden unverified-release-claim phrase); n=3, underpowered | 0.18.5 |
| 2026-07-20 | Revenue Ops / setup qualification | 2/3 | 0/3 (0%, 95% CI 0-56%) | 0/3 | — | **does not qualify** — one run truncated operationally; both completed runs over-claimed forbidden phrases ("customer result", "buying intent"); n=3, underpowered | 0.18.5 |

**Honest takeaway:** at adequate sample size, **none** of the three companies
clear the 100% qualification bar. Support HQ's 3-replica gate passed 3/3, but
that was a statistically fragile small-sample result — a 10-replica
confirmation came back 9/10 (90%, 95% CI 60-98%), below the bar. This is the
harness working as intended, not a regression: a 3-replica gate is not enough
evidence to call a company "qualified," and going forward none of these
companies should be described that way except to note the 3-replica Support HQ
result as a small-sample pass later overturned by a larger sample. Reliability
Labs (2/3, 95% CI 21-94%) and Revenue Ops (0/3, 95% CI 0-56%) were only ever
run at n=3 — their CIs are wide enough to span most of the plausible range, so
"failed" is the correct call on the frozen rubric, but the *precise* rate at
those companies is not yet resolved either.

The original 3-replica Support HQ gate cost **$0.060496**; the 10-replica
confirmation, Reliability Labs, and Revenue Ops probes brought total live
spend across the 2026-07-20 matrix to **~$0.70** (plus a ~$0.06 validation
run). A proposed 256-token floor for delegated artifact roles received three
fresh preflights but **zero model runs**: every applicable role already met
the floor, so the candidate was rejected as `candidate_noop`. Earlier pilots
and classifier validation brought total research spend to **$0.272837**.

None of these results establish that memory improves setup outcomes — the
isolated training/holdout versus fresh-control study remains pending, and
"setup qualification" is not "memory win" (see [Honest gaps](#honest-gaps)
below). Read the reviewed result and lessons for each run:
[Support HQ, 3-replica](benchmarks/results/support-hq-setup-qualification-2026-07-20.md)
([JSON](benchmarks/results/support-hq-setup-qualification-2026-07-20.json)),
[Support HQ, 10-replica](benchmarks/results/support-hq-setup-qualification-10replica-2026-07-20.md)
([JSON](benchmarks/results/support-hq-setup-qualification-10replica-2026-07-20.json)),
[Reliability Labs](benchmarks/results/reliability-labs-setup-qualification-2026-07-20.md)
([JSON](benchmarks/results/reliability-labs-setup-qualification-2026-07-20.json)),
[Revenue Ops](benchmarks/results/revenue-ops-setup-qualification-2026-07-20.md)
([JSON](benchmarks/results/revenue-ops-setup-qualification-2026-07-20.json)).

### Memory vs. control

Isolated training/holdout study: train the company on a related task (it
writes learned guidelines to memory), then run a fresh holdout task twice —
once with that SQLite memory copied into a clean compile (memory arm), once
with empty memory (control arm). This is the first live result that isolates
memory's effect on outcome, distinct from setup qualification above.

| date | company / task | replicas (memory / control completed) | guidelines retrieved (memory / control) | rubric (memory) | rubric (control) | rubric delta | mean cost delta | fabri |
|---|---|---:|---|---:|---:|---:|---:|---|
| 2026-07-20 | Support HQ / holdout task (3-replica pilot) | 3 / 3 | 2 / 0 | 3/3 (100%, 95% CI 44-100%) | 2/3 (67%, 95% CI 21-94%) | +33 pp (descriptive; no paired significance test) | +$0.0009 (~1.5%) | 0.18.5 |
| 2026-07-20 | Support HQ / holdout task (10-replica confirmation) | 10 / 10 | 2.0 / 0 | 7/10 (70%, 95% CI 40-89%) | 9/10 (90%, 95% CI 60-98%) | −20 pp (descriptive; no paired significance test) | **−$0.0014** (mean $0.0600 vs $0.0614) | 0.18.5 |
| 2026-07-21 | Reliability Labs / holdout task | 10 / **9** | 2.0 / 0 | 6/10 (60%, 95% CI 31-83%) | 7/9 (78%, 95% CI 45-94%) as-completed; **7/10 (70%, 95% CI 40-89%) conservative** (see note) | **−10 pp** conservative (descriptive; no paired significance test) | **+$0.0184** (mean $0.1150 vs $0.0966) | 0.18.5 |

The control-is-empty sanity check held at both companies: control retrieved
**0** guidelines on every replica, while the memory arm retrieved the same
**2** trained guidelines on every replica — the retrieval mechanism fires
reliably. Completion was 10/10 in both arms for Support HQ at 10 replicas.
**Reliability Labs' control arm completed only 9 of 10 replicas** (one run did
not finish), so its control rubric is properly denominated **7/9 (78%)**, not
7/10 — see [Statistical conventions](#statistical-conventions) for why we
report both readings and use the completed-runs denominator as primary. Memory
was marginally cheaper on Support HQ (mean $0.0600 vs. $0.0614); Reliability
Labs' memory arm was more expensive.

**Honest verdict: the 3-replica pilot was reversed, with no confirmed effect
either way.** The preliminary 3-replica Support HQ
pilot showed memory ahead by +33pp (95% CI 21-94% vs. 44-100% — already
heavily overlapping at n=3). The 10-replica confirmation produced a −20pp
descriptive delta. Because no paired significance test was run, these per-arm
Wilson intervals do not establish whether the difference is statistically
significant. The experiment therefore does not support calling a winner or
asserting that memory loses by 20 points. This is the same
small-sample fragility the setup-qualification study above demonstrated (a 3/3
gate that fell to 9/10 at 10 replicas), now visible in the other direction
too: a small-sample "memory win" can just as easily be a small-sample fluke.
A 10-replica run on **Reliability Labs** shows the same shape — memory 6/10
(60%, 95% CI 31-83%) vs. control 7/9 (78%, 95% CI 45-94%) as-completed, or
7/10 (70%, 95% CI 40-89%) on the conservative completed-vs-attempted basis —
and here too the conservative **−10pp** delta (not the as-completed −18pp, and
emphatically not the previously reported "−18pp") is descriptive; no paired
significance test was run. On both companies,
Fabri's trace-backed memory retrieves lessons reliably, but neither dataset
yet supports a confident claim that memory improves *or* hurts holdout-task
rubric reliability — the honest conclusion is "underpowered, no resolved
effect," across **two** companies, still on one related-task/holdout pair
each, not general memory effectiveness across all workload shapes. One newly
identified factor likely compresses the ceiling on both arms: the trained
memory in these runs held only ~2 guidelines because delegated sub-agent
traces were never mined into memory (only the top-level manager trace was;
see `agent_runner_tool.py`, now fixed) — so the natural next experiment is
re-running this study with the supply-side fix in place, not tuning retrieval
further.

**Retrieval configuration is not the lever.** Re-running the Support HQ memory
arm with `top_k` raised 5→10, and with `hybrid+mmr` selection, left the retrieved
evidence unchanged — memory returned the same **2** guidelines every run in all
three configs, because the trained memory only *contains* ~2 guidelines. You
cannot pull more than exists, so the outcome cannot move. The bottleneck is
upstream — guideline supply/quality (how many good, distinct lessons a training
run mines and promotes) — which is the next experiment. See
[retrieval sweep](benchmarks/results/support-hq-retrieval-sweep-2026-07-21.md).
(Note: the retrieval-count invariance is deterministic and was observed on every
completed pair; a full per-variant rubric re-measurement was cut short by
infrastructure interruptions, but that does not affect the conclusion.)

**Supply smoke — negative, scale stopped.** On 2026-07-21, one memory/control
pair with specialist-trace mining enabled and one with it disabled produced the
same result: the manager supplied 2 guidelines and specialists supplied 0. The
four runs cost $0.217185 total. This does not falsify specialist mining: the old
harness copied only the manager's single SQLite DB into the holdout, and the
short specialist traces produced no promotable candidates. The result is
recorded as a harness-limited null, so no 10× run was launched. The repaired
harness now discovers every compile-declared SQLite DB, reports the
supply→transport→retrieval→outcome funnel, and permits scaling only when both
memory replicas transport and retrieve a specialist-produced entry.

Full results:
[Support HQ](benchmarks/results/support-hq-memory-vs-control-2026-07-20.md)
([JSON](benchmarks/results/support-hq-memory-vs-control-2026-07-20.json)),
[Reliability Labs](benchmarks/results/reliability-labs-memory-vs-control-2026-07-21.md)
([JSON](benchmarks/results/reliability-labs-memory-vs-control-2026-07-21.json)).

### session-N+1 cost delta

| date | task | runs | first $ | median-of-last-3 $ | delta | fabri |
|---|---|---|---|---|---|---|
| 2026-07-19 | read wrong-ext file → recover + summarize (`gpt-4o-mini`, constructed task — see caveat below, **not** canonical sonnet)* | 6 | $0.0006 | $0.0005 | ↓7.8%* | 0.16.4* |

_\*Caveats (read before citing this 7.8% number anywhere): run on
**`gpt-4o-mini`** via `configs/benchmark.openai-recovery.yaml`, **not** the
canonical anthropic-sonnet `configs/benchmark.yaml` (Bedrock Anthropic is
account-gated here and no `ANTHROPIC_API_KEY` was set). It is a **constructed
failure-recovery task** (the workspace deliberately hides the file under a
different extension) and depends on the **guideline-dedup fix** (deterministic
`dedup_key`) — without that fix cross-session reuse is structurally stuck at
0% and this delta does not appear. Full run:
`.fabri/benchmarks/1784483*/results.md`. **The canonical sonnet number is
still pending** — do not cite 7.8% as fabri's general session-N+1 result._

Beyond cost: **steps 5 → 4** (run 1 recovered from a failed read via `list_dir`;
runs 2–6 skipped the failed call and went straight to `list_dir`), **outcome
success_with_recovery → success**, **guideline-reuse 0% → 67%**. The agent learned
to avoid its own first-run mistake — the self-improving loop working end to end.

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

Company setup qualification has now run live for all three companies, and
**none qualify at adequate sample size.** Support HQ's 3-replica gate passed
3/3 but a 10-replica confirmation came back 9/10 (90%, 95% CI 60-98%) — below
the 100% bar. Reliability Labs (2/3, 95% CI 21-94%) and Revenue Ops (0/3, 95%
CI 0-56%) both failed their rubric outright on frozen prompts and assertions,
though at n=3 those two are underpowered reads in their own right. This shows
the 3-replica gate is statistically fragile, not that the harness is broken —
a small sample overstates confidence. A passing (or fragile-passing) setup
probe must not be reported as a memory win. The isolated memory-vs-control
study ([Memory vs. control](#memory-vs-control) above) — Support HQ — shows
the same fragility biting in the other direction: a 3-replica pilot showed
memory +33pp ahead of control, but the 10-replica confirmation shrank that
gap without establishing a resolved loss — memory's 7/10 (95% CI 40-89%) and
control's 9/10 (95% CI 60-98%) yield a descriptive −20pp delta, but no paired
significance test was run. The retrieval mechanism itself is proven reliable
(2 guidelines fetched
on every memory run, 0 on every control run), but on this workload the honest
conclusion is that neither company's data yet supports a confident claim that
memory improves *or* hurts holdout-task reliability — the same underpowered
pattern holds on Reliability Labs (6/10 memory vs. 7/9 completed / 7/10
conservative control, CIs overlapping). This covers two companies on one
related-task/holdout pair each, not general memory effectiveness.

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
