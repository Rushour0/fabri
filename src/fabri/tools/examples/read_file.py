"""Sandboxed file read with optional windowing + structural outline.

Whole-file reads dominate input-token spend on a file-gen workload; an agent
that wants to edit one function shouldn't have to ingest the rest of the
module. The window args (`line_start`/`line_end`, 1-indexed inclusive) return
a slice. `outline_only=true` returns the top-level structure -- def/class/
heading/CONSTANT lines plus their line numbers -- so the agent can locate the
right window in one cheap call (SWE-agent ACI pattern).
"""
import json
import os
import re
import sys
from pathlib import Path

SANDBOX_ROOT_ENV = "FABRI_SANDBOX_ROOT"

# P3 hardening: cap whole-file reads so a single tool call can't blow up the
# agent's context by ingesting a 100MB log file. Windowed and outline reads
# are unaffected (they're already bounded by line slicing). The cap is
# generous (1MB) for normal source files but stops a runaway. The agent gets
# a clear error pointing it at outline_only / line_start / line_end.
READ_FILE_MAX_BYTES = 1_000_000

# def/class for Python/JS/Go-ish, markdown/ini headings, ALL_CAPS = ... constants.
# Deliberately language-agnostic and shallow -- the goal is a coarse map, not a
# parse tree. Extend per-language if it ever matters.
_OUTLINE_RE = re.compile(
    r"^("
    r"\s*(?:def|class|async\s+def|fn|func|function|interface|type|impl|module|package)\b.*"
    r"|#+\s+.+"
    r"|\[[^\]]+\]\s*$"
    r"|[A-Z][A-Z0-9_]{2,}\s*[:=].*"
    r")$"
)


def _err(msg: str) -> str:
    return json.dumps({"error": msg})


def _outline(lines: list[str]) -> list[dict]:
    return [
        {"line": i + 1, "text": ln.rstrip("\n")}
        for i, ln in enumerate(lines)
        if _OUTLINE_RE.match(ln)
    ]


def main() -> int:
    args = json.loads(sys.stdin.read())
    root_env = os.environ.get(SANDBOX_ROOT_ENV)
    if not root_env:
        print(_err(f"{SANDBOX_ROOT_ENV} is not set; refusing to run unsandboxed"))
        return 1
    root = Path(root_env).resolve()
    target = (root / args["path"]).resolve()

    if not target.is_relative_to(root):
        print(_err(f"path escapes sandbox root: {args['path']}"))
        return 1

    if not target.is_file():
        print(_err(f"no such file: {args['path']}"))
        return 1

    # P3: refuse to slurp a huge file. If the agent really wants the whole
    # thing, it can window through it; this is a back-pressure signal, not a
    # silent truncation.
    size = target.stat().st_size
    if size > READ_FILE_MAX_BYTES:
        print(_err(
            f"file is {size} bytes; read_file caps at {READ_FILE_MAX_BYTES}. "
            f"Use outline_only=true to scan structure, or line_start/line_end to window."
        ))
        return 1
    raw = target.read_text()
    lines = raw.splitlines(keepends=True)
    total = len(lines)
    rel_path = str(target.relative_to(root))

    outline_only = bool(args.get("outline_only", False))
    line_start = args.get("line_start")
    line_end = args.get("line_end")

    if outline_only:
        print(json.dumps({
            "path": rel_path,
            "total_lines": total,
            "outline": _outline(lines),
        }))
        return 0

    if line_start is None and line_end is None:
        # Back-compat: behave exactly like the pre-windowing tool.
        print(json.dumps({"path": rel_path, "content": raw}))
        return 0

    start = 1 if line_start is None else int(line_start)
    end = total if line_end is None else int(line_end)
    if start < 1:
        print(_err(f"line_start must be >= 1, got {start}"))
        return 1
    if end < start:
        print(_err(f"line_end ({end}) < line_start ({start})"))
        return 1
    # Clamp to file bounds; asking for [1, 1_000_000] on a 50-line file is a
    # reasonable "read to EOF" gesture, not an error.
    end = min(end, total)
    if start > total:
        content = ""
    else:
        content = "".join(lines[start - 1:end])

    print(json.dumps({
        "path": rel_path,
        "content": content,
        "start_line": start,
        "end_line": end,
        "total_lines": total,
        "truncated": end < total or start > 1,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
