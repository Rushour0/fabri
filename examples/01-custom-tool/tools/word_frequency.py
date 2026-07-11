"""Count the most frequent words in a sandboxed text file.

A worked example of a well-shaped fabri tool. The design choices here map 1:1
to README §"Designing tools for low token cost":

- **Return the minimum.** We return the top-N words, not the whole tokenized
  document. The agent asked "what are the common words", so that's all that
  rides in context on every subsequent step.
- **TOON-friendly shape.** `words` is a flat array of {word, count} records
  with identical keys — that encodes far smaller than a nested object when the
  agent's config uses `result_format: toon`.
- **Path-jailed.** Like every file tool, it reads its jail root from
  `FABRI_SANDBOX_ROOT` and refuses any path that escapes it. The runner sets
  that env var from `tools.sandbox_root`; the tool enforces it.
- **Fails loudly.** Missing file / oversized file / bad args return a clear
  `{"error": ...}` and a non-zero exit, so the agent can correct rather than
  silently getting nothing.

Contract (shared by every fabri tool): read one JSON object from stdin as the
args, print exactly one JSON object to stdout as the result, exit 0 on success
and non-zero on error.
"""
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

SANDBOX_ROOT_ENV = "FABRI_SANDBOX_ROOT"

# Back-pressure, not silent truncation: a single tool call shouldn't ingest a
# 100MB log. The agent gets an actionable error if it points us at one.
MAX_BYTES = 2_000_000

_WORD_RE = re.compile(r"[A-Za-z']+")


def _err(msg: str) -> str:
    return json.dumps({"error": msg})


def main() -> int:
    args = json.loads(sys.stdin.read())

    root_env = os.environ.get(SANDBOX_ROOT_ENV)
    if not root_env:
        print(_err(f"{SANDBOX_ROOT_ENV} is not set; refusing to run unsandboxed"))
        return 1
    root = Path(root_env).resolve()

    rel = args.get("path")
    if not rel:
        print(_err("missing required arg: path"))
        return 1

    target = (root / rel).resolve()
    if not target.is_relative_to(root):
        print(_err(f"path escapes sandbox root: {rel}"))
        return 1
    if not target.is_file():
        print(_err(f"no such file: {rel}"))
        return 1

    size = target.stat().st_size
    if size > MAX_BYTES:
        print(_err(f"file is {size} bytes; word_frequency caps at {MAX_BYTES}"))
        return 1

    top_n = int(args.get("top_n", 20))
    min_length = int(args.get("min_length", 1))
    if top_n < 1:
        print(_err(f"top_n must be >= 1, got {top_n}"))
        return 1

    text = target.read_text(errors="replace").lower()
    counts = Counter(
        w for w in _WORD_RE.findall(text) if len(w) >= min_length
    )

    # Flat array of uniform records — the TOON-friendly shape. Return only the
    # slice the agent asked for, plus the totals it needs to reason about it.
    words = [{"word": w, "count": c} for w, c in counts.most_common(top_n)]
    print(json.dumps({
        "path": str(target.relative_to(root)),
        "total_words": sum(counts.values()),
        "unique_words": len(counts),
        "words": words,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
