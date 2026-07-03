"""``ConfigMapAdapter`` — the zero-code angle. Declare, in YAML, which fields of
a JSON log map to fabri's event roles; no Python needed:

    ingest:
      adapters:
        - name: prodlogs
          kind: configmap
          mapping:
            session_key: trace_id      # groups records into sessions
            task_field:  prompt        # sets the session task
            tool_field:  tool.name     # dotted paths supported
            ok_field:    tool.ok       # truthy → success
            error_field: tool.error
            args_field:  tool.input

Registered under ``name`` at ``Improver.from_config`` time so it's addressable
as ``adapter="prodlogs"``.
"""
from __future__ import annotations

from typing import Iterator

from fabri.ingest.adapters.base import Session, final_event, safe_session_id, start_event, tool_event
from fabri.ingest.sources import LogSource, _dotted


class ConfigMapAdapter:
    def __init__(self, name: str, mapping: dict):
        self.name = name
        self.mapping = mapping or {}
        if not self.mapping.get("tool_field"):
            raise ValueError(f"configmap adapter {name!r} requires mapping.tool_field")

    def sessions(self, source: LogSource, options: dict) -> Iterator[Session]:
        m = {**self.mapping, **(options or {})}
        session_key = m.get("session_key")
        task_field = m.get("task_field")
        tool_field = m["tool_field"]
        ok_field = m.get("ok_field")
        error_field = m.get("error_field")
        args_field = m.get("args_field")

        buckets: dict[str, dict] = {}
        order: list[str] = []
        for rec in source.records():
            skey = str(_dotted(rec, session_key)) if session_key else "_all"
            if _dotted(rec, session_key) is None and session_key:
                skey = "_ungrouped"
            if skey not in buckets:
                buckets[skey] = {"task": None, "events": []}
                order.append(skey)
            b = buckets[skey]
            if task_field and not b["task"]:
                task = _dotted(rec, task_field)
                if task:
                    b["task"] = str(task)
            tool = _dotted(rec, tool_field)
            if tool is None:
                continue
            error = _dotted(rec, error_field) if error_field else None
            if ok_field is not None:
                ok = bool(_dotted(rec, ok_field))
            else:
                ok = error is None  # no explicit ok field → presence of an error means failure
            args = _dotted(rec, args_field) if args_field else None
            b["events"].append(
                tool_event(
                    str(tool),
                    args=args if isinstance(args, dict) else ({"value": args} if args is not None else None),
                    ok=ok,
                    error=str(error) if (error and not ok) else None,
                )
            )

        for skey in order:
            b = buckets[skey]
            task = b["task"] or (skey if skey not in ("_all", "_ungrouped") else source.name)
            events = [start_event(task), *b["events"], final_event(_outcome(b["events"]))]
            key = skey if skey not in ("_all", "_ungrouped") else source.name
            yield Session(safe_session_id(self.name, key), events)


def _outcome(events: list[dict]) -> str:
    calls = [e for e in events if e.get("type") == "tool_call"]
    if not calls:
        return "incomplete"
    return "success" if (calls[-1].get("result") or {}).get("ok") else "failed"
