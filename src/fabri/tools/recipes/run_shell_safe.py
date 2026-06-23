"""Recipe: a whitelisted shell-command runner. Refuses anything that isn't
explicitly in ALLOWED_BINS or that contains shell-control characters."""
import json
import shlex
import subprocess
import sys

ALLOWED_BINS = {
    "git", "ls", "cat", "head", "tail", "grep", "find", "wc", "diff",
    "pwd", "echo", "stat", "file", "which",
}
DENY_TOKENS = {">", ">>", "|", "&&", "||", ";", "`", "$("}
MAX_OUTPUT = 8 * 1024


def main() -> int:
    args = json.loads(sys.stdin.read())
    cmd = args["cmd"]
    if any(tok in cmd for tok in DENY_TOKENS):
        print(json.dumps({"error": "refused: shell-control character in command"}))
        return 1
    try:
        parts = shlex.split(cmd)
    except ValueError as e:
        print(json.dumps({"error": f"parse: {e}"}))
        return 1
    if not parts or parts[0] not in ALLOWED_BINS:
        print(json.dumps({
            "error": f"refused: binary not in allow-list. "
                     f"allowed={sorted(ALLOWED_BINS)}"
        }))
        return 1
    try:
        proc = subprocess.run(parts, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        print(json.dumps({"error": "timeout"}))
        return 1
    print(json.dumps({
        "cmd": cmd,
        "exit": proc.returncode,
        "stdout": proc.stdout[-MAX_OUTPUT:],
        "stderr": proc.stderr[-1000:],
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
