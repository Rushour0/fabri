# LongMemEval port (follow-up)

This directory is a **scaffold for a planned port** of [LongMemEval](https://github.com/xiaowu0162/LongMemEval) — the public long-horizon memory benchmark Mastra, Letta, Mem0, and Zep all report against.

## Why we need this

Without a published number on a canonical benchmark, "fabri has memory" is anecdote. Mastra's headline is 94.87%. Letta and Zep have their own. fabri needs one.

## Porting plan (rough)

1. **Dataset acquisition.** LongMemEval ships ~10k multi-session conversations with question/answer pairs at the end of each session series. Hosted on HuggingFace; needs lazy download on first benchmark run.
2. **Conversation → agent task mapping.** For each test conversation:
   - Replay each session as a fabri run, with the memory loop active, so guidelines accumulate.
   - At the final session, present the question and check whether the agent's answer matches the gold answer.
3. **Scoring.** LongMemEval defines exact-match and LLM-judge variants. Implement both.
4. **Reporting.** Per-category accuracy (single-session-user, multi-session-user, temporal-reasoning, knowledge-update, abstention).

## Why not done in this pass

The dataset download + replay infra is ~2-3 days of work. The `session_delta` benchmark in `../session_delta/` gives an immediate "does the loop work?" number on a single task; LongMemEval is the longer-term public anchor.

## Cross-links

- `decks/internal/code-gaps.md` G1 — the strategic gap this closes.
- `decks/sales/v0.md` slide 4 — where this number goes.
- `decks/technical/v0.md` slide 9 — where the methodology paragraph goes.
