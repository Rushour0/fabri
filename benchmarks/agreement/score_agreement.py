#!/usr/bin/env python3
"""Score a filled human label sheet against raw and corrected scorer verdicts."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path


ITEM_HEADING = re.compile(r"^## Item (\d+)\s*$")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})(?:[^`]*)$")
VERDICT_LINE = re.compile(r"^verdict:\s*(.*?)\s*$", re.IGNORECASE)
REASON_LINE = re.compile(r"^reason:\s*(.*?)\s*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class HumanLabel:
    item: int
    verdict: bool
    reason: str


@dataclass(frozen=True, slots=True)
class KeyItem:
    item: int
    run_id: str
    raw_verdict: bool
    corrected_verdict: bool


def parse_filled_sheet(text: str) -> dict[int, HumanLabel]:
    """Parse pass/fail labels outside fenced agent-output blocks."""
    values: dict[int, dict[str, str | None]] = {}
    current_item: int | None = None
    fence_character: str | None = None
    fence_length = 0

    for line in text.splitlines():
        fence_match = FENCE.match(line)
        if fence_character is not None:
            if (
                fence_match is not None
                and fence_match.group(1)[0] == fence_character
                and len(fence_match.group(1)) >= fence_length
            ):
                fence_character = None
                fence_length = 0
            continue
        if fence_match is not None:
            fence_character = fence_match.group(1)[0]
            fence_length = len(fence_match.group(1))
            continue

        item_match = ITEM_HEADING.match(line)
        if item_match is not None:
            item = int(item_match.group(1))
            if item in values:
                raise ValueError(f"duplicate item heading: {item}")
            values[item] = {"verdict": None, "reason": ""}
            current_item = item
            continue
        if current_item is None:
            continue

        verdict_match = VERDICT_LINE.match(line)
        if verdict_match is not None:
            if values[current_item]["verdict"] is not None:
                raise ValueError(f"duplicate verdict line for item {current_item}")
            values[current_item]["verdict"] = verdict_match.group(1)
            continue
        reason_match = REASON_LINE.match(line)
        if reason_match is not None:
            values[current_item]["reason"] = reason_match.group(1)

    if not values:
        raise ValueError("sheet contains no '## Item N' sections")

    unfilled: list[int] = []
    unparseable: list[int] = []
    labels: dict[int, HumanLabel] = {}
    for item, fields in values.items():
        raw_verdict = fields["verdict"]
        if raw_verdict is None or not raw_verdict.strip():
            unfilled.append(item)
            continue
        normalized = raw_verdict.strip().casefold()
        if normalized not in {"pass", "fail"}:
            unparseable.append(item)
            continue
        labels[item] = HumanLabel(
            item=item,
            verdict=normalized == "pass",
            reason=(fields["reason"] or "").strip(),
        )

    errors = []
    if unfilled:
        errors.append("unfilled verdicts for items: " + ", ".join(map(str, unfilled)))
    if unparseable:
        errors.append("unparseable verdicts for items: " + ", ".join(map(str, unparseable)))
    if errors:
        raise ValueError("; ".join(errors))
    return labels


def _require_bool(value: object, *, field: str, item: int) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"key item {item}: {field} must be a boolean")
    return value


def load_key(path: Path) -> tuple[KeyItem, ...]:
    """Load and validate the fields needed from the hidden JSON key."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError(f"{path}: expected an object containing an items list")

    items: list[KeyItem] = []
    seen: set[int] = set()
    for value in data["items"]:
        if not isinstance(value, dict):
            raise ValueError(f"{path}: every key item must be an object")
        item = value.get("item")
        run_id = value.get("run_id")
        if not isinstance(item, int) or isinstance(item, bool) or item <= 0:
            raise ValueError(f"{path}: key item number must be a positive integer")
        if item in seen:
            raise ValueError(f"{path}: duplicate key item {item}")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError(f"{path}: key item {item} needs a non-empty run_id")
        seen.add(item)
        items.append(
            KeyItem(
                item=item,
                run_id=run_id,
                raw_verdict=_require_bool(
                    value.get("raw_verdict"), field="raw_verdict", item=item
                ),
                corrected_verdict=_require_bool(
                    value.get("corrected_verdict"), field="corrected_verdict", item=item
                ),
            )
        )
    if not items:
        raise ValueError(f"{path}: key has no items")
    return tuple(sorted(items, key=lambda value: value.item))


def cohens_kappa(human: list[bool], scorer: list[bool]) -> float | None:
    """Compute Cohen's kappa, returning None when either rater has no variance."""
    if len(human) != len(scorer):
        raise ValueError("human and scorer label counts differ")
    if not human:
        raise ValueError("cannot compute kappa for zero labels")
    if len(set(human)) < 2 or len(set(scorer)) < 2:
        return None

    count = len(human)
    observed = (
        sum(
            human_label == scorer_label
            for human_label, scorer_label in zip(human, scorer)
        )
        / count
    )
    human_pass = sum(human) / count
    scorer_pass = sum(scorer) / count
    expected = human_pass * scorer_pass + (1.0 - human_pass) * (1.0 - scorer_pass)
    if expected == 1.0:
        return None
    return (observed - expected) / (1.0 - expected)


def _undefined_variance(human: list[bool], scorer: list[bool]) -> str | None:
    if len(set(human)) < 2:
        return "human"
    if len(set(scorer)) < 2:
        return "scorer"
    return None


def _score_one(
    labels: dict[int, HumanLabel], key_items: tuple[KeyItem, ...], scorer_field: str
) -> dict[str, object]:
    human = [labels[key_item.item].verdict for key_item in key_items]
    scorer = [
        key_item.raw_verdict if scorer_field == "raw" else key_item.corrected_verdict
        for key_item in key_items
    ]
    matches = sum(a == b for a, b in zip(human, scorer))
    confusion = {
        "human_pass": {
            "scorer_pass": sum(a and b for a, b in zip(human, scorer)),
            "scorer_fail": sum(a and not b for a, b in zip(human, scorer)),
        },
        "human_fail": {
            "scorer_pass": sum(not a and b for a, b in zip(human, scorer)),
            "scorer_fail": sum(not a and not b for a, b in zip(human, scorer)),
        },
    }
    disagreements = []
    for key_item, human_verdict, scorer_verdict in zip(key_items, human, scorer):
        if human_verdict == scorer_verdict:
            continue
        disagreements.append(
            {
                "item": key_item.item,
                "run_id": key_item.run_id,
                "human_verdict": "pass" if human_verdict else "fail",
                "reason": labels[key_item.item].reason,
                "scorer_verdict": "pass" if scorer_verdict else "fail",
            }
        )

    kappa = cohens_kappa(human, scorer)
    return {
        "agreement_percent": matches / len(key_items) * 100.0,
        "agreement_count": matches,
        "item_count": len(key_items),
        "kappa": kappa,
        "kappa_undefined_no_variance_in": _undefined_variance(human, scorer),
        "confusion": confusion,
        "disagreements": disagreements,
    }


def score_sheet(sheet_path: Path, key_path: Path) -> dict[str, object]:
    """Join a filled sheet to its key and compute both scorer comparisons."""
    labels = parse_filled_sheet(sheet_path.read_text(encoding="utf-8"))
    key_items = load_key(key_path)
    key_numbers = {item.item for item in key_items}
    label_numbers = set(labels)
    if key_numbers != label_numbers:
        missing = sorted(key_numbers - label_numbers)
        extra = sorted(label_numbers - key_numbers)
        details = []
        if missing:
            details.append("missing sheet items: " + ", ".join(map(str, missing)))
        if extra:
            details.append("items absent from key: " + ", ".join(map(str, extra)))
        raise ValueError("sheet/key mismatch: " + "; ".join(details))

    return {
        "item_count": len(key_items),
        "raw": _score_one(labels, key_items, "raw"),
        "corrected": _score_one(labels, key_items, "corrected"),
    }


def _format_comparison(name: str, result: dict[str, object]) -> list[str]:
    count = result["agreement_count"]
    total = result["item_count"]
    lines = [
        f"== {name} scorer vs human ==",
        f"agreement: {result['agreement_percent']:.1f}% ({count}/{total})",
    ]
    if result["kappa"] is None:
        lines.append(
            "kappa: undefined (no variance in "
            f"{result['kappa_undefined_no_variance_in']} labels)"
        )
    else:
        lines.append(f"kappa: {result['kappa']:.4f}")

    confusion = result["confusion"]
    lines.extend(
        [
            "",
            "confusion (human x scorer):",
            "| Human \\ Scorer | Pass | Fail |",
            "| --- | ---: | ---: |",
            f"| Pass | {confusion['human_pass']['scorer_pass']} | "
            f"{confusion['human_pass']['scorer_fail']} |",
            f"| Fail | {confusion['human_fail']['scorer_pass']} | "
            f"{confusion['human_fail']['scorer_fail']} |",
            "",
            "disagreements:",
        ]
    )
    disagreements = result["disagreements"]
    if not disagreements:
        lines.append("(none)")
    else:
        for disagreement in disagreements:
            reason = disagreement["reason"] or "(no reason supplied)"
            lines.append(
                f"- item {disagreement['item']} | run {disagreement['run_id']} | "
                f"human {disagreement['human_verdict']} | scorer "
                f"{disagreement['scorer_verdict']} | reason: {reason}"
            )
    return lines


def format_report(report: dict[str, object]) -> str:
    """Format the machine-computed report for terminal review."""
    lines = [f"items: {report['item_count']}", ""]
    lines.extend(_format_comparison("raw", report["raw"]))
    lines.append("")
    lines.extend(_format_comparison("corrected", report["corrected"]))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare filled human labels with raw and corrected scorer verdicts."
    )
    parser.add_argument("--sheet", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args()

    try:
        report = score_sheet(Path(args.sheet), Path(args.key))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        parser.error(str(error))

    print(format_report(report))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {args.json_out}")


if __name__ == "__main__":
    main()
