"""Unit tests for G14 (`fabri tool init <lang> <name>`)."""
from __future__ import annotations

import json
import stat

import pytest

from fabri.tool_scaffold import SUPPORTED_LANGUAGES, scaffold_tool


@pytest.mark.parametrize("lang", SUPPORTED_LANGUAGES)
def test_scaffold_tool_writes_manifest_and_executable(tmp_path, lang):
    """Every supported language must produce a parseable manifest + an
    executable stub."""
    result = scaffold_tool(lang, "my_tool", tmp_path)
    files = set(result["created"])
    assert "my_tool.json" in files
    manifest = json.loads((tmp_path / "my_tool.json").read_text())
    assert manifest["name"] == "my_tool"
    # The command should reference an executable in the same dir.
    cmd = manifest["command"]
    assert any("my_tool" in part for part in cmd)


def test_scaffold_tool_rejects_unknown_language(tmp_path):
    with pytest.raises(ValueError, match="unknown language"):
        scaffold_tool("brainfuck", "my_tool", tmp_path)


def test_scaffold_tool_rejects_non_alphanumeric_name(tmp_path):
    with pytest.raises(ValueError, match="alphanumeric"):
        scaffold_tool("python", "bad/name", tmp_path)


def test_scaffold_tool_skips_existing_without_force(tmp_path):
    (tmp_path / "my_tool.json").write_text('{"existing": true}')
    result = scaffold_tool("python", "my_tool", tmp_path)
    assert "my_tool.json" in result["skipped"]
    assert json.loads((tmp_path / "my_tool.json").read_text()) == {"existing": True}


def test_scaffold_tool_force_overwrites(tmp_path):
    (tmp_path / "my_tool.json").write_text('{"existing": true}')
    result = scaffold_tool("python", "my_tool", tmp_path, force=True)
    assert "my_tool.json" in result["created"]
    assert "existing" not in (tmp_path / "my_tool.json").read_text()


def test_scaffold_tool_bash_executable_is_chmod_755(tmp_path):
    """Bash scaffolds need to be executable so `bash <path>` works in the
    runner without an extra setup step."""
    scaffold_tool("bash", "my_tool", tmp_path)
    mode = (tmp_path / "my_tool.sh").stat().st_mode
    assert mode & stat.S_IXUSR  # owner execute bit set
