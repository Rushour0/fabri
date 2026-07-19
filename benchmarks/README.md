# Benchmarks

This directory contains reproducible benchmark inputs and runners for Fabri's
agent memory and company orchestration work.

- `fixtures/recovery/` is the file-recovery task used by the OpenAI replica
  study.
- `fixtures/company_release_readiness/` is a seeded, multi-role release
  readiness company fixture for fresh-company replica runs.
- `runs/` is intentionally ignored. It can contain provisional traces,
  workspace state, and model output. Only validated, reviewed aggregate
  results should be published separately.

Run the recovery study with `fabri benchmark openai-recovery-study` (or the
module entry point in `src/fabri/benchmarks/openai_recovery_study.py`). Use a
fresh state directory for each replica when measuring reproducibility.
