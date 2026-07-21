# Company memory & immutable-generation evolution — consolidated result (2026-07-22)

> **CORRECTION (2026-07-22, after log review):** the "null-to-negative / gen-002 worse"
> reading below was largely a **measurement artifact**. The forbidden-term rubric is a
> substring match that false-positives on negations (it flagged the correct, safe statement
> "no evidence that a fix was deployed"). Negation-corrected, Reliability memory is 8/8 (not
> 5/8) and gen-002 ties gen-001 on quality (3/3 vs 3/3); cost is neutral, not up. The real
> finding is that the **evaluator (substring rubric + self-mining control) is broken**, not
> the agent. See `run-logs-2026-07-22/QUALITATIVE-ANALYSIS.md` and the 118 verbatim per-run
> logs. Treat the raw promotion decisions below as instrument output, not ground truth.


- **Fabri version:** 0.19.0
- **Roster revision:** `fabri-rosters@0815368` (worktree clean)
- **Dataset:** `benchmarks/datasets/company_memory_experiments.yaml`
- **Provider:** OpenAI only (`gpt-5.6-terra` managers/specialists; a few `gpt-4o-mini` crews). AWS/Anthropic/Gemini/Google credentials explicitly unset for every run.
- **Companies:** Support HQ, Reliability Labs, Revenue Ops.
- **Raw material** (prompts, traces, session ids, compiled workspaces) stays under the ignored `benchmarks/runs/full-evolution-20260721/**`; only curated aggregates appear here.

This report covers two questions on one axis — *does fabri's memory make a company
better or cheaper?* — measured two ways: (A) a fresh-compile **memory-vs-control**
study, and (B) **immutable generational accumulation** (train gen-001 → accumulate a
second generation gen-002 → compare under a quality-first promotion gate).

## Headline

The plumbing works; the payoff does not — and at higher memory volume it turns
negative. Transport, verification, and generational accumulation all function
mechanically, but **memory shows no material benefit and, at a second generation, causes
harm**: no significant memory-vs-control effect on the one cleanly-comparable company
(sign-test p=1.0); on Support HQ a second memory generation *raised* cost; and on
Reliability Labs a second generation caused a **rubric regression, a forbidden-term leak,
and more retries**. Neither company promoted its child generation. One company was fully
blocked by a config bug (now fixed), and the control-arm design is confounded and must be
corrected before any causal claim.

## Part A — memory vs control (60 arms, 10 replicas × 2 conditions × 3 companies)

| Company | memory completed | control completed | rubric (mem) | median cost (mem) | paired result |
|---|---:|---:|---:|---:|---|
| Support HQ | 10/10 | 8/10 | 100% | $0.0550 | n=8 pairs, cost Δ mean −$0.0010 (memory cheaper 4/8), **sign-test p=1.0, Wilcoxon p=0.64** |
| Reliability Labs | 8/10 | **0/10** | 62.5% | $0.1220 | n=0 clean pairs (all controls excluded — see contamination) |
| Revenue Ops | **0/10** | **0/10** | — | — | total failure (training truncation) |

- **Support HQ is the only cleanly-comparable case, and memory shows no effect** — 4/8
  replicas cheaper, 4/8 pricier, both conditions 100% rubric. Significance is assessed
  with a paired sign test and Wilcoxon signed-rank; confidence-interval overlap is
  intentionally *not* used.
- Total study spend: **$4.658** (Support HQ $1.126, Reliability Labs $2.183, Revenue Ops $1.349).

### Two structural caveats found while validating the study

1. **Accounting bug (fixed).** `_invalid_run` echoed the agent's optimistic
   `success: true` even for truncated/failed training, so 8 of 60 arm records reported
   `training_success: true` alongside `training_failure_reasons: [training_failed,
   truncation]`. `training_success` is now derived authoritatively (training-phase
   failures force `false`; holdout/transport reasons route to `holdout_failure_reasons`),
   guarded by a `validate_memory_payload` invariant and three regression tests. The
   consolidated aggregator reconciles the already-written buggy records at read time.
2. **Control contamination (open).** A "control" holdout starts with no transported DB,
   but the run still *mines fresh lessons into a new DB and retrieves them within the same
   holdout* — so "empty at start" is not a true no-memory baseline. The study correctly
   flags these (`control_guidelines_retrieved_nonzero` → holdout incomplete), which is
   exactly why Reliability Labs and Revenue Ops have **0 clean control completions**. A
   corrected control mode that disables holdout mining *and* retrieval, verified to emit
   zero memory events, is required before any causal memory-vs-control estimate.

## Part B — immutable generational evolution (gen-001 → gen-002)

Method: train a company once (gen-001), verify + snapshot it; restore gen-001 into a
fresh compile and train again so it accumulates a second generation of memory (gen-002),
verify + snapshot; then run the evolution suite (3 frozen variants, verified-only
retrieval, counterbalanced incumbent/candidate arms) under a quality-first promotion gate
(no rubric regression / forbidden hit / unaccounted cost; median cost ≤ 1.05× incumbent;
≥10% cheaper or ≥25% fewer retries; verified specialist retrieval on ≥2 variants).

**Item-6 verification decision (made this session):** the frozen rubric scores the
*holdout* deliverable and training is a *different scenario*, so a training-mined
`success_pattern` is marked `rubric_verified` when its **training run succeeded
operationally** (`outcome ∈ {success, success_with_recovery}` and the recursive analysis
is `complete` — no truncation/tool-failure/incomplete delegation). Deterministic
tool-failure lessons remain `tool_verified` from mining. This was corrected after the
first live canary revealed that scoring training output against the holdout rubric yields
zero verified entries and an unsatisfiable gate.

| Company | gen-001 → gen-002 verified (specialist) | evolution decision | note |
|---|---|---|---|
| Support HQ | 4 → 7 verified | **no-promote** (`incomplete_pair`) | gen-002 consistently *more* expensive |
| Reliability Labs | 6 (5 spec) → 10 (9 spec) | **no-promote** (quality regression + forbidden hit + more retries) | gen-002 was actively *worse* |
| Revenue Ops | — | **cannot evolve** (no valid incumbent) | baseline truncates; bounded fix verified |

### Support HQ — generational accumulation does not pay off
gen-002 accumulated more verified lessons (7 vs 4) and the verified-specialist-retrieval
gate passed on all 3 variants, but **gen-002 was consistently more expensive than gen-001**
(more memory → more context tokens; e.g. $0.0338 vs $0.0281), with quality tied. Promotion
was withheld — sole failing reason `incomplete_pair` (one gen-001 incumbent arm ran but
`analyze_run` marked it incomplete). Even had it completed, the only "gain" was a retry
reduction, not cost. Evolution spend: $0.385.

### Reliability Labs — a second memory generation made the company *worse*
gen-002 accumulated **10 verified lessons (9 specialist) vs gen-001's 6 (5 specialist)**,
and at evaluation it retrieved more of them (8 verified-specialist ids vs the incumbent's
3) — transport + verification + accumulation all function. Yet the paired suite
(3 variants × 1 replica; reduced from 2 because each recursive 4-crew run exceeds the
harness's background window, so it was completed with a resumable per-arm driver) returned
a decisive **no-promote** with three independent failing reasons:
`quality_regression` (candidate failed the `anchor_release_readiness` rubric where the
incumbent passed), `candidate_forbidden_hit` (the extra memory led the candidate to emit a
forbidden term), and `no_material_efficiency_gain` (cost ratio 1.008 — not cheaper — and
`retry_reduction = −1.0`, i.e. *more* retries than the incumbent). Evolution spend: $0.356.
This is the strongest signal in the study: more accumulated memory did not just fail to
help, it degraded quality and safety.

### Revenue Ops — blocked by a config bug; bounded fix verified
Baseline training truncates every time: the shared `agencies/market-research-brief`
`researcher` and `writer` are pinned to `max_tokens: 768` on `gpt-5.6-terra`; the engine
doubles the retry to 1536 and still overflows → `LLMError` → training FAILED
(`failed_required_delegation:market_research_brief`, `truncation`). So the frozen baseline
**cannot produce a valid incumbent** and cannot evolve. Raising `max_tokens` to **2048**
(researcher + writer) makes training **succeed** (`outcome=success`, no failures, $0.093)
and mine **6 verified lessons**. The agency is shared by **7 companies**, so this must ship
as a Revenue-Ops-scoped override / new generation, never an in-place edit of the frozen
agency. The fix here was applied only to a throwaway compiled copy.

## What to conclude

- **Do not claim generational self-improvement lowers COGS — the current evidence points
  the other way.** On the clean case memory is null vs control (p=1.0); a second memory
  generation *raised* cost on Support HQ and *regressed quality + leaked a forbidden term*
  on Reliability Labs. Neither child generation promoted. The honest story is "memory
  transport/verification/accumulation work mechanically; more memory did not help and, at a
  second generation, hurt." A plausible mechanism worth investigating: unfiltered lesson
  accumulation adds distracting/low-value context that crowds the prompt.
- **Fix the control before any causal claim.** With 0 clean control completions on 2/3
  companies, memory-vs-control is currently unfalsifiable for those companies.
- **Land the Revenue Ops `max_tokens` fix** as a scoped per-company override (768→2048 on
  `market-research-brief` researcher/writer), then re-run its generation cycle.
- **Shipped this session:** the accounting-bug fix + `validate_memory_payload` invariant,
  the consolidated read-time-reconciling aggregator, and item-6 training verification.

## Spend accounting (this session)

| Item | Spend |
|---|---:|
| Memory-vs-control study (60 arms) | $4.658 |
| Support HQ generation cycle (gen-001 + gen-002 + evolution) | $0.453 |
| Reliability Labs (gen-001 $0.051 + gen-002 $0.057 + evolution $0.356) | $0.464 |
| Revenue Ops (baseline $0.059 + max_tokens fix-test $0.093) | $0.152 |
| Aborted/killed attempts (first canary caught the item-6 flaw; Reliability evolution resume cycles) | ~$0.35 |
| **Total** | **~$6.1** |

## Reproducing

```bash
set -a; source .env; set +a
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_REGION ANTHROPIC_API_KEY GEMINI_API_KEY GOOGLE_API_KEY
export FABRI_ROSTERS_ROOT=/path/to/fabri-rosters   # pinned at 0815368
# memory-vs-control study, one company:
python -m fabri.benchmarks.company_memory_study --dataset benchmarks/datasets/company_memory_experiments.yaml \
  --case support_hq_safe_incident_response --output-dir benchmarks/runs/<name> --replicas 10
# consolidated aggregate (reconciles the accounting bug at read time):
python -m fabri.benchmarks.company_memory_report --run-root benchmarks/runs/<name> --output <out>.json
```
