# Memory vs a *true* no-memory control, with a fixed rubric (2026-07-22)

- **Fabri version:** 0.19.1 (+ this branch)
- **Dataset:** `benchmarks/datasets/company_memory_experiments.yaml`
- **Roster revision:** `fabri-rosters@410fbcf`
- **Provider:** OpenAI only (`gpt-5.6-terra` managers/specialists, `gpt-4o-mini`/`gpt-5-nano` crews).
  AWS/Anthropic/Gemini/Google credentials explicitly unset for every run.
- **Design:** 3 companies × 6 replicas × {memory, control}. Raw material under the ignored
  `benchmarks/runs/eval-fixed-20260722/**`; only curated aggregates appear here.

## Headline

**The instrument was the confound — in both directions.** The previous study's negative result
("memory doesn't help, and a second generation hurts") and this study's *initial* positive result
("memory +100pp") were both artifacts of substring rubric matching. With both defects fixed and a
genuinely memory-free control, **memory and control are statistically indistinguishable on quality
and on cost.**

## Two instrument bugs, opposite signs

| Side | Defect | Error produced | Fixed by |
|---|---|---|---|
| Forbidden terms | naive substring, no negation | false **positives** — a correct *denial* scored as a violation ("no crew supplied evidence that a corrective **fix was deployed**") | sentence-scoped negation window (was a fixed 60 chars) |
| Required terms | naive substring, no synonym/proximity | false **negatives** — a correct answer scored as missing ("share **further customer-facing updates**" vs required literal `further update`) | order-preserving proximity match within one sentence (≤4 intervening words, plural/compound tolerant) |

The forbidden fix was already partly in place; **the required side had never been fixed** and is
what produced the spurious "+100pp memory win" in this study's own smoke run. Both are now covered
by tests asserting they do not overshoot (a negation in a *previous* sentence still flags; a text
with no follow-up commitment is still missing).

### A third defect: the control was never a control

A "control" holdout previously started with no transported DB but still **mined and retrieved its
own lessons within the same run** — contamination the study could only *detect* after the fact
(`control_guidelines_retrieved_nonzero`), which is why 2 of 3 companies had **zero** usable control
arms. There was no way to actually turn memory off. This adds `memory.mining_enabled` and
`memory.retrieval_enabled`, gates both `process_trace` call sites and short-circuits retrieval, and
the study now writes both `False` into every compiled node for control arms.

**Verified:** control arms retrieve **0** guidelines (memory arms retrieve 8–13). The contamination
detector never fired. This is the first run in which memory-vs-control is actually measurable.

## Results

Pass rates are **raw** (verdict recorded at run time) vs **corrected** (same outputs re-scored with
the fixed rubric via `benchmarks/rescore_runs.py`, no re-spend).

### Support HQ

| condition | n | raw pass | corrected pass | flips |
|---|---:|---:|---:|---|
| control | 6 | 100% | 100% | 0 |
| memory | 6 | 100% | 100% | 0 |

Paired cost (1 pair excluded — replica 1 memory arm **training timeout**, nothing mined, died early
at $0.0224):

| clean pairs | mean Δ | median Δ | memory cheaper | sign test |
|---:|---:|---:|---:|---:|
| 5 | +$0.0010 | +$0.0028 | 2/5 | **p = 1.0000** |

### Reliability Labs

| condition | n | raw pass | corrected pass | flips |
|---|---:|---:|---:|---|
| control | 6 | 83.3% | **100%** | 1 (replica 3, `['verification']` → `[]`) |
| memory | 6 | 80.0% | **100%** | 1 (replica 4, `['verification']` → `[]`) |

Both conditions flip to 100% under the corrected rubric, and the flips are **symmetric** (one per
condition) — the instrument bug was noise, not a bias toward either arm.

Paired cost (1 pair excluded — replica 3 memory arm training-failed/incomplete):

| clean pairs | mean Δ | median Δ | memory cheaper | sign test |
|---:|---:|---:|---:|---:|
| 5 | +$0.0133 | +$0.0182 | 2/5 | **p = 1.0000** |

### Revenue Ops

This company had flips in **both** directions, and is the one place the corrected rubric made a
*stricter* call:

| condition | complete arms | raw pass | corrected pass |
|---|---:|---:|---:|
| control | 6 | 66.7% | 66.7% |
| memory | 4 (2 arms incomplete) | 100% | 100% |

- Control replicas 1 and 3 flipped **PASS → FAIL**: the corrected scorer catches a real
  `'buying intent'` forbidden hit that the old fixed 60-char window had been *wrongly exempting*
  (a negation cue in a **previous sentence** fell inside the character window). Sentence-scoping
  removes that masking — the fix cuts both ways, which is the point.
- Control replicas 4 and 5 flipped **FAIL → PASS** on required `'Maya Chen'` (proximity/morphology).
- Net effect on the headline rate is zero (two flips each way), which is coincidence, not design.

The 100% vs 66.7% looks like a memory win, but it is not one once the comparison is **paired on
arms where both conditions completed** (replicas 2–5): memory 4/4 vs control 3/4 — a **single
discordant pair**, sign test **p = 1.0**. Two memory arms were lost (replica 1 training timeout,
replica 6 incomplete), so the unpaired rates compare n=4 against n=6.

Paired cost (2 pairs excluded — replicas 1 and 6):

| clean pairs | mean Δ | median Δ | memory cheaper | sign test |
|---:|---:|---:|---:|---:|
| 4 | +$0.0314 | +$0.0317 | 0/4 | p = 0.1250 |

Memory was **more expensive on every clean pair here**, though not significantly at n=4.

## What to conclude

- **No significant memory effect on any of the three companies — on quality or on cost.**
  Support HQ and Reliability Labs tie at 100% quality once scored correctly. Revenue Ops *looks*
  like a memory win (100% vs 66.7%) but collapses to 4/4 vs 3/4 — one discordant pair, p = 1.0 —
  once paired on arms where both conditions completed.
- **Cost:** memory cheaper in 2/5, 2/5, and 0/4 clean pairs (p = 1.0, 1.0, 0.125). Every mean Δ is
  *positive*, i.e. memory is marginally **more** expensive — nowhere near significance, but there is
  certainly no evidence for the "cheaper every run" claim.

**Total spend: $4.82 over 38 arms** (36 study + 2 smoke).
- **Do not claim a memory win, and do not claim the old negative result either.** Both prior
  headlines were instrument artifacts. This is the first measurement where the instrument and the
  control are both valid.
- **Quality-signal first.** The per-run log remains the unit of improvement: a rubric that scores
  correct work as failing (or vice versa) makes any cost optimization meaningless.

## Scope limits (explicit)

- **The ActionMemory miner is offline-proven, NOT live-demonstrated.** The rosters fixture already
  contains the `market-research-brief` `max_tokens: 768→2048` fix (`fabri-rosters@410fbcf`), so the
  Revenue-Ops truncation cannot recur live. What is proven by tests is that a miner-produced
  candidate satisfies `recurrence.applicable()` against a matching state and is *refused* against an
  already-fixed state — miner and matcher provably agree. No live end-to-end auto-fix is claimed.
- Orchestration-level action detection is wired as **shadow only** (log, never execute), behind
  `memory.memory_action_enabled` (default off).
- This measures these roster companies on related-task holdouts; it does not establish general
  memory effectiveness.

## Reproducing

```bash
set -a; source .env; set +a
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_REGION ANTHROPIC_API_KEY GEMINI_API_KEY GOOGLE_API_KEY
export FABRI_ROSTERS_ROOT=/path/to/fabri-rosters       # pinned at 410fbcf
python -m fabri.benchmarks.company_memory_study \
  --dataset benchmarks/datasets/company_memory_experiments.yaml \
  --case support_hq_safe_incident_response \
  --output-dir benchmarks/runs/<name> --replicas 6
# re-score any completed run with the CURRENT rubric (raw vs corrected, no re-spend):
python benchmarks/rescore_runs.py --run-root benchmarks/runs/<name> \
  --case <case_id> --dataset benchmarks/datasets/company_memory_experiments.yaml
```
