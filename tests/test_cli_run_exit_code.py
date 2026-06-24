"""`fabri run`'s process exit code is the contract host services dispatch on
(e.g. ludexel's `runs` collection marks a run failed when the CLI exits non-zero).
Both Outcome.SUCCESS and Outcome.SUCCESS_WITH_RECOVERY must exit 0; the
incomplete/failed outcomes must exit non-zero. This pins that contract so a
typo'd literal (the previous "succeeded" bug) can't regress it."""
import argparse

import pytest

from fabri import cli
from fabri.core.outcome import Outcome


def _invoke_cmd_run(monkeypatch, result, *, entries=()):
    monkeypatch.setattr(cli, "load_config", lambda _p: {
        "llm": {"api_key_env": "FAKE_KEY"},
        "memory": {
            "qdrant_url": "x", "collection": "c", "top_k": 1,
            "guideline_max_tokens": 1, "similarity_threshold": 0.0,
            "promotion_threshold_sessions": 1,
        },
        "tools": {"decompose": {"max_subquestions": 1}},
        "agent": {"max_steps": 1},
    })
    monkeypatch.setattr(cli, "_require_api_key", lambda _e: None)
    monkeypatch.setattr(cli, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_open_store", lambda _c: object())
    monkeypatch.setattr(cli, "build_tools", lambda _c: [])
    monkeypatch.setattr(cli, "build_tool_defs", lambda *a, **k: [])
    monkeypatch.setattr(cli, "build_llm", lambda *a, **k: object())
    monkeypatch.setattr(cli, "build_run_llms", lambda *a, **k: {
        "llm": object(), "decompose_llm": None, "planner_llm": None, "narrator_llm": None,
    })
    monkeypatch.setattr(cli, "run_agent", lambda *a, **k: result)
    # Per-call override of process_trace so a test can assert on the
    # "Synthesized N guideline(s)" UX without the helper stomping its setup.
    entries_list = list(entries)
    monkeypatch.setattr(cli, "process_trace", lambda *a, **k: entries_list)

    args = argparse.Namespace(
        config=None, task="t", session_id="sid", verbose=False, ask_user_socket=None,
    )
    try:
        cli.cmd_run(args)
        return 0
    except SystemExit as e:
        return int(e.code or 0)


@pytest.mark.parametrize("outcome", [Outcome.SUCCESS.value, Outcome.SUCCESS_WITH_RECOVERY.value])
def test_cmd_run_exits_zero_on_success_outcomes(monkeypatch, capsys, outcome):
    code = _invoke_cmd_run(monkeypatch, {"success": True, "outcome": outcome, "final_text": "ok"})
    assert code == 0, f"outcome={outcome} should exit 0, got {code}"


@pytest.mark.parametrize("outcome", [
    Outcome.INCOMPLETE.value,
    Outcome.INCOMPLETE_WITH_TOOL_FAILURE.value,
    Outcome.FAILED.value,
])
def test_cmd_run_exits_nonzero_on_failure_outcomes(monkeypatch, capsys, outcome):
    code = _invoke_cmd_run(monkeypatch, {"success": False, "outcome": outcome, "final_text": ""})
    assert code != 0, f"outcome={outcome} should exit non-zero, got {code}"


def test_cmd_run_exits_nonzero_when_success_false_overrides_success_outcome(monkeypatch):
    """`success=False` must always exit non-zero, even if the outcome string
    somehow says SUCCESS. Defensive: the two fields are computed independently
    in core.agent, and a future divergence shouldn't silently swallow a failure."""
    code = _invoke_cmd_run(monkeypatch, {"success": False, "outcome": Outcome.SUCCESS.value})
    assert code != 0


def test_cmd_run_exits_nonzero_on_unknown_outcome_string(monkeypatch):
    """An unrecognized outcome value (e.g. a new enum variant that lands in the
    result before cli.py is updated, or the old "succeeded" typo) must fail
    closed, not open."""
    code = _invoke_cmd_run(monkeypatch, {"success": True, "outcome": "succeeded"})
    assert code != 0, "the old typo'd literal must NOT be treated as success"
    code = _invoke_cmd_run(monkeypatch, {"success": True, "outcome": "totally_made_up"})
    assert code != 0


def test_cmd_run_prints_synthesized_guideline_summary(monkeypatch, capsys):
    """The cmd_run UX includes a `Synthesized N guideline(s)` block when the
    pipeline returns entries. This is the user's only visible signal that the
    memory loop ran on their behalf — pin the rendering."""
    from fabri.memory.schema import MemoryEntry

    entry = MemoryEntry(
        text="Re-read after write_file to confirm persistence",
        kind="tactical",
        session_ids=["sid"],
        tags=[],
        tools=["write_file"],
        hit_count=1,
        created_at=0.0,
    )
    code = _invoke_cmd_run(
        monkeypatch,
        {"success": True, "outcome": Outcome.SUCCESS.value, "final_text": "ok"},
        entries=[entry],
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "Synthesized 1 guideline(s)" in out
    assert "[tactical]" in out
    assert "Re-read after write_file" in out
