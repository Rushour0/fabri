"""Recipe: a whitelisted shell-command runner. Refuses anything that isn't
explicitly in ALLOWED_BINS or that contains shell-control characters."""
import json
import shlex
import subprocess
import sys

# `find` is intentionally NOT allowed: `-exec`/`-execdir`/`-delete` make it an
# arbitrary-exec / arbitrary-delete primitive that an allow-list-of-binaries
# can't contain. Use grep/ls for read-only discovery instead.
ALLOWED_BINS = {
    "git", "ls", "cat", "head", "tail", "grep", "wc", "diff",
    "pwd", "echo", "stat", "file", "which",
}
DENY_TOKENS = {">", ">>", "|", "&&", "||", ";", "`", "$("}
# Even within an allowed binary, these args run a command, delete files, or
# write to an arbitrary path -- reject them regardless of binary so a future
# allow-list addition can't silently reintroduce the escape.
DENY_ARGS = {
    "-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprintf", "-fprint",
    "--output", "--upload-pack", "--receive-pack",
}
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
    # `git -c <cfg>` can alias a command to a shell (`-c alias.x=!sh`); refuse
    # the config flag outright. Combined with DENY_ARGS this closes the known
    # allow-listed-binary exec/file-write escapes.
    bad = [p for p in parts[1:] if p in DENY_ARGS or p.startswith(tuple(DENY_ARGS))]
    if parts[0] == "git" and ("-c" in parts[1:] or any(p.startswith("-c") and p != "-c" for p in parts[1:])):
        bad.append("-c")
    if bad:
        print(json.dumps({"error": f"refused: disallowed argument(s) {sorted(set(bad))}"}))
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
