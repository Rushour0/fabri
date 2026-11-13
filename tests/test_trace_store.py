"""fabri.service.trace_store and the trace paths built on it: tail_events and
append_trace_event must never follow a run-controlled link."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from fabri.service.ask_user_listener import append_trace_event
from fabri.service.tailer import run_trace_path, tail_events
from fabri.service.trace_store import (
    TraceOpenError,
    TraceReader,
    TraceWriter,
    open_append,
    open_read,
    trace_root,
)

SID = "sess-trace-0001"


def _event(n: int, etype: str = "step") -> str:
    return json.dumps({"type": etype, "n": n}) + "\n"


@pytest.fixture
def layout(tmp_path: Path):
    """A run home with its trace, a secret file beside it (installs.db
    stands in for every sensitive host file), and another run's trace."""
    home_root = tmp_path / "homes"
    run_home = home_root / SID
    trace = run_trace_path(run_home, SID)
    trace.parent.mkdir(parents=True)
    secret = home_root / "installs.db"
    secret.write_text("SECRET-TOKEN-ROW\n")
    other = run_trace_path(home_root / "sess-other-0002", "sess-other-0002")
    other.parent.mkdir(parents=True)
    other.write_text(_event(99, "final"))
    return trace, secret, other


def test_trace_root_is_the_run_home(layout):
    trace, _, _ = layout
    assert trace_root(trace) == trace.parent.parent.parent
    assert trace_root(Path("/x/y/t.jsonl")) == Path("/x/y")


# ---------------------------------------------------------------- reader


def test_symlink_to_secret_yields_nothing_and_raises(layout):
    trace, secret, _ = layout
    os.symlink(os.path.relpath(secret, trace.parent), trace)
    seen: list[dict] = []
    with pytest.raises(TraceOpenError):
        for ev in tail_events(trace, is_running=lambda: False):
            seen.append(ev)
    assert seen == []


def test_symlink_to_other_runs_trace_raises(layout):
    trace, _, other = layout
    os.symlink(other, trace)
    with pytest.raises(TraceOpenError):
        list(tail_events(trace, is_running=lambda: False))


def test_symlinked_traces_directory_is_refused(layout, tmp_path):
    trace, _, other = layout
    traces_dir = trace.parent
    traces_dir.rmdir()
    os.symlink(other.parent, traces_dir)
    # Even a file with this run's name in the redirected directory is not read.
    (other.parent / trace.name).write_text(_event(1))
    with pytest.raises(TraceOpenError):
        list(tail_events(trace, is_running=lambda: False))


def test_hardlink_is_refused(layout):
    trace, secret, _ = layout
    os.link(secret, trace)
    with pytest.raises(TraceOpenError):
        list(tail_events(trace, is_running=lambda: False))
    with pytest.raises(TraceOpenError):
        open_read(trace)


def test_fifo_is_refused_without_blocking(layout):
    trace, _, _ = layout
    os.mkfifo(trace)
    start = time.monotonic()
    with pytest.raises(TraceOpenError):
        open_read(trace)
    with pytest.raises(TraceOpenError):
        open_append(trace)
    assert time.monotonic() - start < 2


def test_symlink_swap_before_start_while_running(layout):
    """The run plants the link before the tailer first finds the file."""
    trace, secret, _ = layout
    os.symlink(secret, trace)
    with pytest.raises(TraceOpenError):
        list(tail_events(trace, is_running=lambda: True, poll_interval=0.0, timeout=1.0))


def test_replace_with_symlink_after_open_changes_nothing(layout):
    trace, secret, _ = layout
    trace.write_text(_event(1))
    reader = TraceReader(trace)
    try:
        assert reader.read_new() == _event(1).encode()
        # The run swaps its trace for a link to the secret (rename is atomic).
        tmp_link = trace.with_name("swap")
        os.symlink(secret, tmp_link)
        os.replace(tmp_link, trace)
        assert reader.read_new() == b""
    finally:
        reader.close()


def test_tail_after_open_swap_reads_only_the_original_file(layout):
    trace, secret, _ = layout
    trace.write_text(_event(1))
    state = {"polls": 0}
    original_fd_holder: list[int] = []

    def is_running() -> bool:
        state["polls"] += 1
        if state["polls"] == 1:
            # Writer keeps its own descriptor on the original inode.
            fd = os.open(trace, os.O_WRONLY | os.O_APPEND)
            original_fd_holder.append(fd)
            link = trace.with_name("swap")
            os.symlink(secret, link)
            os.replace(link, trace)
            os.write(fd, _event(2, "final").encode())
            return True
        return False

    got = list(tail_events(trace, is_running=is_running, poll_interval=0.0))
    for fd in original_fd_holder:
        os.close(fd)
    assert got == [{"type": "step", "n": 1}, {"type": "final", "n": 2}]
    assert "SECRET" not in json.dumps(got)


def test_reader_waits_for_a_missing_file(layout):
    trace, _, _ = layout
    reader = TraceReader(trace)
    assert reader.read_new() == b""
    assert not reader.opened
    trace.write_text(_event(1))
    assert reader.read_new() == _event(1).encode()
    reader.close()


def test_reader_keeps_partial_multibyte_lines(layout):
    trace, _, _ = layout
    data = (json.dumps({"type": "step", "text": "café ☃"}, ensure_ascii=False) + "\n").encode()
    cut = data.index("☃".encode()) + 1
    trace.write_bytes(data[:cut])
    calls = {"n": 0}

    def is_running() -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            with open(trace, "ab") as f:
                f.write(data[cut:])
            return True
        return False

    got = list(tail_events(trace, is_running=is_running, poll_interval=0.0))
    assert got == [{"type": "step", "text": "café ☃"}]


def test_large_trace_tails_in_linear_time(layout, monkeypatch):
    """50 MiB written in many polls: the reader reads only new bytes each
    time, so the total stays near one pass over the file."""
    trace, _, _ = layout
    line = (json.dumps({"type": "step", "pad": "x" * 1000}) + "\n").encode()
    per_poll = (1 << 20) // len(line)
    polls = 50
    trace.write_bytes(b"")
    state = {"n": 0}
    read_bytes = {"n": 0}
    real_read = os.read

    def counting_read(fd, n):
        data = real_read(fd, n)
        read_bytes["n"] += len(data)
        return data

    def is_running() -> bool:
        if state["n"] < polls:
            with open(trace, "ab") as f:
                f.write(line * per_poll)
            state["n"] += 1
            return True
        return False

    monkeypatch.setattr(os, "read", counting_read)
    start = time.monotonic()
    count = sum(1 for _ in tail_events(trace, is_running=is_running, poll_interval=0.0))
    elapsed = time.monotonic() - start
    monkeypatch.undo()
    total = len(line) * per_poll * polls
    assert total >= 50 * 1_000_000
    assert count == per_poll * polls
    assert read_bytes["n"] == total
    assert elapsed < 30


# ---------------------------------------------------------------- writer


def test_append_trace_event_refuses_symlink_and_target_unchanged(layout):
    trace, secret, other = layout
    before_secret = secret.read_bytes()
    os.symlink(os.path.relpath(secret, trace.parent), trace)
    with pytest.raises(TraceOpenError):
        append_trace_event(trace, {"type": "ask_user", "question": "q"})
    assert secret.read_bytes() == before_secret

    trace.unlink()
    before_other = other.read_bytes()
    os.symlink(other, trace)
    with pytest.raises(TraceOpenError):
        append_trace_event(trace, {"type": "ask_user", "question": "q"})
    assert other.read_bytes() == before_other


def test_append_trace_event_refuses_hardlink(layout):
    trace, secret, _ = layout
    before = secret.read_bytes()
    os.link(secret, trace)
    with pytest.raises(TraceOpenError):
        append_trace_event(trace, {"type": "ask_user"})
    assert secret.read_bytes() == before


def test_append_trace_event_refuses_symlinked_parent(layout):
    trace, _, other = layout
    traces_dir = trace.parent
    traces_dir.rmdir()
    os.symlink(other.parent, traces_dir)
    with pytest.raises(TraceOpenError):
        append_trace_event(trace, {"type": "ask_user"})
    assert not (other.parent / trace.name).exists()


def test_append_creates_file_and_dirs_with_private_mode(tmp_path):
    run_home = tmp_path / SID
    run_home.mkdir()
    trace = run_trace_path(run_home, SID)
    append_trace_event(trace, {"type": "ask_user", "question": "q"})
    append_trace_event(trace, {"type": "ask_user", "question": "r"})
    lines = [json.loads(x) for x in trace.read_text().splitlines()]
    assert [x["question"] for x in lines] == ["q", "r"]
    assert all("ts" in x for x in lines)
    assert trace.stat().st_mode & 0o777 == 0o600
    assert list(tail_events(trace, is_running=lambda: False)) == lines


def test_writer_keeps_its_descriptor_after_swap(layout):
    trace, secret, _ = layout
    before = secret.read_bytes()
    with TraceWriter(trace) as writer:
        writer.append(_event(1))
        original = trace.with_name("orig")
        os.rename(trace, original)
        os.symlink(secret, trace)
        writer.append(_event(2))
    assert secret.read_bytes() == before
    assert original.read_text() == _event(1) + _event(2)


def test_foreign_owned_file_is_refused(layout, monkeypatch):
    trace, _, _ = layout
    trace.write_text(_event(1))
    real = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: real + 1)
    with pytest.raises(TraceOpenError):
        open_read(trace)
    with pytest.raises(TraceOpenError):
        append_trace_event(trace, {"type": "ask_user"})


# ---------------------------------------------------------------- review fixes


def test_one_huge_chunk_parses_in_linear_time(layout, monkeypatch):
    """20 MiB arriving in a single read_new is split once, not per line."""
    trace, _, _ = layout
    line = (json.dumps({"type": "step", "pad": "x" * 80}) + "\n").encode()
    n = (20 << 20) // len(line)
    trace.write_bytes(line * n)
    real = TraceReader.read_new
    monkeypatch.setattr(TraceReader, "read_new", lambda self, max_bytes=0: real(self, 64 << 20))
    start = time.monotonic()
    count = sum(1 for _ in tail_events(trace, is_running=lambda: False))
    assert count == n
    assert time.monotonic() - start < 10


def test_read_new_is_capped(layout):
    trace, _, _ = layout
    trace.write_bytes(b"a" * (3 << 20))
    with TraceReader(trace) as reader:
        assert len(reader.read_new(1 << 20)) == 1 << 20
        assert len(reader.read_new(1 << 20)) == 1 << 20
        assert len(reader.read_new()) == 1 << 20
        assert reader.read_new() == b""


def test_oversized_line_is_dropped_and_neighbours_kept(layout):
    from fabri.service.tailer import MAX_LINE_BYTES

    trace, _, _ = layout
    big = json.dumps({"type": "step", "pad": "y" * (MAX_LINE_BYTES + 10)}) + "\n"
    trace.write_text(_event(1) + big + _event(2) + big + _event(3, "final"))
    got = list(tail_events(trace, is_running=lambda: False))
    assert got == [{"type": "step", "n": 1}, {"type": "step", "n": 2}, {"type": "final", "n": 3}]


def test_sparse_file_without_newlines_stays_bounded(layout):
    import tracemalloc

    trace, _, _ = layout
    with open(trace, "wb") as f:
        f.truncate(256 << 20)  # 256 MiB of holes, no newline
    with open(trace, "ab") as f:
        f.write(b"\n" + _event(7, "final").encode())
    tracemalloc.start()
    try:
        got = list(tail_events(trace, is_running=lambda: False))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert got == [{"type": "final", "n": 7}]
    assert peak < 48 << 20


def test_truncated_trace_is_read_again_from_the_start(layout):
    trace, _, _ = layout
    trace.write_text(_event(1) + _event(2) + _event(3))
    with TraceReader(trace) as reader:
        assert reader.read_new() == (_event(1) + _event(2) + _event(3)).encode()
        os.truncate(trace, 0)
        with open(trace, "a") as f:
            f.write(_event(4))
        assert reader.read_new() == _event(4).encode()
        assert reader.truncations == 1


def test_tail_survives_truncation(layout):
    trace, _, _ = layout
    trace.write_text(_event(1) + '{"type": "step", "partial')
    state = {"n": 0}

    def is_running() -> bool:
        state["n"] += 1
        if state["n"] == 1:
            os.truncate(trace, 0)
            with open(trace, "a") as f:
                f.write(_event(2, "final"))
            return True
        return False

    got = list(tail_events(trace, is_running=is_running, poll_interval=0.0))
    assert got == [{"type": "step", "n": 1}, {"type": "final", "n": 2}]


def test_symlinked_run_home_is_refused(layout):
    trace, _, _ = layout
    trace.write_text('{"secret": "victim"}\n')
    run_home = trace.parent.parent.parent
    evil = run_home.parent / "evilhome"
    os.symlink(run_home, evil)
    evil_trace = run_trace_path(evil, SID)
    assert str(evil_trace).startswith(str(evil))  # not resolved to the victim's home
    with pytest.raises(TraceOpenError):
        list(tail_events(evil_trace, is_running=lambda: False))
    with pytest.raises(TraceOpenError):
        append_trace_event(evil_trace, {"type": "ask_user"})
    assert trace.read_text() == '{"secret": "victim"}\n'


def test_listener_logs_refused_trace_append_at_warning(layout, monkeypatch):
    from unittest import mock

    from fabri.service import ask_user_listener as aul

    trace, secret, _ = layout
    os.symlink(secret, trace)
    fake_logger = mock.Mock()
    monkeypatch.setattr(aul, "logger", fake_logger)
    listener = aul.AskUserListener(str(trace.parent / "unused.sock"),
                                   lambda q: append_trace_event(trace, {"type": "ask_user", **q}))
    pending = listener.ask_local(question_id="q1", question="?", options=None, default=None)
    assert pending is not None
    assert fake_logger.warning.called
    assert "refused" in fake_logger.warning.call_args[0][0]
    assert secret.read_text() == "SECRET-TOKEN-ROW\n"


def test_service_trace_readers_refuse_a_symlinked_trace(tmp_path):
    """result() and the tool-usage totals read traces through trace_store,
    so a run cannot point them at another file."""
    from fabri.service.service import _read_trace_at, _trace_tail_lines

    victim = tmp_path / "victim.jsonl"
    victim.write_text('{"type": "final", "answer": "VICTIM SECRET"}\n')
    traces = tmp_path / "run" / ".fabri" / "traces"
    traces.mkdir(parents=True)
    trace = traces / "t.jsonl"
    trace.symlink_to(victim)
    assert _read_trace_at(trace) == []
    assert _trace_tail_lines(trace) == []


def test_service_trace_readers_read_a_plain_trace(tmp_path):
    from fabri.service.service import _read_trace_at, _trace_tail_lines

    traces = tmp_path / "run" / ".fabri" / "traces"
    traces.mkdir(parents=True)
    trace = traces / "t.jsonl"
    trace.write_text('{"type": "final", "answer": "ok"}\nnot json\n')
    assert _read_trace_at(trace) == [{"type": "final", "answer": "ok"}]
    assert _trace_tail_lines(trace) == ['{"type": "final", "answer": "ok"}', "not json"]
    assert _read_trace_at(traces / "missing.jsonl") == []
