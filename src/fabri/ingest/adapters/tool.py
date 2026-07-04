"""``ToolAdapter`` — the polyglot escape hatch. An adapter written in ANY
language, reusing fabri's tool contract: a JSON manifest next to an executable
that reads ``{"lines": [...]}`` on stdin and returns ``{"sessions": [...]}`` on
stdout, where each session is ``{"session_id": str, "events": [ ... ]}``.

Ship one as a skill and ``fabri skills install`` wires it in. The subprocess
runs through the same ``Sandbox`` the agent's tools use, so it inherits the
``sandbox_root`` jail for free.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from fabri.core.logging_setup import get_logger
from fabri.ingest.adapters.base import Session, normalize_events, safe_session_id
from fabri.sandbox import LocalSandbox, Sandbox
from fabri.tools.manifest_schema import ToolManifest

logger = get_logger()


class ToolAdapter:
    def __init__(self, name: str, manifest: ToolManifest, sandbox: Sandbox | None = None):
        self.name = name
        self.manifest = manifest
        self.sandbox = sandbox or LocalSandbox()

    @classmethod
    def from_manifest_file(cls, name: str, path: str | Path, sandbox: Sandbox | None = None) -> "ToolAdapter":
        return cls(name, ToolManifest.from_file(Path(path)), sandbox=sandbox)

    def sessions(self, source, options: dict) -> Iterator[Session]:
        # Batch the raw lines and hand them to the external adapter once. Tool
        # adapters are the batch escape hatch; a streaming host uses a native
        # or configmap adapter instead.
        lines = list(source.lines())
        payload = {"lines": lines, "options": options or {}}
        resp = self.sandbox.run_tool(self.manifest, payload)
        if not resp.get("ok"):
            logger.warning("ingest: tool adapter %r failed: %s", self.name, resp.get("error"))
            return
        result = resp.get("result") or {}
        for i, s in enumerate(result.get("sessions", [])):
            raw_events = s.get("events", [])
            events, skipped = normalize_events(raw_events)
            if skipped:
                logger.warning("ingest: tool adapter %r session %d dropped %d bad events", self.name, i, skipped)
            sid = s.get("session_id") or f"{i}"
            yield Session(safe_session_id(self.name, str(sid)), events)
