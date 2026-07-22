from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
import yaml

import fabri.benchmarks.company_setup_probe as company_setup_probe
from fabri.benchmarks.company_setup_probe import (
    CLAIM_BOUNDARY,
    MANIFEST_GIT_TIMEOUT_S,
    ProbeCandidate,
    ProbeError,
    JudgeConfig,
    JudgeResult,
    analyze_run,
    apply_candidate,
    apply_delegated_token_floor,
    build_preflight_manifest,
    load_probe_case,
    main,
    render_markdown,
    run_probe,
    score_structured,
    score_text,
    validate_publication_payload,
)
from fabri.core.llm import LLMUsage

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
    assert case.structured_fields == {}
    assert case.candidates[1].delegated_llm_max_tokens_floor == 256

    raw = yaml.safe_load(dataset.read_text(encoding="utf-8"))
    raw["cases"][0]["expected"]["structured"] = {"rollback": True}
    raw["cases"][0]["judge"] = {
        "model": "gpt-4o-mini",
        "criteria": ["The response confirms a rollback."],
    }
    dataset.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    configured_case = load_probe_case(
        dataset,
        "support",
        environ={"FABRI_ROSTERS_ROOT": str(roster_root)},
    )
    assert configured_case.structured_fields == {"rollback": True}
    assert configured_case.judge == JudgeConfig(
        provider="openai",
        model="gpt-4o-mini",
        api_key_env="OPENAI_API_KEY",
        criteria=("The response confirms a rollback.",),
    )

    raw["cases"][0]["setup_probe"]["candidates"][1]["system_prompt"] = "unsafe"
    dataset.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(ProbeError, match="unsupported settings"):
        load_probe_case(
            dataset,
            "support",
            environ={"FABRI_ROSTERS_ROOT": str(roster_root)},
        )


def test_load_probe_case_accepts_bounded_knobs_and_rejects_invalid_settings(
    tmp_path: Path,
) -> None:
    dataset, roster_root = _write_dataset_and_company(tmp_path)
    valid_candidate = {
        "id": "all-knobs",
        "step_budget": 20,
        "retrieval_top_k": 9,
        "retrieval_strategy": "dense",
        "cost_ceiling": 0.5,
        "role_model": {"role": "main", "model": "gpt-4o-mini"},
        "max_parallel_spawns": 8,
        "delegation_timeout": 300,
    }
    raw = yaml.safe_load(dataset.read_text(encoding="utf-8"))
    raw["cases"][0]["setup_probe"]["candidates"] = [valid_candidate]
    dataset.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    candidate = load_probe_case(
        dataset, "support", environ={"FABRI_ROSTERS_ROOT": str(roster_root)}
    ).candidates[0]
    assert candidate.overrides() == {
        "delegated_llm_max_tokens_floor": None,
        "step_budget": 20,
        "retrieval_top_k": 9,
        "retrieval_strategy": "dense",
        "cost_ceiling": 0.5,
        "role_model": {"role": "main", "model": "gpt-4o-mini"},
        "max_parallel_spawns": 8,
        "delegation_timeout": 300.0,
    }

    for invalid, message in (
        ({**valid_candidate, "step_budget": 201}, "step_budget"),
        ({**valid_candidate, "cost_ceiling": 10.01}, "cost_ceiling"),
        ({**valid_candidate, "retrieval_strategy": "bogus"}, "retrieval_strategy"),
        ({**valid_candidate, "role_model": {"role": "worker", "model": "x"}}, "role_model.role"),
        ({**valid_candidate, "unknown": "nope"}, "unsupported settings"),
    ):
        raw["cases"][0]["setup_probe"]["candidates"] = [invalid]
        dataset.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        with pytest.raises(ProbeError, match=message):
            load_probe_case(
                dataset, "support", environ={"FABRI_ROSTERS_ROOT": str(roster_root)}
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


@pytest.mark.parametrize(
    ("candidate", "assert_written"),
    [
        pytest.param(
            ProbeCandidate("step", step_budget=20),
            lambda root, child: yaml.safe_load(child.read_text(encoding="utf-8"))["agent"]["max_steps"] == 20,
            id="step-budget",
        ),
        pytest.param(
            ProbeCandidate("top-k", retrieval_top_k=9),
            lambda root, child: yaml.safe_load(child.read_text(encoding="utf-8"))["memory"]["top_k"] == 9,
            id="retrieval-top-k",
        ),
        pytest.param(
            ProbeCandidate("strategy", retrieval_strategy="dense"),
            lambda root, child: yaml.safe_load(child.read_text(encoding="utf-8"))["memory"]["retrieval_strategy"] == "dense",
            id="retrieval-strategy",
        ),
        pytest.param(
            ProbeCandidate("cost", cost_ceiling=0.5),
            lambda root, child: yaml.safe_load(child.read_text(encoding="utf-8"))["agent"]["max_cost_usd"] == 0.5,
            id="cost-ceiling",
        ),
        pytest.param(
            ProbeCandidate("model", role_model=("main", "gpt-new")),
            lambda root, child: yaml.safe_load(child.read_text(encoding="utf-8"))["llm"]["model"] == "gpt-new",
            id="main-role-model",
        ),
        pytest.param(
            ProbeCandidate("spawns", max_parallel_spawns=8),
            lambda root, child: yaml.safe_load(root.read_text(encoding="utf-8"))["tools"]["max_parallel_spawns"] == 8,
            id="max-parallel-spawns",
        ),
        pytest.param(
            ProbeCandidate("timeout", delegation_timeout=300.0),
            lambda root, child: yaml.safe_load(root.read_text(encoding="utf-8"))["tools"]["agents"][0]["timeout_s"] == 300.0,
            id="delegation-timeout-parent-edge",
        ),
    ],
)
def test_apply_candidate_writes_each_new_knob_at_its_raw_path(
    tmp_path: Path,
    candidate: ProbeCandidate,
    assert_written: object,
) -> None:
    child = tmp_path / "crew.yaml"
    root = tmp_path / "ceo.yaml"
    child.write_text(yaml.safe_dump(_agent_config("crew", max_tokens=60)), encoding="utf-8")
    root.write_text(
        yaml.safe_dump(_agent_config("ceo", max_tokens=1024, child=child)),
        encoding="utf-8",
    )

    expected_changed = ["root"] if candidate.max_parallel_spawns is not None else ["root/crew"]
    assert apply_candidate(root, candidate) == expected_changed
    assert callable(assert_written)
    assert assert_written(root, child)


def test_apply_candidate_writes_nested_role_model_as_mapping(tmp_path: Path) -> None:
    child = tmp_path / "crew.yaml"
    root = tmp_path / "ceo.yaml"
    config = _agent_config("crew", max_tokens=60)
    cast(dict[str, object], config["llm"])["planner"] = "gpt-old"
    child.write_text(yaml.safe_dump(config), encoding="utf-8")
    root.write_text(
        yaml.safe_dump(_agent_config("ceo", max_tokens=1024, child=child)),
        encoding="utf-8",
    )

    assert apply_candidate(root, ProbeCandidate("planner", role_model=("planner", "gpt-new"))) == ["root/crew"]
    assert yaml.safe_load(child.read_text(encoding="utf-8"))["llm"]["planner"] == {"model": "gpt-new"}


def test_apply_candidate_uses_and_updates_declared_subagent_budgets(tmp_path: Path) -> None:
    child = tmp_path / "crew.yaml"
    root = tmp_path / "ceo.yaml"
    child_config = _agent_config("crew", max_tokens=60)
    child_agent = cast(dict[str, object], child_config["agent"])
    child_agent["max_steps"] = 10
    child_agent["max_cost_usd"] = 1.0
    child_agent["subagent"] = {"max_steps": 20, "max_cost_usd": 0.25}
    child.write_text(yaml.safe_dump(child_config), encoding="utf-8")
    root.write_text(
        yaml.safe_dump(_agent_config("ceo", max_tokens=1024, child=child)),
        encoding="utf-8",
    )

    assert apply_candidate(
        root, ProbeCandidate("already-effective", step_budget=20, cost_ceiling=0.5)
    ) == []

    assert apply_candidate(
        root, ProbeCandidate("raise-effective", step_budget=21, cost_ceiling=0.2)
    ) == ["root/crew"]
    written_agent = yaml.safe_load(child.read_text(encoding="utf-8"))["agent"]
    assert written_agent["max_steps"] == 21
    assert written_agent["subagent"]["max_steps"] == 21
    assert written_agent["max_cost_usd"] == 0.2
    assert written_agent["subagent"]["max_cost_usd"] == 0.2


def test_max_parallel_spawns_ignores_leaf_only_values_and_noops_at_root(
    tmp_path: Path,
) -> None:
    child = tmp_path / "crew.yaml"
    root = tmp_path / "ceo.yaml"
    child_config = _agent_config("crew", max_tokens=60)
    cast(dict[str, object], child_config["tools"])["max_parallel_spawns"] = 4
    child.write_text(yaml.safe_dump(child_config), encoding="utf-8")
    root_config = _agent_config("ceo", max_tokens=1024, child=child)
    cast(dict[str, object], root_config["tools"])["max_parallel_spawns"] = 8
    root.write_text(yaml.safe_dump(root_config), encoding="utf-8")

    assert apply_candidate(root, ProbeCandidate("spawns", max_parallel_spawns=8)) == []
    assert yaml.safe_load(child.read_text(encoding="utf-8"))["tools"]["max_parallel_spawns"] == 4


def _satisfy_candidate_config(config: dict[str, object], candidate: ProbeCandidate) -> None:
    agent = cast(dict[str, object], config["agent"])
    memory = cast(dict[str, object], config["memory"])
    llm = cast(dict[str, object], config["llm"])
    tools = cast(dict[str, object], config["tools"])
    if candidate.delegated_llm_max_tokens_floor is not None:
        llm["max_tokens"] = candidate.delegated_llm_max_tokens_floor
    if candidate.step_budget is not None:
        agent["max_steps"] = candidate.step_budget
    if candidate.retrieval_top_k is not None:
        memory["top_k"] = candidate.retrieval_top_k
    if candidate.retrieval_strategy is not None:
        memory["retrieval_strategy"] = candidate.retrieval_strategy
    if candidate.cost_ceiling is not None:
        agent["max_cost_usd"] = candidate.cost_ceiling / 2
    if candidate.role_model is not None:
        role, model = candidate.role_model
        if role == "main":
            llm["model"] = model
        else:
            llm[role] = {"model": model}
    if candidate.max_parallel_spawns is not None:
        tools["max_parallel_spawns"] = candidate.max_parallel_spawns


@pytest.mark.parametrize(
    "candidate",
    [
        ProbeCandidate("tokens", delegated_llm_max_tokens_floor=256),
        ProbeCandidate("step", step_budget=20),
        ProbeCandidate("top-k", retrieval_top_k=9),
        ProbeCandidate("strategy", retrieval_strategy="dense"),
        ProbeCandidate("cost", cost_ceiling=0.5),
        ProbeCandidate("model", role_model=("main", "gpt-new")),
        ProbeCandidate("spawns", max_parallel_spawns=8),
        ProbeCandidate("timeout", delegation_timeout=300.0),
    ],
)
def test_probe_rejects_each_satisfied_candidate_without_a_model_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate: ProbeCandidate,
) -> None:
    dataset, roster_root = _write_dataset_and_company(tmp_path, replicas=1)
    monkeypatch.setenv("FABRI_ROSTERS_ROOT", str(roster_root))
    case = replace(load_probe_case(dataset, "support"), candidates=(candidate,))

    def compile_only(
        argv: list[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env, timeout_s
        if argv[0] == "git":
            if "--show-toplevel" in argv:
                return subprocess.CompletedProcess(argv, 0, f"{roster_root}\n", "")
            if "HEAD" in argv:
                return subprocess.CompletedProcess(argv, 0, "test-roster-sha\n", "")
            if "status" in argv:
                return subprocess.CompletedProcess(argv, 0, "", "")
            raise AssertionError(f"unexpected git command: {argv}")
        if "compile" not in argv:
            raise AssertionError("a no-op candidate must not start a model run")
        destination = Path(argv[argv.index("--dest") + 1]) / "support-hq"
        child = destination / "agencies" / "crew" / "agent.yaml"
        child.parent.mkdir(parents=True)
        child_config = _agent_config("crew", max_tokens=60)
        _satisfy_candidate_config(child_config, candidate)
        child.write_text(yaml.safe_dump(child_config), encoding="utf-8")
        root_config = _agent_config("ceo", max_tokens=1024, child=child)
        if candidate.max_parallel_spawns is not None:
            cast(dict[str, object], root_config["tools"])["max_parallel_spawns"] = candidate.max_parallel_spawns
        if candidate.delegation_timeout is not None:
            cast(list[dict[str, object]], cast(dict[str, object], root_config["tools"])["agents"])[0]["timeout_s"] = candidate.delegation_timeout
        (destination / "ceo.yaml").write_text(yaml.safe_dump(root_config), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "compiled", "")

    result = run_probe(case, tmp_path / "results", command_runner=compile_only)
    run = cast(list[dict[str, object]], cast(list[dict[str, object]], result["candidates"])[0]["runs"])[0]
    assert run["failure_reasons"] == ["candidate_noop"]
    assert cast(list[dict[str, object]], result["candidates"])[0]["model_runs"] == 0


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


def test_score_text_ignores_forbidden_phrase_in_negated_context() -> None:
    assert score_text(
        "There is no evidence that a fix was deployed.",
        (("evidence",),),
        ("fix was deployed",),
    ) == {"passed": True, "missing": [], "forbidden": []}


@pytest.mark.parametrize(
    ("text", "expected_forbidden"),
    [
        (
            "No crew supplied evidence that a corrective fix was deployed.",
            [],
        ),
        (
            "…not that a corrected checkout fix was deployed.",
            [],
        ),
        (
            "The fix was deployed to production.",
            ["fix was deployed"],
        ),
        (
            "No issues were found. The fix was deployed.",
            ["fix was deployed"],
        ),
    ],
)
def test_score_text_scopes_negation_to_the_current_sentence(
    text: str, expected_forbidden: list[str]
) -> None:
    assert score_text(text, (), ("fix was deployed",))["forbidden"] == expected_forbidden


def test_score_text_flags_plain_forbidden_phrase() -> None:
    assert score_text(
        "The fix was deployed to production.",
        (("fix",),),
        ("fix was deployed",),
    ) == {"passed": False, "missing": [], "forbidden": ["fix was deployed"]}


def test_score_text_flags_mixed_negated_and_plain_forbidden_occurrences() -> None:
    text = (
        "There is no evidence that a fix was deployed. "
        "Later, operations confirmed the production rollout during the scheduled maintenance window. "
        "The fix was deployed."
    )

    assert score_text(text, (("fix",),), ("fix was deployed",)) == {
        "passed": False,
        "missing": [],
        "forbidden": ["fix was deployed"],
    }


def test_score_text_does_not_flag_absent_forbidden_phrase() -> None:
    assert score_text(
        "There is no evidence that a deployment occurred.",
        (("evidence",),),
        ("fix was deployed",),
    ) == {"passed": True, "missing": [], "forbidden": []}


def test_score_structured_uses_recursive_subset_equality() -> None:
    expected = {"rollback": True, "follow_up": {"owner": "ops"}}

    assert score_structured(
        {"rollback": True, "follow_up": {"owner": "ops", "extra": "allowed"}},
        expected,
    ) == {"passed": True, "mismatches": []}
    assert score_structured({"rollback": False}, expected) == {
        "passed": False,
        "mismatches": ["wrong:rollback", "missing:follow_up"],
    }
    assert score_structured(None, expected) == {
        "passed": False,
        "mismatches": ["structured_output:not_a_mapping"],
    }
    assert score_structured(None, {}) == {"passed": True, "mismatches": []}
    # bool/int must not false-pass via Python's True == 1 / False == 0.
    assert score_structured({"rollback": 1}, {"rollback": True}) == {
        "passed": False,
        "mismatches": ["wrong:rollback"],
    }
    assert score_structured({"rollback": True}, {"rollback": 1}) == {
        "passed": False,
        "mismatches": ["wrong:rollback"],
    }


def _complete_probe_runner(
    roster_root: Path,
    structured_output: object,
) -> Callable[[list[str], Path, Mapping[str, str], float], subprocess.CompletedProcess[str]]:
    run_number = 0

    def runner(
        argv: list[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal run_number
        del cwd, timeout_s
        if argv[0] == "git":
            if "--show-toplevel" in argv:
                return subprocess.CompletedProcess(argv, 0, f"{roster_root}\n", "")
            if "HEAD" in argv:
                return subprocess.CompletedProcess(argv, 0, "test-roster-sha\n", "")
            if "status" in argv:
                return subprocess.CompletedProcess(argv, 0, "", "")
            raise AssertionError(f"unexpected git command: {argv}")
        if "compile" in argv:
            destination = Path(argv[argv.index("--dest") + 1]) / "support-hq"
            child = destination / "agencies" / "crew" / "agent.yaml"
            child.parent.mkdir(parents=True)
            child.write_text(yaml.safe_dump(_agent_config("crew", max_tokens=256)), encoding="utf-8")
            (destination / "ceo.yaml").write_text(
                yaml.safe_dump(_agent_config("ceo", max_tokens=1024, child=child)),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "compiled", "")

        run_number += 1
        state_root = Path(env["FABRI_HOME"])
        traces = state_root / ".fabri" / "traces"
        traces.mkdir(parents=True)
        root_session = f"root-{run_number}"
        child_session = f"child-{run_number}"
        (traces / f"{child_session}.jsonl").write_text(
            json.dumps({"type": "final", "outcome": "success"})
            + "\n"
            + json.dumps({"type": "usage", "cost_usd": 0.02})
            + "\n",
            encoding="utf-8",
        )
        (traces / f"{root_session}.jsonl").write_text(
            json.dumps(
                {
                    "type": "tool_call",
                    "name": "crew",
                    "result": {
                        "ok": True,
                        "result": {"session_id": child_session, "outcome": "success"},
                    },
                }
            )
            + "\n"
            + json.dumps({"type": "final", "outcome": "success"})
            + "\n"
            + json.dumps({"type": "usage", "cost_usd": 0.01})
            + "\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "session_id": root_session,
                    "success": True,
                    "final_text": "Checkout complete",
                    "outcome": "success",
                    "structured_output": structured_output,
                }
            ),
            "",
        )

    return runner


def test_probe_structured_assertions_gate_end_to_end_and_preserve_unconfigured_cases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, roster_root = _write_dataset_and_company(tmp_path, replicas=1)
    monkeypatch.setenv("FABRI_ROSTERS_ROOT", str(roster_root))
    loaded = load_probe_case(dataset, "support")
    structured_case = replace(
        loaded,
        candidates=(loaded.candidates[0],),
        structured_fields={"rollback": True},
    )
    structured_result = run_probe(
        structured_case,
        tmp_path / "structured-results",
        command_runner=_complete_probe_runner(roster_root, {"rollback": False}),
    )
    structured_candidate = cast(list[dict[str, object]], structured_result["candidates"])[0]
    structured_run = cast(list[dict[str, object]], structured_candidate["runs"])[0]
    assert structured_run["attempt_status"] == "complete"
    assert structured_run["rubric_passed"] is True
    assert structured_run["structured_passed"] is False
    assert structured_run["structured_mismatches"] == ["wrong:rollback"]
    assert structured_run["end_to_end_passed"] is False
    assert structured_candidate["qualifies"] is False

    backward_compatible_case = replace(loaded, candidates=(loaded.candidates[0],))
    backward_compatible_result = run_probe(
        backward_compatible_case,
        tmp_path / "legacy-results",
        command_runner=_complete_probe_runner(roster_root, None),
    )
    legacy_run = cast(
        list[dict[str, object]],
        cast(list[dict[str, object]], backward_compatible_result["candidates"])[0]["runs"],
    )[0]
    assert legacy_run["structured_passed"] is True
    assert legacy_run["end_to_end_passed"] is True


def test_probe_optional_judge_is_injectable_and_advisory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, roster_root = _write_dataset_and_company(tmp_path, replicas=1)
    monkeypatch.setenv("FABRI_ROSTERS_ROOT", str(roster_root))
    loaded = load_probe_case(dataset, "support")
    case = replace(loaded, candidates=(loaded.candidates[0],))
    calls = 0

    def fake_judge(config: JudgeConfig, final_text: str) -> JudgeResult:
        nonlocal calls
        calls += 1
        assert config.model == "gpt-4o-mini"
        assert final_text == "Checkout complete"
        return JudgeResult(
            verdict="fail",
            rationale="canned semantic failure",
            usage=LLMUsage(input_tokens=100, output_tokens=20, model=config.model),
        )

    judge_off = run_probe(
        case,
        tmp_path / "judge-off",
        command_runner=_complete_probe_runner(roster_root, None),
        judge_runner=fake_judge,
    )
    judge_off_run = cast(
        list[dict[str, object]], cast(list[dict[str, object]], judge_off["candidates"])[0]["runs"]
    )[0]
    assert calls == 0
    assert judge_off_run["judge"] is None
    assert judge_off_run["end_to_end_passed"] is True

    judged_case = replace(
        case,
        judge=JudgeConfig(
            provider="openai",
            model="gpt-4o-mini",
            api_key_env="OPENAI_API_KEY",
            criteria=("The response confirms checkout completion.",),
        ),
    )
    judge_on = run_probe(
        judged_case,
        tmp_path / "judge-on",
        command_runner=_complete_probe_runner(roster_root, None),
        judge_runner=fake_judge,
    )
    judged_candidate = cast(list[dict[str, object]], judge_on["candidates"])[0]
    judged_run = cast(list[dict[str, object]], judged_candidate["runs"])[0]
    assert calls == 1
    assert judged_run["judge"] == {
        "verdict": "fail",
        "rationale": "canned semantic failure",
        "model": "gpt-4o-mini",
        "prompt_version": "v1",
        "cost_usd": pytest.approx(0.000027),
    }
    assert judged_run["judge_cost_usd"] == pytest.approx(0.000027)
    assert judged_run["total_cost_usd"] == pytest.approx(0.03)
    assert judged_run["end_to_end_passed"] is True
    assert judged_candidate["qualifies"] is True
    assert judged_candidate["judge_pass_rate"] == 0
    assert judge_on["judge_model"] == "gpt-4o-mini"
    assert judge_on["judge_prompt_version"] == "v1"
    assert judge_on["judge_temperature"] == 0.0
    validate_publication_payload(judge_on)


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
        if argv[0] == "git":
            if "--show-toplevel" in argv:
                return subprocess.CompletedProcess(argv, 0, f"{roster_root}\n", "")
            if "HEAD" in argv:
                return subprocess.CompletedProcess(argv, 0, "test-roster-sha\n", "")
            if "status" in argv:
                return subprocess.CompletedProcess(argv, 0, "", "")
            raise AssertionError(f"unexpected git command: {argv}")
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
    assert candidate["configured_replicas"] == 1
    assert candidate["preflights"] == 1
    assert candidate["model_runs"] == 0
    assert candidate["decision"] == "candidate_noop"
    assert isinstance(result["fabri_version"], str)
    source = cast(dict[str, object], result["source"])
    assert source["roster_revision"] == "test-roster-sha"
    assert source["roster_worktree_clean"] is True
    company_source_sha256 = cast(str, source["company_source_sha256"])
    assert len(company_source_sha256) == 64
    assert int(company_source_sha256, 16) >= 0
    assert result["claim_boundary"] == CLAIM_BOUNDARY
    assert "released_gate_cost_usd" in result
    assert "total_research_spend_usd" in result
    markdown = render_markdown(result)
    assert "test-roster-sha" in markdown
    assert CLAIM_BOUNDARY in markdown


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
        if argv[0] == "git":
            if "--show-toplevel" in argv:
                return subprocess.CompletedProcess(argv, 0, f"{roster_root}\n", "")
            if "HEAD" in argv:
                return subprocess.CompletedProcess(argv, 0, "test-roster-sha\n", "")
            if "status" in argv:
                return subprocess.CompletedProcess(argv, 0, "", "")
            raise AssertionError(f"unexpected git command: {argv}")
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


def _valid_publication_payload() -> dict[str, object]:
    return {
        "study": "company-setup-qualification",
        "generated_at": "2026-07-20T00:00:00+00:00",
        "case_id": "support",
        "company": "support-hq",
        "fabri_version": "0.18.5",
        "source": {
            "path": "companies/support-hq/company.toml",
            "roster_revision": "test-roster-sha",
            "roster_worktree_clean": True,
            "company_source_sha256": "a" * 64,
        },
        "replicas_per_candidate": 1,
        "selection_policy": "test policy",
        "candidates": [
            {
                "id": "baseline",
                "overrides": {},
                "configured_replicas": 1,
                "scheduled_replicas": 1,
                "preflights": 1,
                "model_runs": 1,
                "completion_rate": 1.0,
                "conditional_rubric_pass_rate": 1.0,
                "end_to_end_pass_rate": 1.0,
                "median_total_cost_usd": 0.01,
                "qualifies": True,
                "runs": [],
            }
        ],
        "recommendation": "baseline",
        "status": "qualified",
        "released_gate_cost_usd": 0.01,
        "total_research_spend_usd": 0.01,
        "claim_boundary": CLAIM_BOUNDARY,
    }


def test_validate_publication_payload_rejects_missing_and_wrong_typed_fields() -> None:
    missing = _valid_publication_payload()
    del missing["source"]
    with pytest.raises(ProbeError, match="missing required key: source"):
        validate_publication_payload(missing)

    wrong_typed = _valid_publication_payload()
    wrong_typed["replicas_per_candidate"] = "one"
    with pytest.raises(ProbeError, match="replicas_per_candidate.*invalid type"):
        validate_publication_payload(wrong_typed)

    top_level_bool = _valid_publication_payload()
    top_level_bool["replicas_per_candidate"] = True
    with pytest.raises(ProbeError, match="replicas_per_candidate.*invalid type"):
        validate_publication_payload(top_level_bool)

    candidate_bool = _valid_publication_payload()
    candidate_bool_candidates = cast(list[dict[str, object]], candidate_bool["candidates"])
    candidate_bool_candidates[0]["model_runs"] = True
    with pytest.raises(ProbeError, match="model_runs.*invalid type"):
        validate_publication_payload(candidate_bool)

    legitimate_bools = _valid_publication_payload()
    legitimate_bools_source = cast(dict[str, object], legitimate_bools["source"])
    legitimate_bools_candidates = cast(
        list[dict[str, object]], legitimate_bools["candidates"]
    )
    legitimate_bools_source["roster_worktree_clean"] = True
    legitimate_bools_candidates[0]["qualifies"] = True
    validate_publication_payload(legitimate_bools)


def test_published_setup_qualification_payload_matches_publication_schema() -> None:
    published_result = (
        Path(__file__).resolve().parents[1]
        / "benchmarks/results/support-hq-setup-qualification-2026-07-20.json"
    )

    with published_result.open(encoding="utf-8") as result_file:
        payload = json.load(result_file)

    validate_publication_payload(payload)


def test_render_markdown_reads_manifest_and_claim_boundary_from_payload() -> None:
    payload = _valid_publication_payload()

    markdown = render_markdown(payload)

    assert "test-roster-sha" in markdown
    assert CLAIM_BOUNDARY in markdown


def test_probe_continues_when_roster_git_commands_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, roster_root = _write_dataset_and_company(tmp_path, replicas=1)
    monkeypatch.setenv("FABRI_ROSTERS_ROOT", str(roster_root))
    loaded = load_probe_case(dataset, "support")
    case = replace(loaded, candidates=(loaded.candidates[1],))

    def git_absent_runner(
        argv: list[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env
        if argv[0] == "git":
            assert timeout_s == MANIFEST_GIT_TIMEOUT_S
            raise FileNotFoundError("git")
        if "compile" not in argv:
            raise AssertionError("a no-op candidate must not start a model run")
        destination = Path(argv[argv.index("--dest") + 1]) / "support-hq"
        child = destination / "agencies" / "crew" / "agent.yaml"
        child.parent.mkdir(parents=True)
        child.write_text(
            yaml.safe_dump(_agent_config("crew", max_tokens=256)),
            encoding="utf-8",
        )
        (destination / "ceo.yaml").write_text(
            yaml.safe_dump(_agent_config("ceo", max_tokens=1024, child=child)),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, "compiled", "")

    output = tmp_path / "results"
    result = run_probe(case, output, command_runner=git_absent_runner)

    source = cast(dict[str, object], result["source"])
    assert source["roster_revision"] is None
    assert source["roster_worktree_clean"] is None
    company_source_sha256 = cast(str, source["company_source_sha256"])
    assert len(company_source_sha256) == 64
    assert int(company_source_sha256, 16) >= 0
    assert (output / "results.json").is_file()


@pytest.mark.parametrize("failed_command", ["HEAD", "status"])
def test_probe_degrades_when_a_later_git_command_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_command: str,
) -> None:
    dataset, roster_root = _write_dataset_and_company(tmp_path, replicas=1)
    monkeypatch.setenv("FABRI_ROSTERS_ROOT", str(roster_root))
    loaded = load_probe_case(dataset, "support")
    case = replace(loaded, candidates=(loaded.candidates[1],))

    def partial_git_failure_runner(
        argv: list[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env
        if argv[0] == "git":
            assert timeout_s == MANIFEST_GIT_TIMEOUT_S
            if "--show-toplevel" in argv:
                return subprocess.CompletedProcess(argv, 0, f"{roster_root}\n", "")
            if "HEAD" in argv:
                return subprocess.CompletedProcess(
                    argv,
                    128 if failed_command == "HEAD" else 0,
                    "" if failed_command == "HEAD" else "test-roster-sha\n",
                    "git failure" if failed_command == "HEAD" else "",
                )
            if "status" in argv:
                return subprocess.CompletedProcess(
                    argv,
                    128 if failed_command == "status" else 0,
                    "",
                    "git failure" if failed_command == "status" else "",
                )
            raise AssertionError(f"unexpected git command: {argv}")
        if "compile" not in argv:
            raise AssertionError("a no-op candidate must not start a model run")
        destination = Path(argv[argv.index("--dest") + 1]) / "support-hq"
        child = destination / "agencies" / "crew" / "agent.yaml"
        child.parent.mkdir(parents=True)
        child.write_text(
            yaml.safe_dump(_agent_config("crew", max_tokens=256)),
            encoding="utf-8",
        )
        (destination / "ceo.yaml").write_text(
            yaml.safe_dump(_agent_config("ceo", max_tokens=1024, child=child)),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, "compiled", "")

    result = run_probe(
        case,
        tmp_path / f"results-{failed_command}",
        command_runner=partial_git_failure_runner,
    )

    source = cast(dict[str, object], result["source"])
    assert source["roster_revision"] is None
    assert source["roster_worktree_clean"] is None
    company_source_sha256 = cast(str, source["company_source_sha256"])
    assert len(company_source_sha256) == 64
    assert int(company_source_sha256, 16) >= 0


def test_probe_marks_compile_failure_without_starting_model_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, roster_root = _write_dataset_and_company(tmp_path, replicas=1)
    monkeypatch.setenv("FABRI_ROSTERS_ROOT", str(roster_root))
    loaded = load_probe_case(dataset, "support")
    case = replace(loaded, candidates=(loaded.candidates[0],))
    run_attempted = False

    def compile_failure_runner(
        argv: list[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal run_attempted
        del cwd, env, timeout_s
        if argv[0] == "git":
            raise FileNotFoundError("git")
        if "compile" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "compile failed")
        run_attempted = True
        raise AssertionError(f"run subprocess must not start after compile failure: {argv}")

    result = run_probe(
        case,
        tmp_path / "results",
        command_runner=compile_failure_runner,
    )

    candidate = cast(list[dict[str, object]], result["candidates"])[0]
    run = cast(list[dict[str, object]], candidate["runs"])[0]
    assert run_attempted is False
    assert run["attempt_status"] == "invalid_measurement"
    assert run["failure_reasons"] == ["company_compile_failed"]
    assert run["total_cost_usd"] is None


def test_probe_marks_preflight_failure_as_invalid_measurement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, roster_root = _write_dataset_and_company(tmp_path, replicas=1)
    monkeypatch.setenv("FABRI_ROSTERS_ROOT", str(roster_root))
    loaded = load_probe_case(dataset, "support")
    case = replace(loaded, candidates=(loaded.candidates[0],))

    def malformed_preflight_runner(
        argv: list[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env, timeout_s
        if argv[0] == "git":
            raise FileNotFoundError("git")
        if "compile" not in argv:
            raise AssertionError(f"run subprocess must not start after preflight failure: {argv}")
        destination = Path(argv[argv.index("--dest") + 1]) / "support-hq"
        destination.mkdir(parents=True)
        (destination / "ceo.yaml").write_text("not: [valid\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "compiled", "")

    result = run_probe(
        case,
        tmp_path / "results",
        command_runner=malformed_preflight_runner,
    )

    candidate = cast(list[dict[str, object]], result["candidates"])[0]
    run = cast(list[dict[str, object]], candidate["runs"])[0]
    assert run["attempt_status"] == "invalid_measurement"
    assert run["failure_reasons"] == ["preflight_failed"]
    assert run["total_cost_usd"] is None


def test_probe_marks_root_process_timeout_as_operational_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, roster_root = _write_dataset_and_company(tmp_path, replicas=1)
    monkeypatch.setenv("FABRI_ROSTERS_ROOT", str(roster_root))
    loaded = load_probe_case(dataset, "support")
    case = replace(loaded, candidates=(loaded.candidates[0],))

    def timeout_runner(
        argv: list[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env
        if argv[0] == "git":
            raise FileNotFoundError("git")
        if "compile" in argv:
            destination = Path(argv[argv.index("--dest") + 1]) / "support-hq"
            child = destination / "agencies" / "crew" / "agent.yaml"
            child.parent.mkdir(parents=True)
            child.write_text(
                yaml.safe_dump(_agent_config("crew", max_tokens=60)), encoding="utf-8"
            )
            (destination / "ceo.yaml").write_text(
                yaml.safe_dump(_agent_config("ceo", max_tokens=1024, child=child)),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "compiled", "")
        raise subprocess.TimeoutExpired(argv, timeout=timeout_s)

    result = run_probe(case, tmp_path / "results", command_runner=timeout_runner)

    candidate = cast(list[dict[str, object]], result["candidates"])[0]
    run = cast(list[dict[str, object]], candidate["runs"])[0]
    assert run["attempt_status"] == "operational_failure"
    assert run["failure_reasons"] == ["root_process_timeout"]


def test_probe_marks_malformed_run_json_as_invalid_measurement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, roster_root = _write_dataset_and_company(tmp_path, replicas=1)
    monkeypatch.setenv("FABRI_ROSTERS_ROOT", str(roster_root))
    loaded = load_probe_case(dataset, "support")
    case = replace(loaded, candidates=(loaded.candidates[0],))

    def malformed_json_runner(
        argv: list[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env, timeout_s
        if argv[0] == "git":
            raise FileNotFoundError("git")
        if "compile" in argv:
            destination = Path(argv[argv.index("--dest") + 1]) / "support-hq"
            child = destination / "agencies" / "crew" / "agent.yaml"
            child.parent.mkdir(parents=True)
            child.write_text(
                yaml.safe_dump(_agent_config("crew", max_tokens=60)), encoding="utf-8"
            )
            (destination / "ceo.yaml").write_text(
                yaml.safe_dump(_agent_config("ceo", max_tokens=1024, child=child)),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "compiled", "")
        return subprocess.CompletedProcess(argv, 0, "not valid json", "")

    result = run_probe(case, tmp_path / "results", command_runner=malformed_json_runner)

    candidate = cast(list[dict[str, object]], result["candidates"])[0]
    run = cast(list[dict[str, object]], candidate["runs"])[0]
    assert run["attempt_status"] == "invalid_measurement"
    assert run["failure_reasons"] == ["unreadable_run_result"]


def test_probe_marks_missing_trace_as_incomplete_operational_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, roster_root = _write_dataset_and_company(tmp_path, replicas=1)
    monkeypatch.setenv("FABRI_ROSTERS_ROOT", str(roster_root))
    loaded = load_probe_case(dataset, "support")
    case = replace(
        loaded,
        candidates=(loaded.candidates[0],),
        required_delegations=(),
    )

    def missing_trace_runner(
        argv: list[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env, timeout_s
        if argv[0] == "git":
            raise FileNotFoundError("git")
        if "compile" in argv:
            destination = Path(argv[argv.index("--dest") + 1]) / "support-hq"
            child = destination / "agencies" / "crew" / "agent.yaml"
            child.parent.mkdir(parents=True)
            child.write_text(
                yaml.safe_dump(_agent_config("crew", max_tokens=60)), encoding="utf-8"
            )
            (destination / "ceo.yaml").write_text(
                yaml.safe_dump(_agent_config("ceo", max_tokens=1024, child=child)),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "compiled", "")
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "session_id": "root",
                    "success": True,
                    "outcome": "success",
                    "final_text": "Checkout update",
                }
            ),
            "",
        )

    result = run_probe(case, tmp_path / "results", command_runner=missing_trace_runner)

    candidate = cast(list[dict[str, object]], result["candidates"])[0]
    run = cast(list[dict[str, object]], candidate["runs"])[0]
    assert run["attempt_status"] == "operational_failure"
    assert run["rubric_passed"] is None
    assert run["failure_reasons"] == ["missing_cost_usage", "missing_trace"]


def test_probe_rejects_completed_run_over_company_cost_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, roster_root = _write_dataset_and_company(tmp_path, replicas=1)
    monkeypatch.setenv("FABRI_ROSTERS_ROOT", str(roster_root))
    loaded = load_probe_case(dataset, "support")
    case = replace(
        loaded,
        candidates=(loaded.candidates[0],),
        company_max_cost_usd=0.01,
        required_delegations=(),
    )

    def over_cost_runner(
        argv: list[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout_s
        if argv[0] == "git":
            raise FileNotFoundError("git")
        if "compile" in argv:
            destination = Path(argv[argv.index("--dest") + 1]) / "support-hq"
            child = destination / "agencies" / "crew" / "agent.yaml"
            child.parent.mkdir(parents=True)
            child.write_text(
                yaml.safe_dump(_agent_config("crew", max_tokens=60)), encoding="utf-8"
            )
            (destination / "ceo.yaml").write_text(
                yaml.safe_dump(_agent_config("ceo", max_tokens=1024, child=child)),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "compiled", "")
        traces = Path(env["FABRI_HOME"]) / ".fabri" / "traces"
        traces.mkdir(parents=True)
        (traces / "root.jsonl").write_text(
            "\n".join(
                json.dumps(event)
                for event in [
                    {"type": "final", "outcome": "success"},
                    {"type": "usage", "cost_usd": 0.02},
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "session_id": "root",
                    "success": True,
                    "outcome": "success",
                    "final_text": "Checkout update",
                }
            ),
            "",
        )

    result = run_probe(case, tmp_path / "results", command_runner=over_cost_runner)

    candidate = cast(list[dict[str, object]], result["candidates"])[0]
    run = cast(list[dict[str, object]], candidate["runs"])[0]
    assert run["attempt_status"] == "complete"
    assert run["failure_reasons"] == ["company_cost_limit_exceeded"]
    assert run["within_cost_limit"] is False
    assert run["end_to_end_passed"] is False


def test_probe_reports_no_viable_setup_when_all_candidates_fail_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, roster_root = _write_dataset_and_company(tmp_path, replicas=1)
    monkeypatch.setenv("FABRI_ROSTERS_ROOT", str(roster_root))
    case = load_probe_case(dataset, "support")
    run_number = 0

    def rubric_failure_runner(
        argv: list[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal run_number
        del cwd, timeout_s
        if argv[0] == "git":
            raise FileNotFoundError("git")
        if "compile" in argv:
            destination = Path(argv[argv.index("--dest") + 1]) / "support-hq"
            child = destination / "agencies" / "crew" / "agent.yaml"
            child.parent.mkdir(parents=True)
            child.write_text(
                yaml.safe_dump(_agent_config("crew", max_tokens=60)), encoding="utf-8"
            )
            (destination / "ceo.yaml").write_text(
                yaml.safe_dump(_agent_config("ceo", max_tokens=1024, child=child)),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "compiled", "")
        run_number += 1
        traces = Path(env["FABRI_HOME"]) / ".fabri" / "traces"
        traces.mkdir(parents=True)
        session_id = f"root-{run_number}"
        (traces / f"{session_id}.jsonl").write_text(
            "\n".join(
                json.dumps(event)
                for event in [
                    {"type": "final", "outcome": "success"},
                    {"type": "usage", "cost_usd": 0.01},
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "session_id": session_id,
                    "success": True,
                    "outcome": "success",
                    "final_text": "No actionable response",
                }
            ),
            "",
        )

    result = run_probe(
        replace(case, required_delegations=()),
        tmp_path / "results",
        command_runner=rubric_failure_runner,
    )

    assert run_number == 2
    assert result["status"] == "no_viable_setup"
    assert result["recommendation"] is None
    candidates = cast(list[dict[str, object]], result["candidates"])
    assert all(candidate["end_to_end_pass_rate"] == 0 for candidate in candidates)


def test_main_returns_status_exit_codes_and_rejects_invalid_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, roster_root = _write_dataset_and_company(tmp_path)
    monkeypatch.setenv("FABRI_ROSTERS_ROOT", str(roster_root))

    def fake_run_probe(
        case: object,
        output_dir: object,
        *,
        run_timeout_s: float,
    ) -> dict[str, object]:
        assert isinstance(case, company_setup_probe.ProbeCase)
        assert output_dir == str(tmp_path / "results")
        assert run_timeout_s == pytest.approx(1.5)
        return {"status": "qualified", "recommendation": "baseline"}

    monkeypatch.setattr(company_setup_probe, "run_probe", fake_run_probe)
    assert (
        main(
            [
                "--dataset",
                str(dataset),
                "--case",
                "support",
                "--output-dir",
                str(tmp_path / "results"),
                "--run-timeout-s",
                "1.5",
            ]
        )
        == 0
    )

    monkeypatch.setattr(
        company_setup_probe,
        "run_probe",
        lambda case, output_dir, *, run_timeout_s: {
            "status": "no_viable_setup",
            "recommendation": None,
        },
    )
    assert (
        main(
            [
                "--dataset",
                str(dataset),
                "--case",
                "support",
                "--output-dir",
                str(tmp_path / "results"),
            ]
        )
        == 1
    )

    with pytest.raises(SystemExit) as invalid_timeout:
        main(
            [
                "--dataset",
                str(dataset),
                "--case",
                "support",
                "--output-dir",
                str(tmp_path / "results"),
                "--run-timeout-s",
                "0",
            ]
        )
    assert invalid_timeout.value.code == 2

    with pytest.raises(SystemExit) as missing_required_argument:
        main(["--dataset", str(dataset)])
    assert missing_required_argument.value.code == 2
