"""Run a bash command inside the sandbox cwd, with timeout + output truncation.
This is NOT a security sandbox (no namespace/container isolation) -- it only
prevents accidental cwd-based footguns and runaway output. Use with eyes open."""
import json
import os
import subprocess
import sys
from pathlib import Path

SANDBOX_ROOT_ENV = "AGENT_SANDBOX_ROOT"
MAX_OUTPUT_BYTES = 50_000
DEFAULT_TIMEOUT = 30


def _truncate(s: str) -> tuple[str, bool]:
    b = s.encode()
    if len(b) <= MAX_OUTPUT_BYTES:
        return s, False
    return b[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"), True


def main() -> int:
    args = json.loads(sys.stdin.read())
    root_env = os.environ.get(SANDBOX_ROOT_ENV)
    if not root_env:
        print(json.dumps({"error": f"{SANDBOX_ROOT_ENV} is not set; refusing to run unsandboxed"}))
        return 1
    root = Path(root_env).resolve()
    cmd = args["command"]
    timeout = args.get("timeout", DEFAULT_TIMEOUT)

    try:
        proc = subprocess.run(
            ["bash", "-c", cmd], cwd=root, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(json.dumps({"error": f"timeout after {timeout}s", "command": cmd}))
        return 1
    except OSError as e:
        print(json.dumps({"error": str(e), "command": cmd}))
        return 1

    stdout, stdout_trunc = _truncate(proc.stdout)
    stderr, stderr_trunc = _truncate(proc.stderr)
    payload = {
        "exit_code": proc.returncode, "stdout": stdout, "stderr": stderr,
        "stdout_truncated": stdout_trunc, "stderr_truncated": stderr_trunc,
    }
    print(json.dumps(payload))
    return 0 if proc.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
