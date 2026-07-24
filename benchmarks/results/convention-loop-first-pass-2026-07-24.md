# Convention loop: first end-to-end pass + 6-replica reliability check (2026-07-24)

Eleven pre-registered smoke rounds and one 6-replica run of
`support_hq_safe_incident_response` against the rewritten (headroom) holdout, run across
convention-mining v1 (#83) and its five follow-up fixes (#84–#89). Model under test:
gpt-5.6-terra. Total spend this date ≈ $2.9.

## Headline results

**2-replica smoke (round 11, post-#89): memory passes 2/2, control fails 2/2.** First
time the full loop — engine-side typed extraction → exact-hash operator approval →
scope-aware placement → atomic retrieval → branch application — closed live. Memory arms
produced all four protocol fields correctly with zero forbidden leaks; control arms
scored 0/4 with leaked identifiers, as in every prior round.

**6-replica confirmation: memory 2/6, control 0/6.** The first study in this series
where the memory arm beats control at all — and it is not yet reliable. Per-replica:

| replica | convention placed at company scope | outcome |
|---|---|---|
| 1 | yes (2 granted) | holdout **timed out** (infra, excluded from rubric) |
| 2 | yes (1 granted, 2 skipped) | fail — model made no branch selection; engine had nothing to copy |
| 3 | yes (2 granted) | **pass**, 4/4 fields, no leaks |
| 4 | yes (3 granted) | **pass**, 4/4 fields, no leaks |
| 5 | no (0 granted, 3 skipped inadmissible) | fail — extraction produced no admissible candidate |
| 6 | yes (2 granted) | fail — same as replica 2 |

Costs: mean memory arm $0.0686 vs control $0.0775 per completed run.

## Claim boundary

- 2/6 vs 0/6 is **not statistically resolved** (Fisher exact p ≈ 0.45). The defensible
  claim is: the mechanism now works end to end, demonstrated twice under pre-registered
  conditions, and control cannot do the task (0 for its last 10 completed arms across
  rounds).
- The binding constraints have moved from possibility to **reliability**, localized to
  two stages: (a) extraction admissibility per replica (~2 of 6 replicas produced no
  usable company-scope record), and (b) selection declaration — when the model answers
  without a `SELECTED_BRANCH:` line, the deterministic engine copy has nothing to act
  on (2 of 4 placed-and-completed arms).
- Nothing here is a general memory-effectiveness claim; one company, one holdout pair.

## What changed to get here (per-round diagnosis trail)

#84 absent-vs-empty effect-class defaults + admissible-only approvals · #85 declared-
protocol extraction + retrieval-armed validation + evolving profile · #86 effect-class
normalization + skip provenance · #87 company-scope placement + marked-line selection
channel · #88 engine-side application (model selects, engine copies) · #89 effect_class
as an engine constant. Full per-round notes: `headroom-smoke-2026-07-24.md` and the
PR descriptions.

## Reproducing

```bash
set -a; source .env; set +a
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_REGION ANTHROPIC_API_KEY GEMINI_API_KEY GOOGLE_API_KEY
export FABRI_ROSTERS_ROOT=/path/to/fabri-rosters
uv run python -m fabri.benchmarks.company_memory_study \
  --dataset benchmarks/datasets/company_memory_experiments.yaml \
  --case support_hq_safe_incident_response \
  --output-dir benchmarks/runs/convention-full-20260724 --replicas 6 --guideline-max-tokens 120
```
