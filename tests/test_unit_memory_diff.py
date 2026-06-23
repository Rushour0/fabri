"""Unit test for G3 (`fabri memory diff`).

We don't go through the CLI argparse; we call `cmd_memory_diff` directly with
a stubbed `_open_store` returning an in-memory list of MemoryEntry. Reading
from stdout via capsys.
"""
from __future__ import annotations

import argparse

import pytest

from fabri import cli
from fabri.memory.schema import MemoryEntry


class _ListStore:
    """Tiny stand-in for the memory store that just returns a list."""
    def __init__(self, entries: list[MemoryEntry]):
        self.entries = entries

    def iterate(self, kind=None, limit=None):
        if kind is None:
            return self.entries
        return [e for e in self.entries if e.kind == kind]


def _entry(text: str, session_ids: list[str], kind: str = "tactical") -> MemoryEntry:
    return MemoryEntry(text=text, kind=kind, session_ids=session_ids, hit_count=len(session_ids))


def test_memory_diff_partitions_new_shared_and_only_in_a(monkeypatch, capsys):
    """Three partitions: new in B, shared, only in A. Each entry shows up in
    exactly one."""
    store = _ListStore([
        _entry("only-A", ["sess-A"]),
        _entry("shared", ["sess-A", "sess-B"]),
        _entry("only-B", ["sess-B"]),
        _entry("unrelated", ["sess-C"]),
    ])
    monkeypatch.setattr(cli, "load_config", lambda _p: {"memory": {}})
    monkeypatch.setattr(cli, "_open_store", lambda _c: store)

    args = argparse.Namespace(
        config=None, session_a="sess-A", session_b="sess-B", markdown=False,
    )
    cli.cmd_memory_diff(args)
    out = capsys.readouterr().out
    assert "only-A" in out
    assert "only-B" in out
    assert "shared" in out
    assert "unrelated" not in out


def test_memory_diff_markdown_mode_emits_headers(monkeypatch, capsys):
    store = _ListStore([_entry("hi", ["sess-A", "sess-B"])])
    monkeypatch.setattr(cli, "load_config", lambda _p: {"memory": {}})
    monkeypatch.setattr(cli, "_open_store", lambda _c: store)
    args = argparse.Namespace(
        config=None, session_a="sess-A", session_b="sess-B", markdown=True,
    )
    cli.cmd_memory_diff(args)
    out = capsys.readouterr().out
    assert "# memory diff" in out
    assert "## " in out  # H2 section headers
