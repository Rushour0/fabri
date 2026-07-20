# Benchmarks

This directory holds reproducible inputs and reviewed outputs for Fabri's agent
memory and company-orchestration evidence. The canonical cross-benchmark status
and methodology live in [`../BENCHMARKS.md`](../BENCHMARKS.md).

## Current company result

| Company / workload | Completion | Conditional rubric | End-to-end | Median cost | Decision |
|---|---:|---:|---:|---:|---|
| Support HQ / safe incident response | 3/3 | 3/3 | 3/3 | $0.020200 | Baseline qualified |

The released gate cost $0.060496. The proposed 256-token artifact floor was
rejected after three preflights as `candidate_noop`, so it received zero model
runs and spent $0. Read the reviewed [narrative result](results/support-hq-setup-qualification-2026-07-20.md)
or [machine-readable aggregate](results/support-hq-setup-qualification-2026-07-20.json).

Reliability Labs and Revenue Ops have dataset cases but no released setup or
memory/control result. Do not infer a score from their presence in the dataset.

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
