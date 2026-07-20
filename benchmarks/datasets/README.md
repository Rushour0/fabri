# Company memory experiment dataset

`company_memory_experiments.yaml` is a declarative dataset, not a new Fabri
company runtime. It runs any roster company through the existing commands.

Set the roster checkout once:

```sh
export FABRI_ROSTERS_ROOT=/path/to/fabri-rosters
```

## Qualify the company setup first

Before spending on the memory/control experiment, run the bounded setup probe:

```sh
python -m fabri.benchmarks.company_setup_probe \
  --dataset benchmarks/datasets/company_memory_experiments.yaml \
  --case support_hq_safe_incident_response \
  --output-dir benchmarks/runs/support-hq-setup-probe
```

The probe recursively inspects the compiled company and compares the baseline
with the case's allowlisted configuration candidates. A candidate qualifies
only when every scheduled replica completes its required delegation tree,
passes the deterministic output rubric, and stays inside the company cost
limit. Incomplete runs receive no rubric verdict. Public aggregate results and
the recommended declarative profile are written at the output root; prompts,
traces, session ids, and raw model output stay under `private-attempts/`.

The Support HQ probe changes only a low delegated artifact-producing `main`
role's token ceiling. It does not change narration, prompts, tools, security
policy, roster source, or Fabri runtime behavior. A candidate that changes no
effective delegated role is recorded as `candidate_noop` and is not run, so an
obsolete hypothesis does not spend model credits.

Only after a profile qualifies should it enter the full memory/control study
below.

In `expected.required`, a string is an exact case-insensitive phrase. A nested
list is a deterministic concept group where any phrase satisfies the concept;
for example, `[rollback, rolled back]`. Define these alternatives before the
qualification run rather than adding them retroactively to rescue an output.

For each case and condition, create isolated training and holdout directories.
The memory arm copies only its learned SQLite database into a fresh holdout
compile. This preserves learned guidelines without leaking files changed in the
training workspace. The control arm also uses a fresh holdout compile, but does
not receive the trained database:

```sh
fabri company compile "$FABRI_ROSTERS_ROOT/companies/support-hq/company.toml" --dest /tmp/support-memory-train
FABRI_HOME=/tmp/support-memory-train-state \
  fabri --config /tmp/support-memory-train/support-hq/ceo.yaml run '<training_prompt>'
fabri company compile "$FABRI_ROSTERS_ROOT/companies/support-hq/company.toml" --dest /tmp/support-memory-holdout
mkdir -p /tmp/support-memory-holdout/support-hq/.fabri
cp /tmp/support-memory-train/support-hq/.fabri/support_hq.db \
  /tmp/support-memory-holdout/support-hq/.fabri/support_hq.db
FABRI_HOME=/tmp/support-memory-holdout-state \
  fabri --config /tmp/support-memory-holdout/support-hq/ceo.yaml run '<holdout_prompt>'

fabri company compile "$FABRI_ROSTERS_ROOT/companies/support-hq/company.toml" --dest /tmp/support-control-train
FABRI_HOME=/tmp/support-control-train-state \
  fabri --config /tmp/support-control-train/support-hq/ceo.yaml run '<training_prompt>'
fabri company compile "$FABRI_ROSTERS_ROOT/companies/support-hq/company.toml" --dest /tmp/support-control-holdout
FABRI_HOME=/tmp/support-control-holdout-state \
  fabri --config /tmp/support-control-holdout/support-hq/ceo.yaml run '<holdout_prompt>'
```

Score only the final executive response with the case's required/forbidden
assertions after the full required delegation tree completes. Record scheduled
count, completion rate, conditional rubric pass rate, end-to-end pass rate,
cost, step count, and retrieved-guideline count. A failed training run
invalidates its train/holdout pair because failure lessons are still mined.
Publish curated aggregates only; the raw workspaces and traces can contain
model output and operational source material.
