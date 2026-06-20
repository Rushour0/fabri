"""Unit tests for the sandboxed file tools (read_file, write_file, edit_file,
list_dir). Each tool is run as its real subprocess via ToolRegistry to also
cover the runner's JSON/exit-code normalization end-to-end -- the path-jail
discipline only matters if the round trip through the registry preserves it."""
import os
from pathlib import Path

import pytest

from fabri.tools.registry import ToolRegistry

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "src" / "fabri" / "tools" / "examples"


@pytest.fixture
def reg(tmp_path):
    os.environ["FABRI_SANDBOX_ROOT"] = str(tmp_path)
    return ToolRegistry(EXAMPLES_DIR)


# ---------- write_file ----------

def test_write_file_creates_file(reg, tmp_path):
    r = reg.invoke("write_file", {"path": "a.txt", "content": "hello"})
    assert r["ok"] is True
    assert r["result"]["bytes_written"] == 5
    assert (tmp_path / "a.txt").read_text() == "hello"


def test_write_file_creates_parent_dirs(reg, tmp_path):
    r = reg.invoke("write_file", {"path": "sub/dir/x.txt", "content": "y"})
    assert r["ok"] is True
    assert (tmp_path / "sub" / "dir" / "x.txt").exists()


def test_write_file_overwrites_existing(reg, tmp_path):
    (tmp_path / "f.txt").write_text("old")
    r = reg.invoke("write_file", {"path": "f.txt", "content": "new"})
    assert r["ok"] is True
    assert (tmp_path / "f.txt").read_text() == "new"


def test_write_file_rejects_path_escape(reg):
    r = reg.invoke("write_file", {"path": "../escape.txt", "content": "x"})
    assert r["ok"] is False
    assert "escapes sandbox root" in r["result"]["error"]


def test_write_file_rejects_absolute_path_outside_sandbox(reg):
    r = reg.invoke("write_file", {"path": "/tmp/agent_test_should_fail.txt", "content": "x"})
    assert r["ok"] is False


def test_write_file_handles_unicode_byte_count(reg, tmp_path):
    r = reg.invoke("write_file", {"path": "u.txt", "content": "café"})
    assert r["ok"] is True
    assert r["result"]["bytes_written"] == len("café".encode())


# ---------- read_file ----------

def test_read_file_round_trip(reg, tmp_path):
    (tmp_path / "f.txt").write_text("contents")
    r = reg.invoke("read_file", {"path": "f.txt"})
    assert r["ok"] is True
    assert r["result"]["content"] == "contents"
    assert r["result"]["path"] == "f.txt"


def test_read_file_missing(reg):
    r = reg.invoke("read_file", {"path": "nope.txt"})
    assert r["ok"] is False
    assert "no such file" in r["result"]["error"]


def test_read_file_rejects_directory(reg, tmp_path):
    (tmp_path / "d").mkdir()
    r = reg.invoke("read_file", {"path": "d"})
    assert r["ok"] is False


def test_read_file_rejects_path_escape(reg):
    r = reg.invoke("read_file", {"path": "../../etc/passwd"})
    assert r["ok"] is False
    assert "escapes sandbox root" in r["result"]["error"]


# ---------- edit_file ----------

def test_edit_file_unique_replacement(reg, tmp_path):
    (tmp_path / "f.txt").write_text("hello world")
    r = reg.invoke("edit_file", {"path": "f.txt", "old": "world", "new": "agent"})
    assert r["ok"] is True
    assert r["result"]["replacements"] == 1
    assert (tmp_path / "f.txt").read_text() == "hello agent"


def test_edit_file_missing_old_string(reg, tmp_path):
    (tmp_path / "f.txt").write_text("hello")
    r = reg.invoke("edit_file", {"path": "f.txt", "old": "bye", "new": "x"})
    assert r["ok"] is False
    assert "not found" in r["result"]["error"]


def test_edit_file_ambiguous_match_rejected_without_replace_all(reg, tmp_path):
    (tmp_path / "f.txt").write_text("ab ab ab")
    r = reg.invoke("edit_file", {"path": "f.txt", "old": "ab", "new": "X"})
    assert r["ok"] is False
    assert "not unique" in r["result"]["error"]
    # untouched
    assert (tmp_path / "f.txt").read_text() == "ab ab ab"


def test_edit_file_replace_all_replaces_every_occurrence(reg, tmp_path):
    (tmp_path / "f.txt").write_text("ab ab ab")
    r = reg.invoke("edit_file", {"path": "f.txt", "old": "ab", "new": "X", "replace_all": True})
    assert r["ok"] is True
    assert r["result"]["replacements"] == 3
    assert (tmp_path / "f.txt").read_text() == "X X X"


def test_edit_file_rejects_path_escape(reg):
    r = reg.invoke("edit_file", {"path": "../x", "old": "a", "new": "b"})
    assert r["ok"] is False


def test_edit_file_missing_file(reg):
    r = reg.invoke("edit_file", {"path": "missing.txt", "old": "a", "new": "b"})
    assert r["ok"] is False
    assert "no such file" in r["result"]["error"]


# ---------- list_dir ----------

def test_list_dir_default_is_root(reg, tmp_path):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.txt").write_text("y")
    (tmp_path / "sub").mkdir()
    r = reg.invoke("list_dir", {})
    assert r["ok"] is True
    names = {e["name"]: e["is_dir"] for e in r["result"]["entries"]}
    assert names == {"a.txt": False, "b.txt": False, "sub": True}


def test_list_dir_subpath(reg, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.txt").write_text("hi")
    r = reg.invoke("list_dir", {"path": "sub"})
    assert r["ok"] is True
    assert r["result"]["entries"] == [{"name": "x.txt", "is_dir": False}]


def test_list_dir_missing_directory(reg):
    r = reg.invoke("list_dir", {"path": "missing"})
    assert r["ok"] is False
    assert "no such directory" in r["result"]["error"]


def test_list_dir_rejects_path_escape(reg):
    r = reg.invoke("list_dir", {"path": "../.."})
    assert r["ok"] is False
    assert "escapes sandbox root" in r["result"]["error"]


def test_list_dir_empty_dir(reg, tmp_path):
    r = reg.invoke("list_dir", {"path": "."})
    assert r["ok"] is True
    assert r["result"]["entries"] == []
