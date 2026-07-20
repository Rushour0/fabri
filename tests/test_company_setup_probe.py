from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
import yaml

from fabri.benchmarks.company_setup_probe import (
    ProbeError,
    analyze_run,
    apply_delegated_token_floor,
    build_preflight_manifest,
    load_probe_case,
    run_probe,
    score_text,
)

pytestmark = pytest.mark.integration


def _write_dataset_and_company(tmp_path: Path, *, replicas: int = 2) -> tuple[Path, Path]:
    roster_root = tmp_path / "rosters"
    company_dir = roster_root / "companies" / "support-hq"
    company_dir.mkdir(parents=True)
    (company_dir / "company.toml").write_text(
        """
[company]
name = "support-hq"
memory_namespace = "support_hq"
max_cost_usd = 1.0

[[node]]
id = "ceo"
report_to = ""

[[node]]
id = "crew"
report_to = "ceo"
agency = "../../agencies/crew"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    dataset = tmp_path / "dataset.yaml"
    dataset.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "defaults": {"roster_root_env": "FABRI_ROSTERS_ROOT"},
                "cases": [
                    {
                        "id": "support",
                        "company_source": "companies/support-hq/company.toml",
                        "holdout_prompt": "Return the checkout update without blame.",
                        "expected": {"required": ["checkout"], "forbidden": ["blame"]},
                        "setup_probe": {
                            "replicas": replicas,
                            "required_delegations": ["crew"],
                            "candidates": [
                                {"id": "baseline"},
                                {
                                    "id": "delegated_artifact_tokens_256",
                                    "delegated_llm_max_tokens_floor": 256,
                                },
                            ],
                        },
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return dataset, roster_root


def _agent_config(
    name: str,
    *,
    max_tokens: int,
    child: Path | None = None,
) -> dict[str, object]:
    agents = []
    enabled: list[str] = []
    if child is not None:
        agents = [{"name": "crew", "description": "crew", "config": str(child)}]
        enabled = ["crew"]
    return {
        "agent": {"name": name, "max_steps": 10},
        "llm": {
            "provider": "openai",
            "model": "gpt-test",
            "max_tokens": max_tokens,
            "api_key_env": "OPENAI_API_KEY",
        },
        "tools": {
            "manifest_dir": ["builtin"],
            "enabled": enabled,
            "agents": agents,
        },
        "memory": {
            "backend": "sqlite",
            "sqlite_path": "memory.sqlite",
            "collection": name,
        },
    }


def test_load_probe_case_resolves_source_and_rejects_arbitrary_candidate_settings(
    tmp_path: Path,
) -> None:
    dataset, roster_root = _write_dataset_and_company(tmp_path)

    case = load_probe_case(
        dataset,
        "support",
        environ={"FABRI_ROSTERS_ROOT": str(roster_root)},
    )

    assert case.company_name == "support-hq"
    assert case.required_delegations == ("crew",)
    assert case.required_terms == (("checkout",),)
    assert case.candidates[1].delegated_llm_max_tokens_floor == 256

    raw = yaml.safe_load(dataset.read_text(encoding="utf-8"))
    raw["cases"][0]["setup_probe"]["candidates"][1]["system_prompt"] = "unsafe"
    dataset.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(ProbeError, match="unsupported settings"):
        load_probe_case(
            dataset,
            "support",
            environ={"FABRI_ROSTERS_ROOT": str(roster_root)},
        )


def test_token_floor_changes_only_delegated_main_and_preflight_labels_narrator(
    tmp_path: Path,
) -> None:
    child = tmp_path / "crew.yaml"
    root = tmp_path / "ceo.yaml"
    child.write_text(yaml.safe_dump(_agent_config("crew", max_tokens=60)), encoding="utf-8")
    root.write_text(
        yaml.safe_dump(_agent_config("ceo", max_tokens=1024, child=child)),
        encoding="utf-8",
    )

    changed = apply_delegated_token_floor(root, 256)
    manifest = build_preflight_manifest(root)

    assert changed == ["root/crew"]
    assert yaml.safe_load(root.read_text(encoding="utf-8"))["llm"]["max_tokens"] == 1024
    assert yaml.safe_load(child.read_text(encoding="utf-8"))["llm"]["max_tokens"] == 256
    assert apply_delegated_token_floor(root, 256) == []
    crew = cast(list[dict[str, object]], manifest["configs"])[1]
    roles = cast(list[dict[str, object]], crew["roles"])
    assert next(role for role in roles if role["role"] == "main")["artifact_role"] is True
    assert next(role for role in roles if role["role"] == "narrator")["max_tokens"] == 60
    assert not cast(list[str], manifest["warnings"])


def test_score_text_is_case_insensitive_and_deterministic() -> None:
    required = (("checkout",), ("rollback", "rolled back"))
    assert score_text("Checkout was rolled back", required, ("blame",)) == {
        "passed": True,
        "missing": [],
        "forbidden": [],
    }
    assert score_text("Checkout blame", required, ("blame",)) == {
        "passed": False,
        "missing": ["rollback | rolled back"],
        "forbidden": ["blame"],
    }
    assert score_text("We will follow up", (("follow-up",),), ())["passed"] is True


@pytest.mark.parametrize(
    ("events", "expected_failure"),
    [
        pytest.param(
            [
                {"type": "cost_unaccounted", "reason": "provider usage missing"},
                {"type": "usage", "cost_usd": 0.01},
            ],
            "cost_unaccounted",
            id="unaccounted-cost",
        ),
        pytest.param(
            [
                {"type": "failed", "reason": "response truncated at max_tokens"},
                {"type": "usage", "cost_usd": 0.01},
            ],
            "truncation",
            id="truncated-terminal-event",
        ),
        pytest.param(
            [
                {"type": "error", "reason": "delegated tool timeout"},
                {"type": "usage", "cost_usd": 0.01},
            ],
            "timeout",
            id="timeout-terminal-event",
        ),
    ],
)
def test_analyze_run_rejects_hard_operational_signals(
    tmp_path: Path,
    events: list[dict[str, object]],
    expected_failure: str,
) -> None:
    traces = tmp_path / ".fabri" / "traces"
    traces.mkdir(parents=True)
    (traces / "root.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )

    result = analyze_run(
        {
            "session_id": "root",
            "outcome": "success",
            "final_text": "scoreable artifact",
        },
        tmp_path,
        (),
    )

    assert result["complete"] is False
    assert expected_failure in result["failures"]


def test_probe_skips_noop_candidate_without_model_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, roster_root = _write_dataset_and_company(tmp_path, replicas=1)
    monkeypatch.setenv("FABRI_ROSTERS_ROOT", str(roster_root))
    loaded = load_probe_case(dataset, "support")
    case = replace(loaded, candidates=(loaded.candidates[1],))

    def compile_only(
        argv: list[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env, timeout_s
        if "compile" not in argv:
            raise AssertionError("a no-op candidate must not start a model run")
        destination = Path(argv[argv.index("--dest") + 1]) / "support-hq"
        child = destination / "agencies" / "crew" / "agent.yaml"
        child.parent.mkdir(parents=True)
        child.write_text(
            yaml.safe_dump(_agent_config("crew", max_tokens=256)),
            encoding="utf-8",
        )
        root = destination / "ceo.yaml"
        root.write_text(
            yaml.safe_dump(_agent_config("ceo", max_tokens=1024, child=child)),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, "compiled", "")

    result = run_probe(case, tmp_path / "results", command_runner=compile_only)

    candidate = cast(list[dict[str, object]], result["candidates"])[0]
    run = cast(list[dict[str, object]], candidate["runs"])[0]
    assert result["status"] == "no_viable_setup"
    assert run["attempt_status"] == "invalid_measurement"
    assert run["failure_reasons"] == ["candidate_noop"]
    assert run["total_cost_usd"] is None


def test_probe_rejects_root_recovery_after_child_failure_and_selects_floor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, roster_root = _write_dataset_and_company(tmp_path)
    monkeypatch.setenv("FABRI_ROSTERS_ROOT", str(roster_root))
    case = load_probe_case(dataset, "support")
    state_roots: set[str] = set()
    run_number = 0

    def fake_runner(
        argv: list[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal run_number
        del cwd, timeout_s
        if "compile" in argv:
            destination = Path(argv[argv.index("--dest") + 1]) / "support-hq"
            child = destination / "agencies" / "crew" / "agent.yaml"
            child.parent.mkdir(parents=True)
            child.write_text(
                yaml.safe_dump(_agent_config("crew", max_tokens=60)), encoding="utf-8"
            )
            root = destination / "ceo.yaml"
            root.write_text(
                yaml.safe_dump(_agent_config("ceo", max_tokens=1024, child=child)),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "compiled", "")

        run_number += 1
        root_config = Path(argv[argv.index("--config") + 1])
        child_config = root_config.parent / "agencies" / "crew" / "agent.yaml"
        child_tokens = yaml.safe_load(child_config.read_text(encoding="utf-8"))["llm"][
            "max_tokens"
        ]
        state_root = env["FABRI_HOME"]
        state_roots.add(state_root)
        traces = Path(state_root) / ".fabri" / "traces"
        traces.mkdir(parents=True)
        root_session = f"root-{run_number}"
        child_session = f"child-{run_number}"
        child_success = child_tokens >= 256
        child_outcome = "success_with_recovery" if child_success else "failed"
        child_events = [
            *(
                [{
                    "type": "tool_call",
                    "name": "list_dir",
                    "result": {"ok": False, "error": "no such directory"},
                }]
                if child_success else []
            ),
            {
                "type": "final" if child_success else "failed",
                "outcome": child_outcome,
                **({} if child_success else {"reason": "response truncated at max_tokens"}),
            },
            {"type": "usage", "cost_usd": 0.02, "guidelines_retrieved": 0},
        ]
        (traces / f"{child_session}.jsonl").write_text(
            "\n".join(json.dumps(event) for event in child_events) + "\n",
            encoding="utf-8",
        )
        wrapped_child = {
            "ok": child_success,
            "result": {
                "session_id": child_session,
                "outcome": child_outcome,
                "usage": {"cost_usd": 0.02, "total_cost_usd": 0.02},
            },
            # Narration is best-effort. A successful artifact must not be
            # rejected merely because its child process logged this retry.
            "stderr": (
                "openai response truncated at max_tokens=60; retrying once at 120"
                if child_success else "response truncated at max_tokens"
            ),
        }
        root_outcome = "success" if child_success else "success_with_recovery"
        root_events = [
            {
                "type": "retrieval",
                "candidates": [{"kind": "postmortem"}],
            },
            {"type": "tool_call", "name": "crew", "result": wrapped_child},
            {"type": "final", "outcome": root_outcome},
            {"type": "usage", "cost_usd": 0.01, "guidelines_retrieved": 1},
        ]
        (traces / f"{root_session}.jsonl").write_text(
            "\n".join(json.dumps(event) for event in root_events) + "\n",
            encoding="utf-8",
        )
        payload = {
            "session_id": root_session,
            "success": True,
            "final_text": "Checkout follow-up" if child_success else "Checkout follow-up",
            "outcome": root_outcome,
            "usage": {"cost_usd": 0.01, "total_cost_usd": 0.03},
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    output = tmp_path / "results"
    result = run_probe(case, output, command_runner=fake_runner)

    assert len(state_roots) == 4
    assert result["status"] == "qualified"
    assert result["recommendation"] == "delegated_artifact_tokens_256"
    candidates = cast(list[dict[str, object]], result["candidates"])
    baseline, floor = candidates
    assert baseline["completion_rate"] == 0
    assert baseline["conditional_rubric_pass_rate"] is None
    assert baseline["end_to_end_pass_rate"] == 0
    assert floor["completion_rate"] == 1
    assert floor["end_to_end_pass_rate"] == 1
    assert floor["median_total_cost_usd"] == pytest.approx(0.03)

    public_json = (output / "results.json").read_text(encoding="utf-8")
    assert "root-" not in public_json
    assert "child-" not in public_json
    assert "Return the checkout" not in public_json
    profile = yaml.safe_load((output / "recommended-profile.yaml").read_text(encoding="utf-8"))
    assert profile["overrides"]["delegated_llm_max_tokens_floor"] == 256
    assert (output / "private-attempts" / "baseline" / "replica-01" / "private" / "run.stdout").is_file()
