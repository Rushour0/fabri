from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fabri.orchestrator.action_execution import (
    ActionExecutionError,
    apply_safe_memory_actions,
)

pytestmark = pytest.mark.unit


def _write_role(tmp_path: Path, role: str, max_tokens: int = 128) -> Path:
    path = tmp_path / f"{role}.yaml"
    path.write_text(
        yaml.safe_dump({
            "agent": {"name": role},
            "llm": {"provider": "openai", "max_tokens": max_tokens},
        }),
        encoding="utf-8",
    )
    return path


def _resolution(*, capability: str = "configure_role", target: int = 256) -> dict:
    return {
        "problem_signature": {"configured_cap": 128, "retry_cap": 256},
        "scope": {
            "company": "revenue_ops",
            "agency": "market_research_brief",
            "roles": ["researcher"],
        },
        "preconditions": [
            {"field": "roles_config.researcher.max_tokens", "equals": 128},
        ],
        "steps": [{
            "capability": capability,
            "args_template": {"role": "researcher", "max_tokens": target},
        }],
        "policy": {"idempotent": True, "max_attempts": 1, "approval": "shadow"},
    }


def test_applies_allowlisted_token_cap_recovery(tmp_path: Path) -> None:
    config = _write_role(tmp_path, "researcher")

    applied = apply_safe_memory_actions(
        [_resolution()],
        [{"name": "researcher", "config": str(config)}],
    )

    assert [item.to_dict() for item in applied] == [{
        "role": "researcher",
        "config_path": str(config),
        "previous_max_tokens": 128,
        "new_max_tokens": 256,
    }]
    assert yaml.safe_load(config.read_text(encoding="utf-8"))["llm"]["max_tokens"] == 256


@pytest.mark.parametrize(
    ("resolution", "message"),
    [
        (_resolution(capability="run_shell"), "unsupported"),
        (_resolution(target=1024), "does not match"),
    ],
)
def test_refuses_non_allowlisted_or_oversized_actions(
    tmp_path: Path,
    resolution: dict,
    message: str,
) -> None:
    config = _write_role(tmp_path, "researcher")

    with pytest.raises(ActionExecutionError, match=message):
        apply_safe_memory_actions(
            [resolution],
            [{"name": "researcher", "config": str(config)}],
        )

    assert yaml.safe_load(config.read_text(encoding="utf-8"))["llm"]["max_tokens"] == 128


def test_refuses_stale_precondition_without_rewriting_config(tmp_path: Path) -> None:
    config = _write_role(tmp_path, "researcher", max_tokens=512)
    before = config.read_text(encoding="utf-8")

    with pytest.raises(ActionExecutionError, match="no longer has"):
        apply_safe_memory_actions(
            [_resolution()],
            [{"name": "researcher", "config": str(config)}],
        )

    assert config.read_text(encoding="utf-8") == before


def test_duplicate_recurrences_apply_one_change_per_role(tmp_path: Path) -> None:
    config = _write_role(tmp_path, "researcher")

    applied = apply_safe_memory_actions(
        [_resolution(), _resolution()],
        [{"name": "researcher", "config": str(config)}],
    )

    assert len(applied) == 1
    assert applied[0].new_max_tokens == 256
