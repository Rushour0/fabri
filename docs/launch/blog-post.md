# An AI code-review crew that gets better at *your* repo each run — how the memory loop works

Most agent systems start each task with a prompt that is effectively static. You can tune it, add examples, and bolt on retries, but the system does not normally turn its own past behavior into reusable operating knowledge. That is the problem fabri is trying to address.

fabri is an Apache-2.0 Python library and agent engine, installable from PyPI and embeddable in a product. Its core idea is small: when an agent makes a recoverable mistake, the next session should have a chance not to make the same mistake again. This is not a claim that every task improves, or that an agent gets cheaper on every run. It is a mechanism for tasks with recurring patterns to accumulate context from what actually happened.

## From a trace to the next run

The loop is trace → distilled guideline → retrieval on a later run.

Each run produces a trace of the agent's decisions and tool results. fabri analyzes that trace and compresses a failure or useful pattern into a short guideline. The memory layer deduplicates and stores the guideline. On a subsequent task, relevant stored guidelines are retrieved and added to the agent's context before it acts.

Conceptually, the stored lesson is not a transcript or another retry. It is closer to a compact instruction derived from the trace: a reminder that a particular first move did not work and what recovered from it. That distinction matters. A retry loop repeats work inside one session; this loop makes a lesson available across sessions.

For a code-review workflow, the useful unit is a repository. A review crew can encounter a recurring convention, a recurring failure mode, or a more reliable way to inspect a change. Per-repository memory gives a later PR review a chance to retrieve that lesson instead of treating every PR as the crew's first day on the repo.

## The bug that made learning look absent

This loop had a real failure mode. Cross-session guideline reuse was stuck at 0%.

The cause was deceptively ordinary: guidelines were written as LLM paraphrases. Two paraphrases of the same lesson were not reliably recognized as duplicates. Without stable deduplication, the memory did not form a reusable identity for the recurring lesson, so cross-session reuse did not show up as intended.

The fix was a deterministic `dedup_key`. The key is stable where prose is not, allowing the memory layer to identify the same underlying guideline despite paraphrase drift. This is one reason the result below should be read as an end-to-end check of a particular implementation, not as a broad score for agent memory.

## The modest benchmark result

The live check used a constructed failure-recovery task, gpt-4o-mini, fresh SQLite memory, and six runs. The first run tried a failed read, then recovered. In later runs the agent learned to skip that failed first move.

| Measure | First run | Later runs / comparison |
|---|---:|---:|
| Guideline reuse | 0% | 67% |
| Steps | 5 | 4 |
| Outcome | `success_with_recovery` | `success` |
| Cost | first run | approximately 7.8% lower at the median of the last 3 runs |

This is evidence for a narrow statement: on this task, the cross-session memory loop retrieved a lesson and avoided a previously failed action. It is not evidence that fabri lowers cost on arbitrary tasks. The task was constructed, the model was inexpensive, the configuration was non-canonical, and the canonical Sonnet number is still pending. There is also no completed broad memory benchmark result to use as a general comparison. The full methodology and caveats are in [BENCHMARKS.md](../../BENCHMARKS.md).

## The repo-native entry point

The practical wedge is a fabri GitHub Action. It runs a code-review crew on a pull request and posts one summary comment. Its memory is per-repository SQLite memory, cached in CI, so the crew can carry forward lessons from earlier runs on that repository.

That setup is intentionally narrower than a hosted agent platform. fabri is an engine/library: install it with `pip`, embed it in your own application or workflow, and keep the control plane in your own hands. The Action is a concrete integration point, not a claim that a dashboard is required to use the system.

## What remains unproven

The main question is generalization. A memory loop can only help when there is something stable to learn and retrieve. Single-step lookups may have nothing to retain. Different task types may expose different failure patterns. A single constructed recovery task does not answer those questions.

The next useful evidence is not a louder headline; it is more varied, reproducible runs, including the pending canonical Sonnet configuration. The benchmark documentation states the configuration and methodology so a result can be challenged or rerun. Until those results exist, the responsible claim is the small one: fabri provides cross-session trace-derived memory, and on one deliberately limited task it helped an agent stop repeating its own failed first move.
