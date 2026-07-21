"""Immutable company-memory snapshots and quality-first promotion gates."""
from __future__ import annotations

import hashlib
import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from fabri.benchmarks.company_memory_study import (
    MemoryCase,
    ProbeError,
    _analysis_for_process,
    _compile_company,
    _funnel_observations,
    _parse_payload,
    _run_company,
    apply_retrieval_overrides,
    discover_memory_dbs,
    load_memory_case,
)
from fabri.memory.embedded_store import SqliteMemoryStore
from fabri.memory.verification import apply_session_verification
from fabri.benchmarks.company_setup_probe import (
    DEFAULT_RUN_TIMEOUT_S,
    CommandRunner,
    _as_required_groups,
    _as_string_list,
    _run_command,
    score_text,
)

_GENERATION_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class PromotionDecision:
    promote: bool
    reasons: tuple[str, ...]
    pair_count: int
    candidate_median_cost_usd: float | None
    incumbent_median_cost_usd: float | None
    cost_ratio: float | None
    retry_reduction: float | None
    variants_with_verified_specialist_retrieval: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class EvaluationVariant:
    variant_id: str
    prompt: str
    required_terms: tuple[tuple[str, ...], ...]
    forbidden_terms: tuple[str, ...]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_manifest(snapshot_dir: Path) -> dict[str, object]:
    manifest_path = snapshot_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProbeError(f"invalid company memory snapshot {snapshot_dir}: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("databases"), list):
        raise ProbeError(f"invalid company memory snapshot manifest: {manifest_path}")
    return manifest


def create_company_snapshot(
    compiled_destination: Path,
    company_name: str,
    snapshots_root: Path,
    generation_id: str,
    *,
    parent_generation: str | None = None,
    source_revision: str | None = None,
    config_fingerprint: str | None = None,
) -> Path:
    """Copy every declared DB into an immutable generation directory."""
    if not _GENERATION_RE.fullmatch(generation_id):
        raise ProbeError(f"unsafe generation id: {generation_id!r}")
    snapshots_root.mkdir(parents=True, exist_ok=True)
    destination = snapshots_root / generation_id
    if destination.exists():
        raise ProbeError(f"snapshot generation already exists: {generation_id}")
    specs = discover_memory_dbs(compiled_destination, company_name)
    company_root = compiled_destination / company_name
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{generation_id}.", dir=snapshots_root))
    try:
        databases: list[dict[str, object]] = []
        for spec in specs:
            source = company_root / spec.relative_path
            if not source.is_file():
                raise ProbeError(f"declared memory DB is missing: {spec.relative_path}")
            target = temp_dir / "databases" / spec.relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            databases.append({
                "path": str(spec.relative_path),
                "agent_ids": list(spec.agent_ids),
                "config_paths": list(spec.config_paths),
                "size_bytes": target.stat().st_size,
                "sha256": _sha256(target),
            })
        manifest = {
            "schema_version": 1,
            "generation_id": generation_id,
            "parent_generation": parent_generation,
            "company": company_name,
            "created_at": datetime.now(UTC).isoformat(),
            "source_revision": source_revision,
            "config_fingerprint": config_fingerprint,
            "databases": databases,
        }
        (temp_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        temp_dir.replace(destination)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return destination


def restore_company_snapshot(
    snapshot_dir: Path,
    compiled_destination: Path,
    company_name: str,
) -> tuple[str, ...]:
    """Restore a snapshot into a fresh compile after validating its manifest."""
    manifest = _read_manifest(snapshot_dir)
    if manifest.get("company") != company_name:
        raise ProbeError("snapshot company does not match compiled company")
    specs = discover_memory_dbs(compiled_destination, company_name)
    declared = {str(spec.relative_path) for spec in specs}
    records = manifest["databases"]
    if not isinstance(records, list):
        raise ProbeError("snapshot databases must be a list")
    snapshot_paths = {
        str(record.get("path")) for record in records if isinstance(record, dict)
    }
    if snapshot_paths != declared:
        raise ProbeError("snapshot and compiled memory manifests do not match")
    restored: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            raise ProbeError("snapshot database record must be a mapping")
        relative = Path(str(record["path"]))
        source = snapshot_dir / "databases" / relative
        expected_hash = record.get("sha256")
        if not source.is_file() or _sha256(source) != expected_hash:
            raise ProbeError(f"snapshot database failed integrity check: {relative}")
        target = compiled_destination / company_name / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        restored.append(str(relative))
    return tuple(sorted(restored))


def apply_training_verification(
    compiled_destination: Path,
    case: MemoryCase,
    *,
    training_succeeded: bool,
    store_factory: Callable[[Path, str], object] | None = None,
) -> dict[str, object]:
    """Mark success-pattern lessons rubric_verified when the training run succeeded.

    Item-6 verification. The case's frozen rubric (required/forbidden terms)
    scores the HOLDOUT deliverable, and training is a DIFFERENT scenario, so the
    holdout rubric cannot be applied to training output. The objective, available
    gate for trusting a training-mined ``success_pattern`` is therefore whether
    the training run itself succeeded operationally -- ``training_succeeded`` is
    ``outcome in SUCCESS_OUTCOMES and analysis.complete`` (no truncation, tool
    failure, or incomplete delegation), computed by the caller. On success, every
    ``success_pattern`` lesson mined in that run is upgraded to
    ``rubric_verified`` (reusing :func:`apply_session_verification` per source
    session). On failure they stay ``unverified`` -- excluded from verified-only
    retrieval -- rather than ``contradicted``, which is reserved for genuine
    contradiction evidence. Deterministic tool-failure lessons are already
    ``tool_verified`` at mining time and are left untouched.

    Run this BEFORE :func:`create_company_snapshot` so the immutable snapshot
    captures the verdicts and their content hashes. Without it the promotion
    gate's verified-specialist-retrieval requirement can never be satisfied.
    """
    factory = store_factory or (
        lambda path, collection: SqliteMemoryStore(path=path, collection=collection)
    )
    databases: list[dict[str, object]] = []
    all_verified: list[str] = []
    for spec in discover_memory_dbs(compiled_destination, case.company_name):
        db_path = compiled_destination / case.company_name / spec.relative_path
        store = factory(db_path, str(spec.relative_path))
        verified: list[str] = []
        try:
            if training_succeeded:
                sessions: set[str] = set()
                for entry in store.iterate(kind="success_pattern"):
                    sessions.update(entry.source_session_ids)
                for session_id in sorted(sessions):
                    verified.extend(
                        apply_session_verification(
                            store,
                            session_id,
                            "rubric_verified",
                            kinds=frozenset({"success_pattern"}),
                        )
                    )
        finally:
            connection = getattr(store, "conn", None)
            if connection is not None:
                connection.close()
        verified = sorted(set(verified))
        all_verified.extend(verified)
        databases.append(
            {"path": str(spec.relative_path), "rubric_verified_entry_ids": verified}
        )
    return {
        "training_succeeded": training_succeeded,
        "rubric_verified_entry_ids": sorted(set(all_verified)),
        "databases": databases,
    }


def promote_company_snapshot(snapshot_dir: Path, current_pointer: Path) -> dict[str, object]:
    """Atomically point the benchmark's incumbent at one validated generation."""
    manifest = _read_manifest(snapshot_dir)
    pointer = {
        "generation_id": manifest.get("generation_id"),
        "snapshot_path": str(snapshot_dir.resolve()),
        "manifest_sha256": _sha256(snapshot_dir / "manifest.json"),
        "promoted_at": datetime.now(UTC).isoformat(),
    }
    current_pointer.parent.mkdir(parents=True, exist_ok=True)
    temporary = current_pointer.with_name(f".{current_pointer.name}.tmp")
    temporary.write_text(json.dumps(pointer, indent=2), encoding="utf-8")
    temporary.replace(current_pointer)
    return pointer


def evaluate_promotion(
    pairs: list[dict[str, object]],
    *,
    minimum_pairs: int = 6,
) -> PromotionDecision:
    """Apply the six-pair quality, cost, retry, and retrieval gate."""
    reasons: list[str] = []
    if len(pairs) < minimum_pairs:
        reasons.append("insufficient_pairs")
    candidate_costs: list[float] = []
    incumbent_costs: list[float] = []
    candidate_retries = 0
    incumbent_retries = 0
    retrieval_variants: set[str] = set()
    for pair in pairs:
        candidate = pair.get("candidate")
        incumbent = pair.get("incumbent")
        if not isinstance(candidate, dict) or not isinstance(incumbent, dict):
            reasons.append("invalid_pair")
            continue
        if candidate.get("complete") is not True or incumbent.get("complete") is not True:
            reasons.append("incomplete_pair")
        if candidate.get("cost_unaccounted") or incumbent.get("cost_unaccounted"):
            reasons.append("unaccounted_cost")
        if incumbent.get("rubric_passed") is True and candidate.get("rubric_passed") is not True:
            reasons.append("quality_regression")
        if candidate.get("forbidden_hits"):
            reasons.append("candidate_forbidden_hit")
        candidate_cost = candidate.get("total_cost_usd")
        incumbent_cost = incumbent.get("total_cost_usd")
        if isinstance(candidate_cost, (int, float)) and not isinstance(candidate_cost, bool):
            candidate_costs.append(float(candidate_cost))
        else:
            reasons.append("candidate_cost_missing")
        if isinstance(incumbent_cost, (int, float)) and not isinstance(incumbent_cost, bool):
            incumbent_costs.append(float(incumbent_cost))
        else:
            reasons.append("incumbent_cost_missing")
        candidate_retries += int(candidate.get("total_retries", 0) or 0)
        incumbent_retries += int(incumbent.get("total_retries", 0) or 0)
        retrieved = candidate.get("verified_specialist_entry_ids")
        if isinstance(retrieved, list) and retrieved:
            retrieval_variants.add(str(pair.get("variant_id", "")))

    candidate_median = statistics.median(candidate_costs) if candidate_costs else None
    incumbent_median = statistics.median(incumbent_costs) if incumbent_costs else None
    cost_ratio = (
        candidate_median / incumbent_median
        if candidate_median is not None and incumbent_median not in (None, 0)
        else None
    )
    retry_reduction = (
        (incumbent_retries - candidate_retries) / incumbent_retries
        if incumbent_retries > 0
        else (0.0 if candidate_retries == 0 else -1.0)
    )
    if cost_ratio is None or cost_ratio > 1.05:
        reasons.append("cost_guardrail_failed")
    if len(retrieval_variants) < 2:
        reasons.append("verified_specialist_retrieval_insufficient")
    strict_gain = (cost_ratio is not None and cost_ratio <= 0.90) or retry_reduction >= 0.25
    if not strict_gain:
        reasons.append("no_material_efficiency_gain")
    unique_reasons = tuple(sorted(set(reasons)))
    return PromotionDecision(
        promote=not unique_reasons,
        reasons=unique_reasons,
        pair_count=len(pairs),
        candidate_median_cost_usd=candidate_median,
        incumbent_median_cost_usd=incumbent_median,
        cost_ratio=cost_ratio,
        retry_reduction=retry_reduction,
        variants_with_verified_specialist_retrieval=len(retrieval_variants),
    )


def load_evaluation_variants(dataset_path: Path, case_id: str) -> tuple[EvaluationVariant, ...]:
    """Load one anchor plus two or more frozen semantic variants."""
    try:
        dataset = yaml.safe_load(dataset_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProbeError(f"could not load evolution dataset: {exc}") from exc
    cases = dataset.get("cases") if isinstance(dataset, dict) else None
    selected = next(
        (case for case in cases or [] if isinstance(case, dict) and case.get("id") == case_id),
        None,
    )
    evolution = selected.get("evolution") if isinstance(selected, dict) else None
    raw_variants = evolution.get("variants") if isinstance(evolution, dict) else None
    if not isinstance(raw_variants, list) or len(raw_variants) < 3:
        raise ProbeError("evolution.variants must contain an anchor and at least two holdouts")
    variants: list[EvaluationVariant] = []
    for index, raw in enumerate(raw_variants):
        if not isinstance(raw, dict):
            raise ProbeError(f"evolution variant {index} must be a mapping")
        expected = raw.get("expected")
        if not isinstance(expected, dict):
            raise ProbeError(f"evolution variant {index}.expected must be a mapping")
        variant_id = raw.get("id")
        prompt = raw.get("prompt")
        if not isinstance(variant_id, str) or not variant_id:
            raise ProbeError(f"evolution variant {index}.id must be a non-empty string")
        if not isinstance(prompt, str) or not prompt:
            raise ProbeError(f"evolution variant {index}.prompt must be a non-empty string")
        variants.append(EvaluationVariant(
            variant_id=variant_id,
            prompt=prompt,
            required_terms=_as_required_groups(
                expected.get("required"), f"evolution variant {variant_id}.expected.required"
            ),
            forbidden_terms=_as_string_list(
                expected.get("forbidden"), f"evolution variant {variant_id}.expected.forbidden"
            ),
        ))
    if len({variant.variant_id for variant in variants}) != len(variants):
        raise ProbeError("evolution variant ids must be unique")
    return tuple(variants)


def _snapshot_verified_specialist_ids(snapshot_dir: Path, root_id: str) -> set[str]:
    ids: set[str] = set()
    manifest = _read_manifest(snapshot_dir)
    records = manifest.get("databases")
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        database = snapshot_dir / "databases" / str(record.get("path"))
        try:
            import sqlite3

            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
                rows = connection.execute("SELECT id, payload FROM guidelines").fetchall()
        except (OSError, sqlite3.DatabaseError):
            continue
        for entry_id, payload_json in rows:
            try:
                payload = json.loads(payload_json)
            except (TypeError, json.JSONDecodeError):
                continue
            producer = payload.get("producer_agent_id", payload.get("agent_id"))
            if (
                producer
                and producer != root_id
                and payload.get("verification") in {"tool_verified", "rubric_verified"}
            ):
                ids.add(str(entry_id))
    return ids


def _run_evaluation_arm(
    case: MemoryCase,
    variant: EvaluationVariant,
    arm: str,
    snapshot_dir: Path,
    attempt_root: Path,
    command_runner: CommandRunner,
    command_cwd: Path,
    environment: dict[str, str],
    timeout_s: float,
    arm_cost_cap_usd: float,
) -> dict[str, object]:
    compile_root = attempt_root / f"{arm}-compiled"
    state_root = attempt_root / f"{arm}-state"
    process = _compile_company(
        case, compile_root, command_runner, command_cwd, environment, timeout_s
    )
    if process.returncode != 0:
        return {"complete": False, "failure": "compile_failed"}
    apply_retrieval_overrides(
        compile_root, case.company_name, top_k=None, strategy=None,
        verification="verified",
    )
    restore_company_snapshot(snapshot_dir, compile_root, case.company_name)
    root_config = compile_root / case.company_name / f"{case.root_id}.yaml"
    config = yaml.safe_load(root_config.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or not isinstance(config.get("agent"), dict):
        return {"complete": False, "failure": "root_config_invalid"}
    configured_cap = config["agent"].get("max_cost_usd")
    config["agent"]["max_cost_usd"] = min(
        float(configured_cap)
        if isinstance(configured_cap, (int, float)) and not isinstance(configured_cap, bool)
        else arm_cost_cap_usd,
        arm_cost_cap_usd,
    )
    root_config.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    try:
        run_process, _elapsed = _run_company(
            root_config, variant.prompt, state_root, command_runner,
            command_cwd, environment, timeout_s,
        )
        payload = _parse_payload(run_process)
        analysis = _analysis_for_process(
            run_process, payload, state_root, case.required_delegations
        )
    except (ProbeError, subprocess.TimeoutExpired) as exc:
        return {"complete": False, "failure": type(exc).__name__}
    complete = bool(analysis.get("complete"))
    rubric = score_text(
        payload.get("final_text") if isinstance(payload.get("final_text"), str) else "",
        variant.required_terms,
        variant.forbidden_terms,
    )
    funnel = _funnel_observations(state_root)
    verified_specialist_ids = _snapshot_verified_specialist_ids(snapshot_dir, case.root_id)
    retrieved_ids = set(funnel["retrieved_entry_ids"])
    private = attempt_root / "private"
    private.mkdir(parents=True, exist_ok=True)
    (private / f"{arm}.stdout").write_text(run_process.stdout, encoding="utf-8")
    (private / f"{arm}.stderr").write_text(run_process.stderr, encoding="utf-8")
    return {
        "complete": complete,
        "rubric_passed": rubric["passed"] if complete else None,
        "forbidden_hits": rubric["forbidden"] if complete else [],
        "total_cost_usd": analysis.get("total_cost_usd"),
        "total_retries": funnel["total_retries"],
        "cost_unaccounted": funnel["cost_unaccounted"],
        "verified_specialist_entry_ids": sorted(verified_specialist_ids & retrieved_ids),
    }


def run_evolution_suite(
    case: MemoryCase,
    variants: tuple[EvaluationVariant, ...],
    incumbent_snapshot: Path,
    candidate_snapshot: Path,
    output_dir: Path,
    *,
    replicas_per_variant: int = 2,
    max_cost_usd: float = 1.0,
    current_pointer: Path | None = None,
    command_runner: CommandRunner = _run_command,
    timeout_s: float = DEFAULT_RUN_TIMEOUT_S,
    cwd: Path | None = None,
) -> dict[str, object]:
    """Run paired snapshot trials, stop on budget, and promote only on a full pass."""
    if len(variants) < 3 or replicas_per_variant < 1 or max_cost_usd <= 0:
        raise ProbeError("evolution suite requires >=3 variants, replicas >=1, and positive cost")
    output_dir.mkdir(parents=True, exist_ok=True)
    work_root = output_dir / "private-attempts"
    work_root.mkdir(parents=True, exist_ok=True)
    command_cwd = Path.cwd() if cwd is None else cwd
    environment = dict(os.environ)
    pairs: list[dict[str, object]] = []
    cumulative_cost = 0.0
    projected_pair_cost = 0.0
    budget_stopped = False
    for variant in variants:
        for replica in range(1, replicas_per_variant + 1):
            if pairs and cumulative_cost + projected_pair_cost > max_cost_usd:
                budget_stopped = True
                break
            attempt = work_root / variant.variant_id / f"replica-{replica:02d}"
            attempt.mkdir(parents=True, exist_ok=False)
            order = ("candidate", "incumbent") if replica % 2 else ("incumbent", "candidate")
            arms: dict[str, dict[str, object]] = {}
            arm_cost_cap = max(0.0, max_cost_usd - cumulative_cost) / 2
            for arm in order:
                snapshot = candidate_snapshot if arm == "candidate" else incumbent_snapshot
                arms[arm] = _run_evaluation_arm(
                    case, variant, arm, snapshot, attempt, command_runner,
                    command_cwd, environment, timeout_s, arm_cost_cap,
                )
            pair = {
                "variant_id": variant.variant_id,
                "replica": replica,
                "execution_order": list(order),
                **arms,
            }
            pairs.append(pair)
            pair_cost = sum(
                float(arm.get("total_cost_usd", 0) or 0) for arm in arms.values()
            )
            cumulative_cost += pair_cost
            projected_pair_cost = cumulative_cost / len(pairs)
        if budget_stopped:
            break
    decision = evaluate_promotion(
        pairs, minimum_pairs=len(variants) * replicas_per_variant
    )
    if budget_stopped:
        decision = PromotionDecision(
            **{**decision.to_dict(), "promote": False,
               "reasons": tuple(sorted({*decision.reasons, "budget_stopped"}))}
        )
    pointer = None
    if decision.promote and current_pointer is not None:
        pointer = promote_company_snapshot(candidate_snapshot, current_pointer)
    result = {
        "study": "company-memory-evolution",
        "company": case.company_name,
        "case_id": case.case_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "replicas_per_variant": replicas_per_variant,
        "variant_ids": [variant.variant_id for variant in variants],
        "max_cost_usd": max_cost_usd,
        "total_cost_usd": round(cumulative_cost, 6),
        "pairs": pairs,
        "decision": decision.to_dict(),
        "promotion": pointer,
    }
    (output_dir / "results.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--case", required=True, dest="case_id")
    parser.add_argument("--incumbent-snapshot", required=True)
    parser.add_argument("--candidate-snapshot", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--current-pointer")
    parser.add_argument("--max-cost-usd", type=float, default=1.0)
    args = parser.parse_args(argv)
    try:
        dataset = Path(args.dataset)
        result = run_evolution_suite(
            load_memory_case(dataset, args.case_id),
            load_evaluation_variants(dataset, args.case_id),
            Path(args.incumbent_snapshot),
            Path(args.candidate_snapshot),
            Path(args.output_dir),
            max_cost_usd=args.max_cost_usd,
            current_pointer=Path(args.current_pointer) if args.current_pointer else None,
        )
    except (OSError, ProbeError) as exc:
        parser.error(str(exc))
    print(json.dumps({"status": "completed", "decision": result["decision"]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
