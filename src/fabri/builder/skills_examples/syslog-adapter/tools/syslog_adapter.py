"""syslog_adapter -- example polyglot Improver adapter (fabri tool contract).

Reads one JSON object from stdin: {"lines": [<raw log line>, ...], "options": {}}
Writes one JSON object to stdout: {"sessions": [...]}  (the fabri runner wraps
this in the {"ok": ..., "result": ...} envelope; the tool emits only its payload)

Each session is {"session_id": str, "events": [<fabri event dict>, ...]} using
the fabri EventType vocabulary. This file is pure stdlib on purpose: an adapter
is language-agnostic and must not depend on fabri being importable.

Line format understood (space-separated key=value tokens):
    trace=<id> task=<text> tool=<name> status=<ok|error> error=<msg>
`trace` groups lines into a session; a line without `tool` only supplies task.
"""
import json
import sys


def _kv(line):
    out = {}
    for tok in line.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out


def _tool_event(name, ok, error):
    result = {"ok": True} if ok else {"ok": False, "error": error or "unknown error"}
    return {"type": "tool_call", "name": name, "args": {}, "result": result}


def build_sessions(lines):
    order = []
    groups = {}
    for raw in lines:
        line = (raw or "").strip()
        if not line:
            continue
        kv = _kv(line)
        skey = kv.get("trace", "_all")
        if skey not in groups:
            groups[skey] = {"task": None, "events": []}
            order.append(skey)
        g = groups[skey]
        if kv.get("task") and not g["task"]:
            g["task"] = kv["task"]
        if kv.get("tool"):
            ok = kv.get("status", "ok").lower() not in ("error", "fail", "failed", "false")
            g["events"].append(_tool_event(kv["tool"], ok, kv.get("error")))

    sessions = []
    for skey in order:
        g = groups[skey]
        calls = [e for e in g["events"] if e["type"] == "tool_call"]
        outcome = "incomplete" if not calls else ("success" if calls[-1]["result"]["ok"] else "failed")
        events = [{"type": "start", "task": g["task"] or skey}]
        events += g["events"]
        events.append({"type": "final", "outcome": outcome})
        sessions.append({"session_id": skey, "events": events})
    return sessions


def main() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    sessions = build_sessions(payload.get("lines", []))
    print(json.dumps({"sessions": sessions}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
