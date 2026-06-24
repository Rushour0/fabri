"""Human-readable rendering of JSONL trace events.

The orchestrator writes every step (start / tool_call / thought / final /
failed / ...) to `.fabri/traces/<sid>.jsonl` as raw dicts. This module turns
one such dict into the terminal-friendly block that `fabri traces show` and
`fabri traces tail` print. Kept out of cli.py so the formatting is unit-testable
without spawning the argparse layer.
"""
import datetime as _dt
import json
import shutil
import textwrap


def ts_prefix(ev: dict, t0: float) -> str:
    """Wallclock + relative-delta prefix used by every rendered trace line.
    Trace events always carry `ts` (orchestrator/traces.py), so this is safe
    by default -- "time should just work" without extra flags."""
    ts = ev.get("ts", t0)
    wall = _dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
    dt = ts - t0
    return f"  {wall} (+{dt:6.2f}s)"


def wrap_block(text: str, indent: str = "    ", width: int | None = None) -> str:
    """Wrap a (possibly multi-line) text block under a fixed indent. Preserves
    intentional newlines; only the long lines get wrapped."""
    if width is None:
        width = max(60, shutil.get_terminal_size((100, 20)).columns - len(indent))
    out = []
    for line in text.splitlines() or [text]:
        if not line.strip():
            out.append("")
            continue
        out.extend(textwrap.wrap(line, width=width) or [""])
    return "\n".join(indent + l for l in out)


def _looks_like_code(text: str) -> bool:
    first = next((l for l in text.splitlines() if l.strip()), "")
    return first.lstrip().startswith(("def ", "class ", "import ", "from ", "{", "[", "```"))


def format_payload(value, max_lines: int = 40) -> str:
    """Pretty-print a JSON-ish payload, truncating to `max_lines` so a giant
    tool result doesn't blow up the viewer (the full payload is still in the
    JSONL on disk)."""
    try:
        s = json.dumps(value, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = repr(value)
    lines = s.splitlines()
    if len(lines) > max_lines:
        omitted = len(lines) - max_lines
        lines = lines[:max_lines] + [f"... ({omitted} more lines truncated; see raw JSONL)"]
    return "\n".join(lines)


def _format_thought_body(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            pretty = format_payload(json.loads(stripped))
            return "\n".join("    " + l for l in pretty.splitlines())
        except json.JSONDecodeError:
            pass
    if _looks_like_code(stripped):
        return "\n".join("    ┃ " + l for l in stripped.splitlines())
    return wrap_block(text)


def render_event(ev: dict, t0: float) -> str:
    """Render one trace event dict into a printable, multi-line block."""
    kind = ev.get("type", "?")
    prefix = ts_prefix(ev, t0)
    if kind == "tool_call":
        name = ev.get("name", "?")
        result = ev.get("result", {}) or {}
        ok = result.get("ok")
        tag = ev.get("parallel_group")
        tag_str = f" [{tag}]" if tag else ""
        header = f"{prefix} tool_call {name}{tag_str} ok={ok}"
        parts = [header]
        if ev.get("args"):
            parts.append("    args:")
            parts.append(wrap_block(format_payload(ev["args"]), indent="      "))
        if result:
            parts.append("    result:")
            parts.append(wrap_block(format_payload(result), indent="      "))
        return "\n".join(parts)
    if kind == "thought":
        body = _format_thought_body(ev.get("text", ""))
        return f"{prefix} thought\n{body}"
    if kind == "step_started":
        return f"{prefix} ── step {ev.get('step')} ──"
    if kind == "step_finished":
        bits = [f"step {ev.get('step')} done"]
        for k in ("elapsed_s", "reason", "tool_count", "tool_failure"):
            if k in ev:
                bits.append(f"{k}={ev[k]}")
        return f"{prefix} ── {' '.join(bits)} ──"
    if kind == "start":
        return f"{prefix} start task={ev.get('task', '')!r}"
    if kind == "final":
        return f"{prefix} final outcome={ev.get('outcome')}\n{wrap_block(ev.get('text', ''))}"
    if kind in ("failed", "llm_error"):
        return f"{prefix} {kind} reason={ev.get('reason', '')!r}"
    if kind == "ask_user":
        return f"{prefix} ask_user q={ev.get('question', '')!r}"
    if kind == "discrepancy":
        return (
            f"{prefix} discrepancy path={ev.get('path', '')!r} "
            f"reason={ev.get('reason', '')!r}"
        )
    rest = {k: v for k, v in ev.items() if k != "ts"}
    return f"{prefix} {kind} {json.dumps(rest, default=str)[:200]}"
