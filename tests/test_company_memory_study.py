from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
import yaml

from fabri.benchmarks.company_memory_study import (
    CLAIM_BOUNDARY,
    _invalid_run,
    apply_holdout_response_contract,
    apply_retrieval_overrides,
    discover_memory_dbs,
    load_memory_case,
    main,
    render_markdown,
    run_memory_study,
    validate_memory_payload,
)
from fabri.benchmarks.company_setup_probe import ProbeError


pytestmark = pytest.mark.integration


def _write_case(tmp_path: Path, *, replicas: int = 1) -> tuple[Path, Path]:
    roster_root = tmp_path / "rosters"
    company_dir = roster_root / "companies" / "support-hq"
    company_dir.mkdir(parents=True)
    (company_dir / "company.toml").write_text(
        """
[company]
name = "support-hq"
memory_namespace = "support_hq"

[[node]]
id = "ceo"
report_to = ""

[[node]]
id = "crew"
report_to = "ceo"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    dataset = tmp_path / "dataset.yaml"
    dataset.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "defaults": {
                    "roster_root_env": "FABRI_ROSTERS_ROOT",
                    "replicas": replicas,
                    "conditions": ["memory", "control"],
                },
                "cases": [
                    {
                        "id": "support",
                        "company_source": "companies/support-hq/company.toml",
                        "training_prompt": "Train privately.",
                        "holdout_prompt": "Hold out privately.",
                        "expected": {
                            "required": [["checkout"], ["rollback", "rolled back"]],
                            "structured": {"decision": "READY"},
                            "forbidden": ["blame"],
                        },
                        "legacy_expected": {
                            "required": [["checkout"], ["rollback", "rolled back"]],
                            "forbidden": ["legacy-only"],
                        },
                        "response_schema": {
                            "type": "object",
                            "required": ["response", "decision"],
                            "properties": {
                                "response": {"type": "string"},
                                "decision": {"type": "string"},
                            },
                        },
                        "setup_probe": {"required_delegations": ["crew"]},
                        "retrieval_expectations": {"useful_lesson": "be factual"},
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return dataset, roster_root


class FakeRunner:
    def __init__(
        self,
        roster_root: Path,
        *,
        incomplete_holdout: str | None = None,
        failed_training: str | None = None,
        truncated_training: str | None = None,
        structured_outputs: Mapping[str, object] | None = None,
    ) -> None:
        self.roster_root = roster_root
        self.incomplete_holdout = incomplete_holdout
        self.failed_training = failed_training
        self.truncated_training = truncated_training
        self.structured_outputs = structured_outputs
        self.compile_destinations: list[Path] = []
        self.holdout_db_before_run: dict[str, bytes | None] = {}
        self.holdout_dbs_before_run: dict[str, dict[str, bytes | None]] = {}
        self.run_stages: list[tuple[str, str]] = []
        self.retrieval_configs_before_run: dict[tuple[str, str], dict[str, object]] = {}
        self.response_contracts_before_run: dict[tuple[str, str], dict[str, object]] = {}

    def __call__(
        self,
        argv: list[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout_s
        if argv[0] == "git":
            if "--show-toplevel" in argv:
                return subprocess.CompletedProcess(argv, 0, f"{self.roster_root}\n", "")
            if "HEAD" in argv:
                return subprocess.CompletedProcess(argv, 0, "test-roster-sha\n", "")
            if "status" in argv:
                return subprocess.CompletedProcess(argv, 0, "", "")
            raise AssertionError(f"unexpected git command: {argv}")
        if "compile" in argv:
            destination = Path(argv[argv.index("--dest") + 1])
            self.compile_destinations.append(destination)
            company = destination / "support-hq"
            company.mkdir(parents=True)
            manager_db = company / ".fabri" / "support_hq.db"
            specialist_db = company / ".fabri" / "support_hq_crew.db"
            (company / "ceo.yaml").write_text(
                yaml.safe_dump({
                    "agent": {"name": "ceo", "system_prompt": "Delegate and summarize."},
                    "memory": {
                        "backend": "sqlite", "top_k": 5,
                        "sqlite_path": str(manager_db),
                    },
                }, sort_keys=False),
                encoding="utf-8",
            )
            specialist_config = company / "agencies" / "crew" / "specialist.yaml"
            specialist_config.parent.mkdir(parents=True)
            specialist_config.write_text(
                yaml.safe_dump({
                    "agent": {"name": "specialist"},
                    "memory": {
                        "backend": "sqlite", "top_k": 5,
                        "sqlite_path": str(specialist_db),
                    },
                }, sort_keys=False),
                encoding="utf-8",
            )
            manager_db.parent.mkdir(parents=True)
            manager_db.write_bytes(f"manager:{destination.name}".encode())
            specialist_db.write_bytes(f"specialist:{destination.name}".encode())
            return subprocess.CompletedProcess(argv, 0, "compiled", "")

        root_config = Path(argv[argv.index("--config") + 1])
        stage = "training" if root_config.parents[1].name == "training-compiled" else "holdout"
        condition = root_config.parents[2].name
        self.run_stages.append((condition, stage))
        root_config_data = yaml.safe_load(root_config.read_text(encoding="utf-8"))
        assert isinstance(root_config_data, dict)
        memory = root_config_data.get("memory")
        assert isinstance(memory, dict)
        self.retrieval_configs_before_run[(condition, stage)] = memory
        agent = root_config_data.get("agent")
        assert isinstance(agent, dict)
        self.response_contracts_before_run[(condition, stage)] = agent
        database = root_config.parent / ".fabri" / "support_hq.db"
        if stage == "holdout":
            self.holdout_db_before_run[condition] = (
                database.read_bytes() if database.exists() else None
            )
            self.holdout_dbs_before_run[condition] = {
                name: (path.read_bytes() if path.exists() else None)
                for name, path in {
                    "manager": database,
                    "specialist": root_config.parent / ".fabri" / "support_hq_crew.db",
                }.items()
            }
        state_root = Path(env["FABRI_HOME"])
        traces = state_root / ".fabri" / "traces"
        traces.mkdir(parents=True)
        session = f"{condition}-{stage}"
        child_session = f"{session}-child"
        failed = stage == "training" and condition == self.failed_training
        truncated = stage == "training" and condition == self.truncated_training
        incomplete = stage == "holdout" and condition == self.incomplete_holdout
        outcome = "failed" if failed else "success"
        crew_result: dict[str, object] = {
            "ok": not failed and not truncated,
            "result": {"session_id": child_session, "outcome": outcome},
        }
        if truncated:
            # The agent self-reports success while a nested delegation was cut
            # short — the exact shape that used to leak ``training_success: true``
            # into an invalid, truncated run.
            crew_result["error"] = "response truncated: max_tokens reached"
        root_events: list[dict[str, object]] = [
            {"type": "tool_call", "name": "crew", "result": crew_result},
            {"type": "final" if not failed else "failed", "outcome": outcome},
        ]
        if not incomplete:
            root_events.extend(
                [
                    {
                        "type": "retrieval",
                        "candidates": ([{"kind": "guideline"}] if condition == "memory" else []),
                    },
                    {
                        "type": "usage",
                        "cost_usd": 0.01,
                        "guidelines_retrieved": (
                            2 if condition == "memory" and stage == "holdout" else 0
                        ),
                    },
                ]
            )
            (traces / f"{child_session}.jsonl").write_text(
                json.dumps({"type": "usage", "cost_usd": 0.02}) + "\n",
                encoding="utf-8",
            )
        (traces / f"{session}.jsonl").write_text(
            "\n".join(json.dumps(event) for event in root_events) + "\n",
            encoding="utf-8",
        )
        final_text = "checkout rollback" if condition == "memory" else "checkout rollback blame"
        structured_output: object = None
        if stage == "holdout":
            if self.structured_outputs is not None:
                structured_output = self.structured_outputs.get(condition)
            else:
                structured_output = {
                    "response": final_text,
                    "decision": "READY" if condition == "memory" else "UNKNOWN",
                }
        payload = {
            "session_id": session,
            "success": not failed,
            "final_text": final_text,
            "outcome": outcome,
            "structured_output": structured_output,
            "usage": {"cost_usd": 0.01},
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")


def test_memory_study_copies_every_declared_db_and_emits_safe_public_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, roster_root = _write_case(tmp_path)
    monkeypatch.setenv("FABRI_ROSTERS_ROOT", str(roster_root))
    case = load_memory_case(dataset, "support")
    runner = FakeRunner(roster_root)

    result = run_memory_study(case, tmp_path / "results", command_runner=runner)

    assert case.namespace == "support_hq"
    assert len(runner.compile_destinations) == 4
    assert len(set(runner.compile_destinations)) == 4
    assert runner.holdout_db_before_run["memory"] == b"manager:training-compiled"
    assert runner.holdout_db_before_run["control"] is None
    assert runner.holdout_dbs_before_run["memory"] == {
        "manager": b"manager:training-compiled",
        "specialist": b"specialist:training-compiled",
    }
    assert runner.holdout_dbs_before_run["control"] == {
        "manager": None,
        "specialist": None,
    }
    runs = cast(list[dict[str, object]], result["runs"])
    memory_run = next(run for run in runs if run["condition"] == "memory")
    control_run = next(run for run in runs if run["condition"] == "control")
    assert memory_run["rubric_passed"] is True
    assert control_run["rubric_passed"] is False
    assert memory_run["scoring_mode"] == "structured"
    assert control_run["guidelines_retrieved"] == 0
    assert memory_run["funnel"]["transport"]["intact"] is True
    assert control_run["funnel"]["transport"]["intact"] is True
    assert result["claim_boundary"] == CLAIM_BOUNDARY
    assert result["retrieval_overrides"] == {"top_k": None, "retrieval_strategy": None}
    assert all(
        config["top_k"] == 5 and config["backend"] == "sqlite"
        for config in runner.retrieval_configs_before_run.values()
    )
    for condition in ("memory", "control"):
        training_agent = runner.response_contracts_before_run[(condition, "training")]
        holdout_agent = runner.response_contracts_before_run[(condition, "holdout")]
        assert "response_schema" not in training_agent
        assert holdout_agent["response_schema"] == case.response_schema
        assert "READY" not in json.dumps(holdout_agent["response_schema"])
    public_text = (tmp_path / "results" / "results.json").read_text(encoding="utf-8")
    assert "Train privately" not in public_text
    assert "memory-training" not in public_text
    emitted = json.loads(public_text)
    assert all("structured_output" not in run for run in emitted["runs"])
    private_result = json.loads(
        (
            tmp_path
            / "results/private-attempts/replica-01/memory/private/result.json"
        ).read_text(encoding="utf-8")
    )
    assert private_result["structured_output"] == {
        "response": "checkout rollback",
        "decision": "READY",
    }
    validate_memory_payload(emitted)
    markdown = (tmp_path / "results" / "results.md").read_text(encoding="utf-8")
    assert markdown == render_markdown(emitted)


def test_load_memory_case_accepts_only_value_free_string_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, roster_root = _write_case(tmp_path)
    monkeypatch.setenv("FABRI_ROSTERS_ROOT", str(roster_root))

    case = load_memory_case(dataset, "support")

    assert case.structured_fields == {"decision": "READY"}
    assert case.legacy_required_terms == (("checkout",), ("rollback", "rolled back"))
    assert case.legacy_forbidden_terms == ("legacy-only",)
    assert case.response_schema == {
        "type": "object",
        "required": ["response", "decision"],
        "properties": {
            "response": {"type": "string"},
            "decision": {"type": "string"},
        },
    }
    assert "READY" not in json.dumps(case.response_schema)

    data = yaml.safe_load(dataset.read_text(encoding="utf-8"))
    data["cases"][0]["response_schema"]["properties"]["decision"]["enum"] = ["READY"]
    dataset.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ProbeError, match="must expose only type: string"):
        load_memory_case(dataset, "support")


def test_load_memory_case_rejects_holdout_prompt_leaking_structured_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, roster_root = _write_case(tmp_path)
    monkeypatch.setenv("FABRI_ROSTERS_ROOT", str(roster_root))

    data = yaml.safe_load(dataset.read_text(encoding="utf-8"))
    data["cases"][0]["holdout_prompt"] = "Hold out privately. The answer is READY."
    dataset.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(ProbeError, match="holdout_prompt leaks expected.structured"):
        load_memory_case(dataset, "support")


def test_load_memory_case_rejects_holdout_prompt_leaking_structured_value_case_insensitively(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, roster_root = _write_case(tmp_path)
    monkeypatch.setenv("FABRI_ROSTERS_ROOT", str(roster_root))

    data = yaml.safe_load(dataset.read_text(encoding="utf-8"))
    data["cases"][0]["holdout_prompt"] = "Hold out privately. It is ready to ship."
    dataset.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(ProbeError, match="holdout_prompt leaks expected.structured"):
        load_memory_case(dataset, "support")


def test_load_memory_case_allows_structured_case_without_archive_rubric(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, roster_root = _write_case(tmp_path)
    data = yaml.safe_load(dataset.read_text(encoding="utf-8"))
    del data["cases"][0]["legacy_expected"]
    dataset.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("FABRI_ROSTERS_ROOT", str(roster_root))

    case = load_memory_case(dataset, "support")

    assert case.legacy_required_terms is None
    assert case.legacy_forbidden_terms is None


def test_apply_holdout_response_contract_sets_strict_root_only_schema(tmp_path: Path) -> None:
    root = tmp_path / "ceo.yaml"
    root.write_text(
        yaml.safe_dump(
            {"agent": {"name": "ceo", "system_prompt": "Base prompt."}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    schema = {
        "type": "object",
        "required": ["response", "decision"],
        "properties": {
            "response": {"type": "string"},
            "decision": {"type": "string"},
        },
    }

    apply_holdout_response_contract(root, schema)

    agent = yaml.safe_load(root.read_text(encoding="utf-8"))["agent"]
    assert agent["response_schema"] == schema
    assert agent["response_retries"] == 1
    assert agent["error_strategy"] == "strict"
    assert "return the JSON object matching" in agent["system_prompt"]
    assert "append the required AGENT_MEMORY block" in agent["system_prompt"]


@pytest.mark.parametrize(
    ("structured_output", "expected_missing", "expected_forbidden"),
    [
        ({"response": "safe"}, ["missing:decision"], []),
        (None, ["structured_output:not_a_mapping"], []),
        (
            {"response": "This assigns blame.", "decision": "READY"},
            [],
            ["blame"],
        ),
    ],
)
def test_structured_required_and_designated_prose_safety_are_both_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    structured_output: object,
    expected_missing: list[str],
    expected_forbidden: list[str],
) -> None:
    dataset, roster_root = _write_case(tmp_path)
    monkeypatch.setenv("FABRI_ROSTERS_ROOT", str(roster_root))
    case = replace(load_memory_case(dataset, "support"), conditions=("memory",))
    runner = FakeRunner(roster_root, structured_outputs={"memory": structured_output})

    result = run_memory_study(case, tmp_path / "results", command_runner=runner)

    run = cast(list[dict[str, object]], result["runs"])[0]
    assert run["rubric_passed"] is False
    assert run["missing_required"] == expected_missing
    assert run["forbidden_hits"] == expected_forbidden
    private_result = json.loads(
        (
            tmp_path
            / "results/private-attempts/replica-01/memory/private/result.json"
        ).read_text(encoding="utf-8")
    )
    assert "structured_output" in private_result
    assert private_result["structured_output"] == structured_output
    assert "structured_output" not in cast(list[dict[str, object]], result["runs"])[0]


def test_apply_retrieval_overrides_rewrites_compiled_node_configs(tmp_path: Path) -> None:
    config = tmp_path / "compiled" / "support-hq" / "ceo.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "agent: {name: ceo}\nmemory:\n  top_k: 5\n  retrieval_strategy: hybrid\n",
        encoding="utf-8",
    )

    changed = apply_retrieval_overrides(
        tmp_path / "compiled",
        "support-hq",
        top_k=11,
        strategy="hybrid+mmr",
    )

    assert changed == [str(config)]
    assert yaml.safe_load(config.read_text(encoding="utf-8"))["memory"] == {
        "top_k": 11,
        "retrieval_strategy": "hybrid+mmr",
    }
    before = config.read_text(encoding="utf-8")
    assert apply_retrieval_overrides(
        tmp_path / "compiled", "support-hq", top_k=None, strategy=None
    ) == []
    assert config.read_text(encoding="utf-8") == before


def test_discover_memory_dbs_deduplicates_shared_paths_and_rejects_escape(
    tmp_path: Path,
) -> None:
    company = tmp_path / "compiled" / "support-hq"
    nested = company / "agencies" / "crew"
    nested.mkdir(parents=True)
    shared = company / ".fabri" / "shared.db"
    for path, agent in ((company / "ceo.yaml", "ceo"), (nested / "agent.yaml", "worker")):
        path.write_text(yaml.safe_dump({
            "agent": {"name": agent},
            "memory": {"backend": "sqlite", "sqlite_path": str(shared)},
        }), encoding="utf-8")

    specs = discover_memory_dbs(tmp_path / "compiled", "support-hq")

    assert len(specs) == 1
    assert specs[0].relative_path == Path(".fabri/shared.db")
    assert specs[0].agent_ids == ("ceo", "worker")

    (nested / "agent.yaml").write_text(yaml.safe_dump({
        "agent": {"name": "worker"},
        "memory": {"backend": "sqlite", "sqlite_path": "../../../../escaped.db"},
    }), encoding="utf-8")
    with pytest.raises(ProbeError, match="escapes company root"):
        discover_memory_dbs(tmp_path / "compiled", "support-hq")


def test_memory_study_counterbalances_condition_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, roster_root = _write_case(tmp_path, replicas=2)
    monkeypatch.setenv("FABRI_ROSTERS_ROOT", str(roster_root))
    runner = FakeRunner(roster_root)

    result = run_memory_study(
        load_memory_case(dataset, "support"), tmp_path / "results", command_runner=runner
    )

    runs = cast(list[dict[str, object]], result["runs"])
    assert [(run["replica"], run["condition"], run["execution_order"]) for run in runs] == [
        (1, "memory", 1),
        (1, "control", 2),
        (2, "control", 1),
        (2, "memory", 2),
    ]


@pytest.mark.parametrize(
    ("argument", "value", "message"),
    [
        ("--retrieval-top-k", "0", "--retrieval-top-k must be an integer between 1 and 50"),
        ("--retrieval-top-k", "51", "--retrieval-top-k must be an integer between 1 and 50"),
        ("--retrieval-strategy", "invalid", "--retrieval-strategy must be one of"),
    ],
)
def test_cli_rejects_invalid_retrieval_overrides(
    argument: str, value: str, message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit, match="2"):
        main(
            [
                "--dataset",
                "unused.yaml",
                "--case",
                "unused",
                "--output-dir",
                "unused",
                argument,
                value,
            ]
        )
    assert message in capsys.readouterr().err


def test_memory_study_applies_and_records_retrieval_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, roster_root = _write_case(tmp_path)
    monkeypatch.setenv("FABRI_ROSTERS_ROOT", str(roster_root))
    runner = FakeRunner(roster_root)

    result = run_memory_study(
        load_memory_case(dataset, "support"),
        tmp_path / "results",
        command_runner=runner,
        retrieval_top_k=11,
        retrieval_strategy="sparse",
    )

    assert set(runner.retrieval_configs_before_run) == {
        ("memory", "training"), ("memory", "holdout"),
        ("control", "training"), ("control", "holdout"),
    }
    assert all(
        config["top_k"] == 11 and config["retrieval_strategy"] == "sparse"
        for config in runner.retrieval_configs_before_run.values()
    )
    assert result["retrieval_overrides"] == {"top_k": 11, "retrieval_strategy": "sparse"}
    validate_memory_payload(result)
    assert "Retrieval overrides: top_k=`11`, retrieval_strategy=`sparse`" in render_markdown(result)


def test_incomplete_holdout_gets_no_rubric_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, roster_root = _write_case(tmp_path)
    monkeypatch.setenv("FABRI_ROSTERS_ROOT", str(roster_root))
    runner = FakeRunner(roster_root, incomplete_holdout="control")

    result = run_memory_study(
        load_memory_case(dataset, "support"), tmp_path / "results", command_runner=runner
    )

    control = next(
        run
        for run in cast(list[dict[str, object]], result["runs"])
        if run["condition"] == "control"
    )
    assert control["holdout_complete"] is False
    assert control["rubric_passed"] is None


def test_failed_training_invalidates_pair_without_running_holdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, roster_root = _write_case(tmp_path)
    monkeypatch.setenv("FABRI_ROSTERS_ROOT", str(roster_root))
    case = replace(load_memory_case(dataset, "support"), conditions=("memory",))
    runner = FakeRunner(roster_root, failed_training="memory")

    result = run_memory_study(case, tmp_path / "results", command_runner=runner)

    assert runner.run_stages == [("memory", "training")]
    run = cast(list[dict[str, object]], result["runs"])[0]
    assert run["training_outcome"] == "failed"
    assert run["rubric_passed"] is None
    assert "training_failed" in cast(list[str], run["training_failure_reasons"])


def test_truncated_training_reports_failure_not_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A truncated run self-reports success; the study must not.

    Regression for the accounting bug where ``training_success`` echoed the
    agent's optimistic ``success: true`` even though training was truncated and
    incomplete, contradicting ``training_failure_reasons``.
    """
    dataset, roster_root = _write_case(tmp_path)
    monkeypatch.setenv("FABRI_ROSTERS_ROOT", str(roster_root))
    case = replace(load_memory_case(dataset, "support"), conditions=("memory",))
    runner = FakeRunner(roster_root, truncated_training="memory")

    result = run_memory_study(case, tmp_path / "results", command_runner=runner)

    # Training truncated, so the holdout must never run.
    assert runner.run_stages == [("memory", "training")]
    run = cast(list[dict[str, object]], result["runs"])[0]
    assert run["training_outcome"] == "success"
    assert run["training_success"] is False
    reasons = cast(list[str], run["training_failure_reasons"])
    assert "training_failed" in reasons
    assert "truncation" in reasons
    assert run["holdout_failure_reasons"] == []
    # The published payload must satisfy the reconciled invariant.
    validate_memory_payload(result)


def test_invalid_run_routes_holdout_reason_to_holdout_phase() -> None:
    """A post-training failure keeps training successful and is filed as holdout."""
    run = _invalid_run(
        1,
        "memory",
        "success",
        0.05,
        "holdout_run_timeout",
        execution_order=1,
    )
    assert run["training_success"] is True
    assert run["training_failure_reasons"] == []
    assert run["holdout_failure_reasons"] == ["holdout_run_timeout"]


def test_validate_memory_payload_rejects_success_with_training_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, roster_root = _write_case(tmp_path)
    monkeypatch.setenv("FABRI_ROSTERS_ROOT", str(roster_root))
    case = replace(load_memory_case(dataset, "support"), conditions=("memory",))
    payload = run_memory_study(case, tmp_path / "results", command_runner=FakeRunner(roster_root))

    contradictory = cast(list[dict[str, object]], payload["runs"])[0]
    contradictory["training_success"] = True
    contradictory["training_failure_reasons"] = ["training_failed"]
    with pytest.raises(ProbeError, match="training success with training failures"):
        validate_memory_payload(payload)


def test_validate_memory_payload_rejects_malformed_payload() -> None:
    malformed: dict[str, object] = {"study": "company-memory-vs-control"}
    with pytest.raises(ProbeError, match="missing required key"):
        validate_memory_payload(malformed)
