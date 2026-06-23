"""LongMemEval benchmark adapter.

LongMemEval (Zhou et al., 2024) is the public memory benchmark Mastra, Letta,
Mem0, and Zep all report against. Porting it gives fabri an apples-to-apples
public number — the highest-leverage marketing artifact for the memory pitch.

Status (v0.7.2): runner implemented end-to-end with exact-match scoring;
dataset downloader uses HuggingFace `datasets`. The LLM-judge scoring path is
behind `--judge`. Validated end-to-end on a tiny fixture; the published number
needs a real ~10k-case run with API credits.

CLI:
    python -m fabri.benchmarks.longmemeval --config agent.yaml --limit 10
"""
from fabri.benchmarks.longmemeval.runner import (
    LongMemEvalResults,
    TestCaseResult,
    run_benchmark,
)

__all__ = ["LongMemEvalResults", "TestCaseResult", "run_benchmark"]
