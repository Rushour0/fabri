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
    apply_retrieval_overrides,
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
                            "forbidden": ["blame"],
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
    ) -> None:
        self.roster_root = roster_root
        self.incomplete_holdout = incomplete_holdout
        self.failed_training = failed_training
        self.compile_destinations: list[Path] = []
        self.holdout_db_before_run: dict[str, bytes | None] = {}
        self.run_stages: list[tuple[str, str]] = []
        self.retrieval_configs_before_run: dict[tuple[str, str], dict[str, object]] = {}

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
            (company / "ceo.yaml").write_text(
                "agent: {name: ceo}\nmemory:\n  top_k: 5\n",
                encoding="utf-8",
            )
            database = company / ".fabri" / "support_hq.db"
            database.parent.mkdir(parents=True)
            database.write_bytes(f"compiled:{destination.name}".encode())
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
        database = root_config.parent / ".fabri" / "support_hq.db"
        if stage == "holdout":
            self.holdout_db_before_run[condition] = (
                database.read_bytes() if database.exists() else None
            )
        state_root = Path(env["FABRI_HOME"])
        traces = state_root / ".fabri" / "traces"
        traces.mkdir(parents=True)
        session = f"{condition}-{stage}"
        child_session = f"{session}-child"
        failed = stage == "training" and condition == self.failed_training
        incomplete = stage == "holdout" and condition == self.incomplete_holdout
        outcome = "failed" if failed else "success"
        root_events: list[dict[str, object]] = [
            {
                "type": "tool_call",
                "name": "crew",
                "result": {
                    "ok": not failed,
                    "result": {"session_id": child_session, "outcome": outcome},
                },
            },
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
        payload = {
            "session_id": session,
            "success": not failed,
            "final_text": final_text,
            "outcome": outcome,
            "usage": {"cost_usd": 0.01},
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")


def test_memory_study_copies_only_memory_db_and_emits_safe_public_payload(
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
    assert runner.holdout_db_before_run["memory"] == b"compiled:training-compiled"
    assert runner.holdout_db_before_run["control"] is None
    runs = cast(list[dict[str, object]], result["runs"])
    memory_run = next(run for run in runs if run["condition"] == "memory")
    control_run = next(run for run in runs if run["condition"] == "control")
    assert memory_run["rubric_passed"] is True
    assert control_run["rubric_passed"] is False
    assert control_run["guidelines_retrieved"] == 0
    assert result["claim_boundary"] == CLAIM_BOUNDARY
    assert result["retrieval_overrides"] == {"top_k": None, "retrieval_strategy": None}
    assert all(
        config == {"top_k": 5}
        for config in runner.retrieval_configs_before_run.values()
    )
    public_text = (tmp_path / "results" / "results.json").read_text(encoding="utf-8")
    assert "Train privately" not in public_text
    assert "memory-training" not in public_text
    emitted = json.loads(public_text)
    validate_memory_payload(emitted)
    markdown = (tmp_path / "results" / "results.md").read_text(encoding="utf-8")
    assert markdown == render_markdown(emitted)


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

    assert runner.retrieval_configs_before_run == {
        ("memory", "training"): {"top_k": 11, "retrieval_strategy": "sparse"},
        ("memory", "holdout"): {"top_k": 11, "retrieval_strategy": "sparse"},
        ("control", "training"): {"top_k": 11, "retrieval_strategy": "sparse"},
        ("control", "holdout"): {"top_k": 11, "retrieval_strategy": "sparse"},
    }
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


def test_validate_memory_payload_rejects_malformed_payload() -> None:
    malformed: dict[str, object] = {"study": "company-memory-vs-control"}
    with pytest.raises(ProbeError, match="missing required key"):
        validate_memory_payload(malformed)
