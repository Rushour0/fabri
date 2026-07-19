# Company memory experiment dataset

`company_memory_experiments.yaml` is a declarative dataset, not a new Fabri
company runtime. It runs any roster company through the existing commands.

Set the roster checkout once:

```sh
export FABRI_ROSTERS_ROOT=/path/to/fabri-rosters
```

For each case and condition, create an isolated run directory. The memory arm
uses one compiled directory for training followed by holdout; the control arm
uses separate compiled directories so holdout begins with no learned SQLite
memory:

```sh
fabri company compile "$FABRI_ROSTERS_ROOT/companies/support-hq/company.toml" --dest /tmp/support-memory
fabri --config /tmp/support-memory/support-hq/ceo.yaml run '<training_prompt>'
fabri --config /tmp/support-memory/support-hq/ceo.yaml run '<holdout_prompt>'

fabri company compile "$FABRI_ROSTERS_ROOT/companies/support-hq/company.toml" --dest /tmp/support-control-train
fabri --config /tmp/support-control-train/support-hq/ceo.yaml run '<training_prompt>'
fabri company compile "$FABRI_ROSTERS_ROOT/companies/support-hq/company.toml" --dest /tmp/support-control-holdout
fabri --config /tmp/support-control-holdout/support-hq/ceo.yaml run '<holdout_prompt>'
```

Score only the final executive response with the case's required/forbidden
assertions. Record cost, step count, rubric pass/fail, and retrieved-guideline
count. Publish curated aggregates only; the raw workspaces and traces can
contain model output and operational source material.
