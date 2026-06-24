"""Recipe: `git diff` with output capped at 64KB so the agent can read it
without blowing its context."""
import json
import re
import subprocess
import sys


MAX_OUTPUT = 64 * 1024
# A `ref` is a commit-ish, not an option. Without this, a model-supplied
# `ref="--output=/path"` reaches `git diff` as an option and writes the diff to
# an arbitrary file. Reject anything that isn't ref-shaped (notably a leading
# dash). `path` is already safely placed after `--`.
_REF_RE = re.compile(r"^[A-Za-z0-9._/~^@{}-]+$")


def main() -> int:
    args = json.loads(sys.stdin.read())
    cmd = ["git", "diff"]
    ref = args.get("ref")
    if ref:
        if ref.startswith("-") or not _REF_RE.match(ref):
            print(json.dumps({"error": f"refused: invalid ref {ref!r}"}))
            return 1
        cmd.append(ref)
    if args.get("path"):
        cmd += ["--", args["path"]]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        print(json.dumps({"error": "git diff timed out"}))
        return 1
    out = proc.stdout
    truncated = len(out) > MAX_OUTPUT
    if truncated:
        out = out[:MAX_OUTPUT]
    print(json.dumps({
        "cmd": " ".join(cmd),
        "exit": proc.returncode,
        "stdout": out,
        "stderr": proc.stderr[-1000:],
        "truncated": truncated,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
