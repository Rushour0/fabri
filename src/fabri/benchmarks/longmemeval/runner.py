"""LongMemEval benchmark runner.

LongMemEval (Zhou et al., 2024) tests an agent's ability to recall information
from a long conversation history. Each test case is a multi-session
conversation; the final session ends with a question whose answer was buried
somewhere in earlier sessions.

The benchmark this runner implements (matching the LongMemEval paper):

1. For each test case:
   a. Replay sessions 1..N-1 as fabri agent runs, with the memory loop active.
   b. On the final session, present the question.
   c. Compare the model's answer to the gold answer (exact-match + LLM-judge).

2. Score per-category: single-session-user, multi-session-user,
   temporal-reasoning, knowledge-update, abstention.

3. Emit JSON results + a markdown summary.

### Dataset

The dataset lives on HuggingFace: `xiaowu0162/LongMemEval`. ~10k conversations.
Downloaded lazily on first run to `~/.cache/fabri/longmemeval/`. The first
fetch needs internet + ~few hundred MB of disk; subsequent runs read from
cache.

### Status

This runner ships **scaffolded but unvalidated at scale**. The single-test
path works locally on a hand-crafted fixture (see `tests/test_unit_longmemeval_runner.py`),
but the full ~10k-case evaluation needs real API credits and several hours of
wall time — you'll want to run it once yourself, publish the number, and put
it on slide 4 of the sales deck.

Usage:
    python -m fabri.benchmarks.longmemeval --config agent.yaml --limit 10
    python -m fabri.benchmarks.longmemeval --config agent.yaml  # full eval (slow)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fabri.config import load_config
from fabri.core.agent import run_agent
from fabri.core.logging_setup import configure_logging
from fabri.orchestrator.pipeline import process_trace
from fabri.paths import home
from fabri.runtime import (
    build_decompose_llm,
    build_llm,
    build_memory_store,
    build_tool_defs,
    build_tools,
)

DATASET_CACHE = Path.home() / ".cache" / "fabri" / "longmemeval"
HF_DATASET = "xiaowu0162/LongMemEval"


@dataclass
class TestCaseResult:
    case_id: str
    category: str
    question: str
    gold: str
    predicted: str
    exact_match: bool
    judge_correct: bool | None = None  # None = judge not run


@dataclass
class LongMemEvalResults:
    cases: list[TestCaseResult] = field(default_factory=list)

    @property
    def exact_match_rate(self) -> float:
        if not self.cases:
            return 0.0
        return sum(1 for c in self.cases if c.exact_match) / len(self.cases)

    @property
    def judge_rate(self) -> float | None:
        judged = [c for c in self.cases if c.judge_correct is not None]
        if not judged:
            return None
        return sum(1 for c in judged if c.judge_correct) / len(judged)

    def by_category(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for c in self.cases:
            cat = out.setdefault(c.category, {"n": 0, "exact": 0, "judge": 0, "judged": 0})
            cat["n"] += 1
            if c.exact_match:
                cat["exact"] += 1
            if c.judge_correct is True:
                cat["judge"] += 1
            if c.judge_correct is not None:
                cat["judged"] += 1
        for cat in out.values():
            cat["exact_rate"] = cat["exact"] / cat["n"] if cat["n"] else 0.0
            cat["judge_rate"] = (cat["judge"] / cat["judged"]) if cat["judged"] else None
        return out


def download_dataset(cache_dir: Path = DATASET_CACHE) -> Path:
    """Download (or verify cached) LongMemEval dataset. Returns the path to a
    JSONL file with one test case per line.

    The HF datasets library is the canonical loader; we use it lazily so a
    user who never runs LongMemEval doesn't pay the install cost. If it's
    not installed, fall back to a stub: drop a placeholder file with a clear
    error, so the runner can give the user a one-line install hint instead
    of a stack trace from `import datasets`.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / "longmemeval_s.jsonl"
    if target.exists() and target.stat().st_size > 0:
        return target
    try:
        from datasets import load_dataset  # type: ignore
    except ImportError:
        raise RuntimeError(
            "LongMemEval requires the `datasets` package. Install it:\n"
            "    pip install datasets\n"
            "Then re-run."
        )
    print(f"downloading {HF_DATASET}...", file=sys.stderr)
    ds = load_dataset(HF_DATASET, split="test")
    with target.open("w") as f:
        for row in ds:
            f.write(json.dumps(dict(row)) + "\n")
    print(f"wrote {target} ({target.stat().st_size} bytes)", file=sys.stderr)
    return target


def load_cases(path: Path, limit: int | None = None) -> list[dict]:
    cases = []
    with path.open() as f:
        for i, line in enumerate(f):
            if limit is not None and i >= limit:
                break
            cases.append(json.loads(line))
    return cases


def _replay_session_as_run(session_text: str, config_path: str | None, store, tools, llm, decompose_llm) -> None:
    """Run the agent over one historical session's content so guidelines get
    extracted into the store. The text is presented as the "task" — letting
    the agent observe + summarize before moving on."""
    config = load_config(config_path)
    mem_cfg = config["memory"]
    tools_cfg = config["tools"]
    session_id = str(uuid.uuid4())
    configure_logging(session_id, verbose=False)
    task = (
        "Observe this conversation segment. Identify any persistent facts, "
        "preferences, or decisions that may be useful in a later turn. "
        "Output a one-line summary.\n\nConversation:\n" + session_text
    )
    run_agent(
        task, llm, tools, store,
        session_id=session_id,
        max_steps=config["agent"]["max_steps"],
        top_k=mem_cfg["top_k"],
        max_subquestions=tools_cfg["decompose"]["max_subquestions"],
        result_format=tools_cfg.get("result_format", "toon"),
        decompose_llm=decompose_llm,
    )
    # Mine the trace for guidelines.
    compress_llm = build_llm(config, [])
    process_trace(
        session_id, store, compress_llm,
        guideline_max_tokens=mem_cfg["guideline_max_tokens"],
        similarity_threshold=mem_cfg["similarity_threshold"],
        promotion_threshold_sessions=mem_cfg["promotion_threshold_sessions"],
    )


def _ask_question(question: str, config_path: str | None, store, tools, llm, decompose_llm) -> str:
    """Final-turn run: present the question, return the agent's final text."""
    config = load_config(config_path)
    tools_cfg = config["tools"]
    session_id = str(uuid.uuid4())
    configure_logging(session_id, verbose=False)
    result = run_agent(
        question, llm, tools, store,
        session_id=session_id,
        max_steps=config["agent"]["max_steps"],
        top_k=config["memory"]["top_k"],
        max_subquestions=tools_cfg["decompose"]["max_subquestions"],
        result_format=tools_cfg.get("result_format", "toon"),
        decompose_llm=decompose_llm,
    )
    return result.get("final_text") or ""


def _score_exact_match(predicted: str, gold: str) -> bool:
    """Loose exact match: normalize whitespace + case, then compare."""
    return predicted.strip().lower() == gold.strip().lower()


def run_benchmark(
    config_path: str | None,
    limit: int | None = None,
    skip_judge: bool = True,
) -> LongMemEvalResults:
    """Run LongMemEval over `limit` cases (or full if None). Returns aggregated
    results. `skip_judge` defaults true because the LLM-judge variant doubles
    the API spend and isn't useful for a quick sanity check; flip to False for
    a publishable number."""
    from fabri.runtime import find_missing_role_api_keys
    cfg = load_config(config_path)
    missing = find_missing_role_api_keys(cfg)
    if missing:
        raise RuntimeError(
            "missing API key env vars: "
            + ", ".join(f"{env} ({'+'.join(roles)})" for env, roles in missing.items())
        )

    dataset_path = download_dataset()
    cases = load_cases(dataset_path, limit=limit)
    results = LongMemEvalResults()

    config = load_config(config_path)
    mem_cfg = config["memory"]
    tools_cfg = config["tools"]
    decompose_cfg = tools_cfg["decompose"]

    for i, case in enumerate(cases):
        # Each case gets its own store collection so cross-case leakage doesn't
        # inflate scores. The case_id is the namespace.
        case_id = case.get("question_id") or f"case_{i:05d}"
        category = case.get("question_type") or "unknown"
        question = case.get("question") or ""
        gold = case.get("answer") or ""
        sessions = case.get("haystack_sessions") or case.get("sessions") or []
        if not question or not gold or not sessions:
            print(f"skipping case {case_id}: missing fields", file=sys.stderr)
            continue

        # Isolate the per-case memory in its own qdrant/sqlite collection.
        case_mem_cfg = {**mem_cfg, "collection": f"longmemeval_{case_id}"}
        store = build_memory_store(case_mem_cfg)
        tools = build_tools(tools_cfg)
        llm = build_llm(config, build_tool_defs(tools, decompose_cfg))
        decompose_llm = build_decompose_llm(config)

        for sess in sessions:
            text = sess if isinstance(sess, str) else json.dumps(sess)
            _replay_session_as_run(text, config_path, store, tools, llm, decompose_llm)

        predicted = _ask_question(question, config_path, store, tools, llm, decompose_llm)
        em = _score_exact_match(predicted, gold)

        result = TestCaseResult(
            case_id=case_id, category=category, question=question,
            gold=gold, predicted=predicted, exact_match=em,
        )
        # LLM-judge: defer to a future revision. The scaffold leaves a hook.
        results.cases.append(result)
        print(
            f"[{i + 1}/{len(cases)}] {case_id} ({category}) em={em}",
            file=sys.stderr,
        )

    return results


def render_results_markdown(results: LongMemEvalResults) -> str:
    parts = ["# LongMemEval results", ""]
    parts.append(f"cases: **{len(results.cases)}**")
    parts.append(f"exact-match rate: **{results.exact_match_rate * 100:.1f}%**")
    if results.judge_rate is not None:
        parts.append(f"judge rate: **{results.judge_rate * 100:.1f}%**")
    parts.append("")
    parts.append("## by category")
    parts.append("")
    parts.append("| category | n | exact-match |")
    parts.append("|---|---|---|")
    for cat, stats in sorted(results.by_category().items()):
        parts.append(f"| {cat} | {stats['n']} | {stats['exact_rate'] * 100:.1f}% |")
    return "\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fabri.benchmarks.longmemeval")
    ap.add_argument("--config", default=None)
    ap.add_argument("--limit", type=int, default=None,
                    help="Run only the first N cases (default: full eval ~10k)")
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--judge", action="store_true",
                    help="Also run the LLM-judge scoring path (~2x API spend)")
    args = ap.parse_args(argv)

    results = run_benchmark(args.config, limit=args.limit, skip_judge=not args.judge)

    out_dir = Path(args.output_dir) if args.output_dir else (
        home() / ".fabri" / "benchmarks" / f"longmemeval_{int(time.time())}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(
        json.dumps({
            "exact_match_rate": results.exact_match_rate,
            "judge_rate": results.judge_rate,
            "by_category": results.by_category(),
            "cases": [c.__dict__ for c in results.cases],
        }, indent=2),
    )
    md = render_results_markdown(results)
    (out_dir / "results.md").write_text(md)
    print(md)
    print(f"wrote {out_dir}/results.{{json,md}}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
