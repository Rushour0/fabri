# Qualitative analysis of 60 agent runs (2026-07-22)

**The negative "memory doesn't help" headline was a measurement artifact.** The agents
produced correct, careful work. Our evaluator — a substring rubric plus a contaminated
control — could not see it, and in one mode actively penalized the right behavior.

Evidence: 118 verbatim per-run logs in `per-run/`, structured metrics in `index.json`,
aggregates in `summary.json`. Every number below is reproducible from `index.json`.

## Finding 1 — the rubric produces negation false positives (the core bug)

The forbidden-term check is a naive substring match. It flags a correct answer that
*denies* the forbidden claim.

Reliability Labs holdout, `study-memory-holdout`, raw vs negation-corrected:

| | raw pass | corrected pass |
|---|---:|---:|
| memory arms that ran (n=8 completed) | 5/8 (62%) | **8/8 (100%)** |

All three raw failures (replicas 04, 05, 06) dropped only on the forbidden phrase
`"fix was deployed"`, each time inside a negation:

- r04: "No crew supplied evidence that a corrective **fix was deployed**."
- r05: "…**not** that a corrected checkout **fix was deployed**."
- r06: "no crew supplied evidence that a corrected **fix was deployed**."

These are exactly the safety statement the task demands (do not claim resolution). The
rubric scored the correct behavior as a violation. The one remaining "real" forbidden hit
after correction (control r06) is *also* semantically negated — the "no" sits just beyond
the 60-char window — so the corrected numbers are a conservative floor, not a ceiling.

Support HQ was unaffected (10/10 raw = 10/10 corrected): its agents happened to phrase the
same idea without the trigger substring ("No fix deployment is claimed"). **Whether a
correct answer passes depends on phrasing luck, not correctness.** That is an invalid
instrument.

## Finding 2 — generation 2 did not regress; it tied

`evo-incumbent` = gen-001, `evo-candidate` = gen-002, on the frozen evolution variants:

| Company | raw pass (inc → cand) | corrected pass (inc → cand) |
|---|---|---|
| Reliability Labs | 3/3 → 2/3 | **3/3 → 3/3** |

The single raw "quality regression" that blocked promotion was the same
`"fix was deployed"` negation false positive. Corrected, gen-001 and gen-002 are a quality
tie. Read the two full outputs side by side (`per-run/reliability-labs/evo-*__replica-01__anchor_release_readiness.md`):
both return a correct NO-GO / NO-SHIP release-gate decision with the rollback facts and the
missing production config framed as a verification item. gen-002 was also marginally
*cheaper* on that pair ($0.0356 vs $0.0382).

## Finding 3 — cost is neutral across generations

Median evaluation-arm cost, gen-001 → gen-002:

- Support HQ: $0.0326 → $0.0328 (flat; mean +5%, driven by one variant).
- Reliability Labs: $0.0588 → $0.0592 (flat).

Accumulating a second memory generation neither lowered nor materially raised cost. The
earlier claim that gen-002 was "consistently more expensive" was wrong; it was cost-neutral.

## Finding 4 — the control is not a no-memory baseline

Control holdouts mine fresh lessons into a new DB and retrieve them *within the same run*,
so the study marks them incomplete (`control_guidelines_retrieved_nonzero`) and excludes
them — 0 "completed" control arms on Reliability Labs and Revenue Ops. But they still
produce good answers: Reliability control corrected pass is 7/9. So the comparison cannot
isolate a memory effect from two sides at once: the control is contaminated *and* the
rubric is broken. No causal memory-vs-control claim is currently supportable.

## Finding 5 — training and holdout are different scenarios

The training task and the holdout task are different incidents (training here is a
double-charge/billing brief; the holdout is the checkout rollback). This is why the
holdout rubric cannot score training output, and why verification of training-mined
lessons must gate on *operational training success*, not the holdout rubric. (This was
caught live and corrected mid-session; see the handoff.)

## Finding 6 — Revenue Ops was a config failure, not a memory failure

Every Revenue Ops training truncates: `market-research-brief`'s `researcher`/`writer` are
capped at `max_tokens: 768` on a verbose model, the engine's one retry to 1536 still
overflows, and the run fails. Raising the cap to 2048 makes training succeed and mine 6
verified lessons. Nothing about memory was tested here until the config is fixed.

## What this means for an agent-improvement platform

The loop is **quality first, cost later**. Before any "cheaper per run" work, the evaluator
must be able to tell a good run from a bad one. Right now it cannot:

1. **Fix the evaluator before optimizing anything.** Replace substring forbidden-matching
   with negation-aware / semantic checking (or an LLM judge with a rubric), and replace the
   self-mining control with a true no-memory control (mining and retrieval both disabled in
   the holdout). Until then, pass/fail is noise.
2. **The per-run log is the unit of improvement.** `per-run/` now captures every run's verbatim
   output plus mechanical metrics. That is the substrate: read runs, label real quality,
   feed labels back. An evolving agent needs a trustworthy quality signal per run before a
   cost signal means anything.
3. **What we actually fixed this session** (independent of the null result): the
   `training_success` accounting bug (+ regression tests), the Revenue Ops truncation
   (verified fix), training-lesson verification (item 6), and a read-time-reconciling
   aggregator. And we falsified two instruments — the rubric and the control — which is the
   most useful output here.

## How to reproduce

```bash
.venv/bin/python benchmarks/results/run-logs-2026-07-22/extract_quality_logs.py
# writes index.json, summary.json, and per-run/<company>/<phase>__replica-NN[__variant].md
```
