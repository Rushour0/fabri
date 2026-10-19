"""Measure how often an agent's answers carry AI-writing tells, with and
without fabri.style.PLAIN_WRITING_RULES in the system prompt.

    python benchmarks/style/slop_eval.py --env-file ../.env --reps 2 --out results/style.json

Each prompt is sent once per arm per rep to the same model. Answers are
scored with fabri.style.slop_report; the summary is tells per answer and per
100 words, plus answer length, so a drop in tells is not just shorter text.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fabri import runtime
from fabri.style import PLAIN_WRITING_RULES, slop_report

ROLE = (
    "You are the assistant inside a sports league management app. You help league "
    "admins with registrations, schedules, payments, canvases (dashboard documents) "
    "and tasks. Answer the admin's message."
)

PROMPTS = [
    "How do I set up early bird pricing for the fall soccer season?",
    "Why are three families showing as unpaid when they say they paid?",
    "Can you explain what a canvas is and when I should use one?",
    "Summarize what I should do before opening registration next week.",
    "What's the difference between a team and a division here?",
    "Write a short note I can send to coaches about the new field schedule.",
    "Our refund policy is confusing parents. How should we word it?",
    "Give me ideas for tracking volunteer hours across teams.",
    "Is it a good idea to let parents pick their kid's team?",
    "What metrics should I put on a dashboard for the board meeting?",
    "How do waitlists work when a division fills up?",
    "Draft a reminder for parents who haven't signed the waiver.",
]


def _load_env(path: str | None) -> None:
    if not path:
        return
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.removeprefix("export ").split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--env-file")
    p.add_argument("--model", default="gpt-5-mini")
    p.add_argument("--api-key-env", default="PLAYBOOK_OPENAI_API_KEY")
    p.add_argument("--reps", type=int, default=2)
    p.add_argument("--out", required=True)
    args = p.parse_args()
    _load_env(args.env_file)
    if not os.environ.get(args.api_key_env):
        print(f"{args.api_key_env} is not set", file=sys.stderr)
        return 2

    cfg = {"provider": "openai", "model": args.model, "api_key_env": args.api_key_env, "max_tokens": 2048}
    arms = {"baseline": ROLE, "plain_rules": PLAIN_WRITING_RULES + "\n" + ROLE}
    jobs = [(arm, i, rep) for arm in arms for i in range(len(PROMPTS)) for rep in range(args.reps)]

    def run(job):
        arm, i, rep = job
        backend = runtime._instantiate(cfg, [])
        resp = backend.step(arms[arm], [{"role": "user", "content": PROMPTS[i]}])
        text = resp.final_text or ""
        r = slop_report(text)
        return {"arm": arm, "prompt": i, "rep": rep, "words": r.words, "tells": r.total,
                "counts": r.counts, "examples": r.examples, "text": text}

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(run, jobs))

    summary = {}
    for arm in arms:
        arm_rows = [r for r in rows if r["arm"] == arm]
        words = sum(r["words"] for r in arm_rows)
        tells = sum(r["tells"] for r in arm_rows)
        by_kind: dict[str, int] = {}
        for r in arm_rows:
            for k, v in r["counts"].items():
                by_kind[k] = by_kind.get(k, 0) + v
        summary[arm] = {
            "answers": len(arm_rows),
            "tells_per_answer": round(tells / len(arm_rows), 2),
            "tells_per_100_words": round(100 * tells / words, 2) if words else 0.0,
            "answers_with_zero_tells": sum(1 for r in arm_rows if r["tells"] == 0),
            "median_words": statistics.median(r["words"] for r in arm_rows),
            "by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1])),
        }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"model": args.model, "summary": summary, "rows": rows}, indent=2))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
