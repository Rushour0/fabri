"""LongMemEval benchmark adapter — scaffold; dataset port TBD.

LongMemEval (Zhou et al., 2024) is the public memory benchmark Mastra, Letta,
Mem0, and Zep all report against. Porting it gives fabri an apples-to-apples
public number — the single highest-leverage marketing artifact for the memory
pitch.

Status: scaffold only. The dataset itself (~hundreds of multi-session
conversations) is not bundled — it's downloaded at first run. See
`benchmarks/longmemeval/README.md` (next to this file) for the porting plan.

Tracking issue: see decks/internal/code-gaps.md G1 (follow-up).
"""
