# Channel drafts

## r/LocalLLaMA

**Title:** Open-source Python agent engine: trace-derived cross-session memory, with a small reproducible recovery-task result

I am sharing fabri, an Apache-2.0 Python/PyPI agent engine. The premise is that agents should be able to mine their own run traces into retrievable memory, rather than run forever with static prompts.

There is one limited live result so far: on a constructed failure-recovery task using gpt-4o-mini, fresh SQLite memory, and 6 runs, reuse rose from 0% to 67% and steps fell from 5 to 4. The first run recovered from a failed move; later runs skipped it. This is not a general benchmark: it is one task, a cheap model, and a non-canonical configuration; the canonical Sonnet result is pending.

There is also a GitHub Action that runs a PR code-review crew and retains per-repo SQLite memory through CI caching. Interested in criticism of the dedup/retrieval approach and better task shapes to test. Benchmark details: [BENCHMARKS.md](../../BENCHMARKS.md).

## r/MachineLearning

**Title:** A small test of trace-derived cross-session memory for agents, plus an open-source implementation

fabri is an Apache-2.0 Python agent engine that turns agent traces into compact guidelines, deduplicates them, then retrieves them on later sessions. We found and fixed a concrete failure: LLM-paraphrased guidelines did not deduplicate reliably, leaving cross-session reuse at 0%. A deterministic dedup key made the guideline identity stable.

On a constructed failure-recovery task with gpt-4o-mini, fresh SQLite memory, and 6 runs, reuse increased from 0% to 67%, steps went from 5 to 4, and outcome changed from `success_with_recovery` to `success`. The cost comparison was approximately 7.8% lower from the first run to the median of the last 3. This is deliberately not presented as a general result: it is a single task, cheap model, and non-canonical config; canonical Sonnet results are pending. Methodology: [BENCHMARKS.md](../../BENCHMARKS.md).

## X thread

1/4 Most agents keep running prompts that never learn from their own traces. fabri is an open-source Apache-2.0 Python agent engine for cross-session memory: trace → distilled guideline → retrieval on a later run.

2/4 A real bug made reuse stay at 0%: LLM-paraphrased guidelines did not deduplicate. We fixed it with a deterministic dedup key, so the same underlying lesson has a stable identity.

3/4 Limited result, not a sweeping benchmark: on a constructed failure-recovery task with gpt-4o-mini, fresh SQLite memory, 6 runs: reuse 0% → 67%, steps 5 → 4, and `success_with_recovery` → `success`. Cost was about 7.8% lower from run 1 to the median of the last 3.

4/4 Caveats: one task, cheap model, non-canonical config; canonical Sonnet result pending. The repo-native wedge is a GitHub Action: a PR code-review crew posts one summary comment and keeps per-repo SQLite memory cached in CI. Details: BENCHMARKS.md

## Discovery checklist

- [ ] Review submission guidance for `awesome-llm-agents`.
- [ ] Review submission guidance for `awesome-ai-agents`.
- [ ] Prepare a concise entry describing fabri as an Apache-2.0 Python agent engine with trace-derived cross-session memory; include the benchmark caveats.
- [ ] Set GitHub repository topics: `ai-agents`, `agent-memory`, `python`, `github-actions`, `code-review`, `llm`.
