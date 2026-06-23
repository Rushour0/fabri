"""`fabri run`'s process exit code is the contract host services dispatch on
(e.g. ludexel's `runs` collection marks a run failed when the CLI exits non-zero).
Both Outcome.SUCCESS and Outcome.SUCCESS_WITH_RECOVERY must exit 0; the
incomplete/failed outcomes must exit non-zero. This pins that contract so a
typo'd literal (the previous "succeeded" bug) can't regress it."""
import argparse

import pytest

from fabri import cli
from fabri.core.outcome import Outcome


def _invoke_cmd_run(monkeypatch, result):
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
    monkeypatch.setattr(cli, "build_decompose_llm", lambda *a, **k: object())
    monkeypatch.setattr(cli, "run_agent", lambda *a, **k: result)
    monkeypatch.setattr(cli, "process_trace", lambda *a, **k: [])

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
