"""A1 -- block on a clarifying question routed to the host.

Two transports:

* Unix-socket (production / service host): the runner was launched with
  `--ask-user-socket=<path>`, which sets `FABRI_ASK_USER_SOCKET` in the
  subprocess env. The host already has a listener on that socket. We send
  one JSON line `{"kind": "ask_user", "question_id, question, options?,
  default?}` and block on a single JSON-line reply
  `{"question_id", "answer", "selected_option?"}`. Question IDs make it
  safe for the host to interleave replies from multiple concurrent
  sub-agents -- A1's spec calls out this exact case.
* stdin (CLI dev): no socket env var. We print the question to stderr (so
  it doesn't pollute the tool's stdout-JSON contract) and read one line of
  answer from stdin.

Trace event: the runner already logs every tool_call (name + args +
result), so the question and answer end up in the JSONL trace without this
tool having to write its own event. Keeps the trust boundary on the runner.
"""
import json
import os
import socket
import sys
import uuid

SOCKET_ENV = "FABRI_ASK_USER_SOCKET"


def _ask_via_socket(socket_path: str, payload: dict) -> dict:
    """One question, one reply. We use SOCK_STREAM + a single newline as the
    record separator -- the same line-delimited JSON the runner's trace uses,
    so the host's listener code can reuse the same parser."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(socket_path)
    try:
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        # Use a file-like wrapper to read one line; the host writes one
        # newline-terminated JSON object back.
        f = sock.makefile("r", encoding="utf-8")
        line = f.readline()
        if not line:
            raise RuntimeError("ask-user socket closed before reply")
        return json.loads(line)
    finally:
        sock.close()


def _ask_via_stdin(payload: dict) -> dict:
    """No host wired up; ask the user inline. Question goes to stderr so we
    don't smear stdout (the tool contract is one JSON object on stdout)."""
    sys.stderr.write(payload["question"] + "\n")
    if "options" in payload and payload["options"]:
        for i, opt in enumerate(payload["options"], start=1):
            sys.stderr.write(f"  {i}) {opt}\n")
    if payload.get("default"):
        sys.stderr.write(f"[default: {payload['default']}] ")
    sys.stderr.flush()
    answer = sys.stdin.readline().strip()
    if not answer and payload.get("default"):
        answer = payload["default"]
    return {"question_id": payload["question_id"], "answer": answer}


def main() -> int:
    raw = sys.stdin.read()
    try:
        args = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"malformed JSON on stdin: {e}"}))
        return 1
    if "question" not in args:
        print(json.dumps({"error": "missing required field: question"}))
        return 1

    question_id = str(uuid.uuid4())
    payload = {
        "kind": "ask_user",
        "question_id": question_id,
        "question": args["question"],
    }
    if "options" in args:
        payload["options"] = args["options"]
    if "default" in args:
        payload["default"] = args["default"]

    socket_path = os.environ.get(SOCKET_ENV)
    try:
        if socket_path:
            # Re-use the stdin transport path if the socket can't be opened
            # rather than failing the whole tool call; a flaky listener
            # shouldn't murder an otherwise-valid agent run. The runner's
            # trace will show the fallback via stderr.
            try:
                reply = _ask_via_socket(socket_path, payload)
            except (OSError, ConnectionError) as e:
                sys.stderr.write(f"ask_user: socket {socket_path} unreachable ({e}); falling back to stdin\n")
                # Re-include question_id in stdin payload for trace symmetry.
                reply = _ask_via_stdin(payload)
        else:
            reply = _ask_via_stdin(payload)
    except Exception as e:
        print(json.dumps({"error": f"ask_user transport failure: {e}"}))
        return 1

    if reply.get("question_id") != question_id:
        # A mismatched question_id means the host crossed wires; surface as
        # error rather than silently accepting a stale answer.
        print(json.dumps({
            "error": "ask_user reply question_id mismatch",
            "expected": question_id,
            "got": reply.get("question_id"),
        }))
        return 1

    out: dict = {"answer": reply.get("answer", "")}
    if "selected_option" in reply:
        out["selected_option"] = reply["selected_option"]
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
