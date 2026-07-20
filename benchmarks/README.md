# Benchmarks

This directory holds reproducible inputs and reviewed outputs for Fabri's agent
memory and company-orchestration evidence. The canonical cross-benchmark status
and methodology live in [`../BENCHMARKS.md`](../BENCHMARKS.md).

## Current company result

| Company / workload | Completion | Conditional rubric | End-to-end | Median cost | Decision |
|---|---:|---:|---:|---:|---|
| Support HQ / safe incident response (3-replica gate) | 3/3 | 3/3 | 3/3 | $0.020200 | Release gate cleared |
| Support HQ / safe incident response (10-replica confirmation) | 10/10 | 9/10 | 9/10 | — | Does not clear the 100% bar |
| Reliability Labs / incident release gate | 3/3 | 2/3 | 2/3 | — | Not qualified |
| Revenue Ops / evidence-backed outreach | 2/3 | 0/3 | 0/3 | — | Not qualified |

The 3-replica gate cost $0.060496. The proposed 256-token artifact floor was
rejected after three preflights as `candidate_noop`, so it received zero model
runs and spent $0. Read the reviewed narrative results
([3-replica gate](results/support-hq-setup-qualification-2026-07-20.md),
[10-replica confirmation](results/support-hq-setup-qualification-10replica-2026-07-20.md))
or the machine-readable aggregates
([3-replica gate](results/support-hq-setup-qualification-2026-07-20.json),
[10-replica confirmation](results/support-hq-setup-qualification-10replica-2026-07-20.json)).

At a 10-replica sample, Support HQ's 3/3 release gate did not hold: one run
omitted the follow-up commitment, dropping the rubric to 9/10 (~90%). Reliability
Labs (3/3 completion, 2/3 rubric; over-claimed a deployed fix) and Revenue Ops
(2/3 completion with a truncation failure, 0/3 rubric; over-claimed customer
result and buying intent) were also run live and neither qualified. Read the
reviewed narrative results
([Reliability Labs](results/reliability-labs-setup-qualification-2026-07-20.md),
[Revenue Ops](results/revenue-ops-setup-qualification-2026-07-20.md)) or the
machine-readable aggregates
([Reliability Labs](results/reliability-labs-setup-qualification-2026-07-20.json),
[Revenue Ops](results/revenue-ops-setup-qualification-2026-07-20.json)).

Takeaway: at adequate sample size, none of the three companies clear the 100%
bar. The 3-replica release gate is statistically fragile and should not be
read as a qualification guarantee.

## Artifact map

- [`datasets/company_memory_experiments.yaml`](datasets/company_memory_experiments.yaml)
  defines three dynamic roster experiments using existing `fabri company
  compile` and `fabri run` commands. It contains prompts and assertions, not
  company runtime code.
- [`datasets/README.md`](datasets/README.md) defines setup qualification,
  train/holdout isolation, scoring, and publication rules.
- `fixtures/recovery/` is the file-recovery task used by the OpenAI replica
  study.
- `fixtures/company_release_readiness/` is a seeded multi-role fixture for
  fresh-company replica runs.
- `results/` contains reviewed public aggregates only.
- `runs/` is ignored because provisional traces, workspaces, prompts, session
  IDs, and raw model output can be private or misleading before review.

## Runners

```sh
# Dynamic roster setup qualification
python -m fabri.benchmarks.company_setup_probe --help

# Recovery study
fabri benchmark openai-recovery-study --help
```

Use a fresh compile and state directory per replica. The company setup probe
does this automatically.

## Tests

The benchmark runners have dedicated, offline pytest coverage:

```sh
pytest -q tests/test_company_setup_probe.py tests/test_openai_recovery_study.py
```

The setup tests cover roster-source resolution, candidate allowlisting,
recursive preflight and role classification, bounded token-floor changes,
deterministic required/forbidden scoring, isolated state roots, recursive cost
accounting, child outcome handling, private/public artifact separation, and
profile selection. The recovery-runner tests cover training/holdout task
selection, rubric completeness, recovery evidence, and cost summaries. Live
model calls are experiments, not CI tests.
