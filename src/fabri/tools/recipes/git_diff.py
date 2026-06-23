"""Recipe: `git diff` with output capped at 64KB so the agent can read it
without blowing its context."""
import json
import subprocess
import sys


MAX_OUTPUT = 64 * 1024


def main() -> int:
    args = json.loads(sys.stdin.read())
    cmd = ["git", "diff"]
    if args.get("ref"):
        cmd.append(args["ref"])
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
