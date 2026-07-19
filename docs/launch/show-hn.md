# Show HN draft

## Title options

1. Show HN: fabri – agents that learn from their own run traces (open source)
2. Show HN: fabri – an open-source agent engine for prompts that learn from runs
3. Show HN: fabri – cross-session memory for agents whose prompts never learn

## First comment

fabri is an Apache-2.0 Python/PyPI agent engine for prompts that never learn from their own runs.

After an agent runs, fabri analyzes its trace, distills a short guideline from a failure or useful pattern, deduplicates it, and makes it retrievable on a later session. It is a library you embed, not a control-plane dashboard.

One modest result: on a constructed failure-recovery task with gpt-4o-mini, fresh SQLite memory, and 6 runs, guideline reuse rose from 0% to 67%; steps went from 5 to 4; and the outcome moved from `success_with_recovery` to `success`. The first run recovered from a failed read; later runs skipped it. Cost from the first run to the median of the last 3 was about 7.8% lower. Caveats: one constructed task, cheap model, non-canonical config; canonical Sonnet is pending. Details: [BENCHMARKS.md](../../BENCHMARKS.md).

The repo-native wedge is a GitHub Action that runs a code-review crew on PRs, posts one summary comment, and uses per-repo SQLite memory cached in CI.

“Isn't this just a retry loop?” No: retries are within one run. This is cross-session memory distilled from traces and retrieved later. The 0% to 67% reuse and 5-to-4 step change are narrow evidence—not proof it improves every workload or gets cheaper every run.
