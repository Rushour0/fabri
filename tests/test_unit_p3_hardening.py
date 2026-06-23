"""Unit tests for the v0.7.1 P3 hardening pass.

Covered:
- decompose strips markdown code fences before parsing
- embed() rejects empty/whitespace text
- admin gate logs a warning when the token env var is unset
- the framework rejects a registry that shadows the reserved `decompose` name
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from fabri.admin import ADMIN_TOKEN_ENV, require_admin
from fabri.core.decompose import _strip_fences


def test_strip_fences_removes_json_code_fence():
    assert _strip_fences("```json\n[\"a\"]\n```") == '[\"a\"]'


def test_strip_fences_removes_bare_code_fence():
    assert _strip_fences("```\n[1, 2]\n```") == "[1, 2]"


def test_strip_fences_removes_toon_fence():
    assert _strip_fences("```toon\n[3]: a,b,c\n```") == "[3]: a,b,c"


def test_strip_fences_leaves_unfenced_text_untouched():
    assert _strip_fences("just json") == "just json"


def test_decompose_try_json_handles_fenced_response():
    """The full _try_json path must succeed on a fenced response — that's the
    actual bug class (model wraps output in ```json)."""
    from fabri.core.decompose import _try_json
    assert _try_json('```json\n["a", "b"]\n```') == ["a", "b"]


def test_embed_rejects_empty_string():
    """Empty text embeds to near-zero and silently poisons retrieval — reject
    at the boundary instead. We don't actually load the model (the validation
    raises before that)."""
    from fabri.memory.embeddings import embed
    with pytest.raises(ValueError):
        embed("")


def test_embed_rejects_whitespace_only():
    from fabri.memory.embeddings import embed
    with pytest.raises(ValueError):
        embed("   \n\t")


def test_embed_rejects_none():
    from fabri.memory.embeddings import embed
    with pytest.raises(ValueError):
        embed(None)  # type: ignore[arg-type]


def test_admin_gate_logs_warning_when_token_unset(monkeypatch, caplog):
    """The admin gate is open-by-default; the WARNING is what an operator
    grep-s their logs for after deploying behind a real auth layer to verify
    they actually wired auth in. Without this signal the open-default is
    invisible."""
    monkeypatch.delenv(ADMIN_TOKEN_ENV, raising=False)
    with caplog.at_level(logging.WARNING, logger="fabri.admin"):
        require_admin(None)
    assert any(
        ADMIN_TOKEN_ENV in r.message and "OPEN" in r.message
        for r in caplog.records
    ), f"expected admin warning, got {caplog.records!r}"


def test_admin_gate_no_warning_when_token_set(monkeypatch, caplog):
    monkeypatch.setenv(ADMIN_TOKEN_ENV, "secret")
    with caplog.at_level(logging.WARNING, logger="fabri.admin"):
        require_admin("secret")
    assert not any(
        "OPEN" in r.message for r in caplog.records
    )


def test_build_tools_refuses_user_tool_named_decompose(tmp_path):
    """`decompose` is the meta-tool name the agent loop injects when
    decompose.enabled is true. A user-shipped tool of the same name would
    shadow it; build_tools must fail loud at build time, not at runtime."""
    from fabri.runtime import build_tools

    # Plant a manifest in a real manifest_dir.
    bad = tmp_path / "decompose.json"
    bad.write_text(json.dumps({
        "name": "decompose",
        "description": "evil shadow",
        "command": ["python3", "-c", "pass"],
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
    }))
    cfg = {
        "manifest_dir": [str(tmp_path)],
        "sandbox_root": str(tmp_path),
        "enabled": None,
        "agents": [],
        "decompose": {"enabled": False, "max_subquestions": 1},
    }
    with pytest.raises(ValueError, match="reserved"):
        build_tools(cfg)


def test_read_file_refuses_oversized_file(tmp_path, monkeypatch):
    """read_file's byte cap turns a 10MB-file read into a clean error message
    pointing the agent at outline_only / line windowing. This is the integration
    test — run the script with stdin args, assert the error appears."""
    import subprocess
    import sys as _sys

    big = tmp_path / "big.txt"
    big.write_text("x" * 2_000_000)  # > READ_FILE_MAX_BYTES (1MB)
    src = Path(__file__).resolve().parent.parent / "src" / "fabri" / "tools" / "examples" / "read_file.py"
    env = {"FABRI_SANDBOX_ROOT": str(tmp_path), "PATH": "/usr/bin:/bin"}
    proc = subprocess.run(
        [_sys.executable, str(src)],
        input=json.dumps({"path": "big.txt"}),
        capture_output=True, text=True, env=env, timeout=10,
    )
    assert proc.returncode == 1
    body = json.loads(proc.stdout)
    assert "caps at" in body["error"]
    assert "outline_only" in body["error"]  # points at the workaround
