"""Summarize a results dir written by run.py into report.md and summary.json.

    python benchmarks/jev/report.py benchmarks/jev/results/<timestamp>
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from run import to_bool  # noqa: E402

ARM_ORDER = ["jev", "gpt5mini", "cascade"]
FAMILY_ORDER = ["routing", "tool_select", "escalate_yesno", "urgency_score"]
CONF_THRESHOLDS = (0.6, 0.8, 0.95)
PASS_FAMILIES = ("routing", "tool_select")


# ---------------------------------------------------------------- stats

def wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return None, None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def percentile(values, q: float):
    """Nearest-rank percentile; None for an empty list."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    rank = max(1, math.ceil(q / 100.0 * len(vals)))
    return vals[rank - 1]


def ece(pairs, bins: int = 10):
    """Expected calibration error over (confidence, correct) pairs, equal-width bins."""
    if not pairs:
        return None
    buckets = defaultdict(list)
    for conf, ok in pairs:
        b = min(bins - 1, max(0, int(conf * bins)))
        buckets[b].append((conf, ok))
    total = len(pairs)
    err = 0.0
    for rows in buckets.values():
        mean_conf = sum(c for c, _ in rows) / len(rows)
        acc = sum(1 for _, ok in rows if ok) / len(rows)
        err += abs(acc - mean_conf) * len(rows) / total
    return err


def p_true(row):
    """Probability the model put on 'yes' for a yesno row, or None if unknown.
    Without probabilities it is rebuilt from the confidence, which is
    |2p - 1| for yesno: the chosen side has p = (1 + confidence) / 2."""
    probs = row.get("probabilities")
    if isinstance(probs, dict):
        for key in ("true", "yes", "True", "Yes", True):
            if key in probs and probs[key] is not None:
                return float(probs[key])
    conf, val = row.get("confidence"), to_bool(row.get("value"))
    if conf is None or val is None:
        return None
    p_chosen = (1.0 + float(conf)) / 2.0
    return p_chosen if val else 1.0 - p_chosen


def p_chosen(row):
    """Predicted P(correct): the probability the model gave the answer it
    chose. That is the largest of `probabilities`, or max(p, 1 - p) for
    yesno. None when the row has no probabilities. The normalized
    `confidence` is not a probability (0 means a uniform guess), so it is
    never used here directly."""
    if row.get("kind") == "yesno":
        p = p_true(row)
        return None if p is None else max(p, 1.0 - p)
    probs = row.get("probabilities")
    if not isinstance(probs, dict):
        return None
    vals = [float(v) for v in probs.values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return max(vals) if vals else None


# ---------------------------------------------------------------- aggregation

def load_rows(results_dir: Path) -> list[dict]:
    rows = {}
    path = results_dir / "results.jsonl"
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows[(r.get("arm"), r.get("id"))] = r  # later rows win
    return list(rows.values())


def load_log(results_dir: Path) -> list[dict]:
    path = results_dir / "run_log.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    k = sum(1 for r in rows if r.get("correct"))
    lo, hi = wilson(k, n)
    lat = [r.get("latency_ms") for r in rows]
    costs = [r.get("cost_usd") for r in rows]
    known_costs = [c for c in costs if c is not None]
    total_cost = sum(known_costs) if known_costs else None
    esc = [r.get("escalated") for r in rows if r.get("escalated") is not None]
    out = {
        "n": n,
        "correct": k,
        "accuracy": k / n if n else None,
        "ci95": [lo, hi],
        "errors": sum(1 for r in rows if r.get("error")),
        "latency_ms": {"p50": percentile(lat, 50), "p95": percentile(lat, 95), "p99": percentile(lat, 99)},
        "cost_usd_total": total_cost,
        "cost_rows_missing": sum(1 for c in costs if c is None),
        "cost_per_1k": (1000 * total_cost / n) if (total_cost is not None and n) else None,
        "cost_per_1k_correct": (1000 * total_cost / k) if (total_cost is not None and k) else None,
        "escalation_rate": (sum(1 for e in esc if e) / len(esc)) if esc else None,
    }
    if any("jev_cost_usd" in r for r in rows):
        jev = [r.get("jev_cost_usd") for r in rows]
        fb = [r.get("fallback_cost_usd") for r in rows]
        out["spend_split_usd"] = {
            "jev": sum(c for c in jev if c is not None),
            "fallback": sum(c for c in fb if c is not None),
            "rows_missing_split": sum(1 for a, b in zip(jev, fb) if a is None or b is None),
        }
    return out


def calibration(rows: list[dict]) -> dict | None:
    """ECE, accuracy at a threshold and coverage, all on P(chosen answer).
    The raw `confidence` is summarized separately."""
    pairs = []
    for r in rows:
        p = p_chosen(r)
        if p is not None:
            pairs.append((p, bool(r.get("correct"))))
    confs = [float(r["confidence"]) for r in rows if r.get("confidence") is not None]
    if not pairs and not confs:
        return None
    out = {
        "n_with_p": len(pairs),
        "ece_10bin": ece(pairs),
        "mean_p_chosen": (sum(p for p, _ in pairs) / len(pairs)) if pairs else None,
        "n_with_confidence": len(confs),
        "mean_confidence": (sum(confs) / len(confs)) if confs else None,
        "share_conf_ge_0.99": (sum(1 for c in confs if c >= 0.99) / len(confs)) if confs else None,
        "at_threshold": {},
    }
    for t in CONF_THRESHOLDS:
        sel = [ok for p, ok in pairs if p >= t]
        out["at_threshold"][str(t)] = {
            "coverage": (len(sel) / len(pairs)) if pairs else None,
            "accuracy": (sum(sel) / len(sel)) if sel else None,
            "n": len(sel),
        }
    yes = [(p_true(r), r.get("gold")) for r in rows if r.get("kind") == "yesno"]
    yes = [(p, g) for p, g in yes if p is not None and isinstance(g, bool)]
    if yes:
        out["brier_yesno"] = sum((p - (1.0 if g else 0.0)) ** 2 for p, g in yes) / len(yes)
        out["brier_n"] = len(yes)
    return out


def _ordered(keys, order):
    return [k for k in order if k in keys] + sorted(k for k in keys if k not in order)


def build_summary(rows: list[dict], log: list[dict]) -> dict:
    by_arm = defaultdict(list)
    for r in rows:
        by_arm[r["arm"]].append(r)
    arms = _ordered(by_arm, ARM_ORDER)
    starts = [e for e in log if e.get("event") == "start"]
    dry = any(r.get("dry_run") for r in rows) or any(e.get("dry_run") for e in starts)
    skipped = {}
    for e in starts:
        skipped.update(e.get("skipped_arms") or {})

    summary = {"dry_run": dry, "arms": arms, "skipped_arms": skipped, "rows": len(rows),
               "by_arm": {}, "by_arm_family": {}, "pooled_pass_families": {},
               "calibration": {}, "calibration_by_family": {}, "by_tag": {}}
    for arm in arms:
        ar = by_arm[arm]
        summary["by_arm"][arm] = summarize(ar)
        fams = defaultdict(list)
        for r in ar:
            fams[r["family"]].append(r)
        summary["by_arm_family"][arm] = {f: summarize(fams[f]) for f in _ordered(fams, FAMILY_ORDER)}
        pooled = [r for r in ar if r["family"] in PASS_FAMILIES]
        summary["pooled_pass_families"][arm] = summarize(pooled) if pooled else None
        cal = calibration(ar)
        if cal:
            summary["calibration"][arm] = cal
            by_fam = {f: calibration(fams[f]) for f in _ordered(fams, FAMILY_ORDER)}
            summary["calibration_by_family"][arm] = {f: c for f, c in by_fam.items() if c}
        tags = defaultdict(list)
        for r in ar:
            for t in r.get("tags") or []:
                tags[t].append(r)
            if not set(r.get("tags") or []) - {"none_applies"}:
                tags["(no perturbation)"].append(r)
        summary["by_tag"][arm] = {
            t: {"n": len(v), "correct": sum(1 for r in v if r.get("correct")),
                "accuracy": sum(1 for r in v if r.get("correct")) / len(v),
                "ci95": list(wilson(sum(1 for r in v if r.get("correct")), len(v)))}
            for t, v in sorted(tags.items())
        }
    summary["pass_rule"] = pass_rule(summary)
    return summary


def pass_rule(s: dict) -> dict:
    """Evaluate the pre-registered rule from README.md on these rows."""
    pooled = s["pooled_pass_families"]
    casc, gpt = pooled.get("cascade"), pooled.get("gpt5mini")
    out = {"evaluable": bool(casc and gpt and casc["n"] and gpt["n"])}
    if out["evaluable"]:
        acc_ok = casc["accuracy"] >= gpt["accuracy"] - 0.02
        p50c, p50g = casc["latency_ms"]["p50"], gpt["latency_ms"]["p50"]
        lat_ok = p50c is not None and p50g is not None and p50c * 2 <= p50g
        cc, cg = casc["cost_per_1k_correct"], gpt["cost_per_1k_correct"]
        cost_ok = cc is not None and cg is not None and cc < cg
        out.update({
            "accuracy_within_2pts": acc_ok, "p50_at_least_2x_lower": lat_ok,
            "cost_per_1k_correct_lower": cost_ok, "cascade_ships_as_default": acc_ok and lat_ok and cost_ok,
        })
    jev_y = s["by_arm_family"].get("jev", {}).get("escalate_yesno")
    gpt_y = s["by_arm_family"].get("gpt5mini", {}).get("escalate_yesno")
    if jev_y and gpt_y and jev_y["n"] and gpt_y["n"]:
        out["jev_may_take_act_no_act"] = jev_y["accuracy"] >= gpt_y["accuracy"] - 0.02
    else:
        out["jev_may_take_act_no_act"] = None
    return out


# ---------------------------------------------------------------- rendering

def pct(x):
    return "n/a" if x is None else f"{100 * x:.1f}%"


def ms(x):
    return "n/a" if x is None else f"{x:.0f}"


def usd(x):
    return "n/a" if x is None else f"${x:.4f}"


def ci(c):
    lo, hi = c
    return "n/a" if lo is None else f"{100 * lo:.1f} to {100 * hi:.1f}"


def num3(x):
    return "n/a" if x is None else f"{x:.3f}"


def yn(v):
    return "n/a" if v is None else ("yes" if v else "no")


def render(s: dict, results_dir: Path) -> str:
    L = []
    L.append("# Jev decision benchmark report")
    L.append("")
    L.append(f"Results dir: `{results_dir}`. Rows: {s['rows']}. Arms: {', '.join(s['arms']) or 'none'}.")
    if s["dry_run"]:
        L.append("")
        L.append("**DRY RUN.** These rows came from fake deciders that return the gold answer with "
                 "made-up latency, confidence and cost. Every number below checks the pipeline only. "
                 "None of it is a measurement of Jev or gpt-5-mini.")
    if s["skipped_arms"]:
        L.append("")
        for arm, keys in s["skipped_arms"].items():
            L.append(f"Arm `{arm}` was skipped because {', '.join(keys)} was not set.")
    L.append("")
    L.append("## Accuracy, latency and cost by arm and family")
    L.append("")
    L.append("Accuracy has a 95% Wilson interval. Latency is in milliseconds (nearest-rank percentiles). "
             "Cost per 1k correct divides total spend by correct decisions only.")
    L.append("")
    L.append("| arm | family | n | accuracy | 95% CI | errors | p50 | p95 | p99 | cost / 1k | cost / 1k correct | escalation |")
    L.append("|---|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|")
    for arm in s["arms"]:
        fams = dict(s["by_arm_family"][arm])
        fams["all"] = s["by_arm"][arm]
        for fam, m in fams.items():
            lat = m["latency_ms"]
            L.append(f"| {arm} | {fam} | {m['n']} | {pct(m['accuracy'])} | {ci(m['ci95'])} | {m['errors']} | "
                     f"{ms(lat['p50'])} | {ms(lat['p95'])} | {ms(lat['p99'])} | {usd(m['cost_per_1k'])} | "
                     f"{usd(m['cost_per_1k_correct'])} | {pct(m['escalation_rate']) if arm == 'cascade' else 'n/a'} |")
    casc = s["by_arm"].get("cascade")
    if casc and casc.get("spend_split_usd"):
        sp = casc["spend_split_usd"]
        total = sp["jev"] + sp["fallback"]
        share = (sp["jev"] / total) if total else None
        L.append("")
        L.append("## Cascade spend by provider")
        L.append("")
        L.append(f"The cascade spent {usd(sp['jev'])} on Jev attempts and {usd(sp['fallback'])} on gpt-5-mini "
                 f"fallbacks. Jev's share of cascade spend is {pct(share)}. "
                 f"{sp['rows_missing_split']} rows had no cost split.")
    L.append("")
    L.append("## Calibration")
    L.append("")
    if not s["calibration"]:
        L.append("No arm reported probabilities or confidence.")
    else:
        L.append("Calibration uses P, the probability the model gave its chosen answer (the largest "
                 "probability, or max(p, 1 - p) for yesno), as the predicted chance of being right. "
                 "ECE uses 10 equal-width bins of P. Coverage is the share of decisions with P at or above "
                 "the threshold, and accuracy is measured on that share only. The raw confidence columns "
                 "show the decider's normalized confidence, where 0 means a uniform guess, so they are not "
                 "probabilities. Brier is for yesno items and uses the probability given to yes.")
        L.append("")
        hdr = " | ".join(f"acc at P >= {t} | cover at P >= {t}" for t in CONF_THRESHOLDS)
        L.append(f"| arm | scope | n with P | ECE | mean P | mean raw conf | share raw conf >= 0.99 | {hdr} | Brier (yesno) |")
        L.append("|---|---|---:|---:|---:|---:|---:|" + "---:|" * (2 * len(CONF_THRESHOLDS)) + "---:|")
        for arm in s["arms"]:
            scopes = {"all": s["calibration"].get(arm)}
            scopes.update(s["calibration_by_family"].get(arm, {}))
            for scope, c in scopes.items():
                if not c:
                    continue
                cells = " | ".join(f"{pct(c['at_threshold'][str(t)]['accuracy'])} | {pct(c['at_threshold'][str(t)]['coverage'])}"
                                   for t in CONF_THRESHOLDS)
                brier = c.get("brier_yesno")
                L.append(f"| {arm} | {scope} | {c['n_with_p']} | {num3(c['ece_10bin'])} | "
                         f"{num3(c['mean_p_chosen'])} | {num3(c['mean_confidence'])} | "
                         f"{pct(c['share_conf_ge_0.99'])} | {cells} | {num3(brier)} |")
    L.append("")
    L.append("## Accuracy by perturbation tag")
    L.append("")
    L.append("Tags pool across families. The row \"(no perturbation)\" holds items with no perturbation tag, "
             "for comparison. `none_applies` marks tool_select items whose gold is `none`.")
    L.append("")
    L.append("| arm | tag | n | accuracy | 95% CI |")
    L.append("|---|---|---:|---:|---|")
    for arm in s["arms"]:
        for tag, m in s["by_tag"][arm].items():
            L.append(f"| {arm} | {tag} | {m['n']} | {pct(m['accuracy'])} | {ci(m['ci95'])} |")
    L.append("")
    L.append("## Pre-registered pass rule")
    L.append("")
    L.append("Jev-first cascade ships as the fabri routing default if, on routing and tool_select, its accuracy "
             "is within 2 points of gpt-5-mini or better, its p50 latency is at least 2x lower, and its cost per "
             "1k correct decisions is lower. Act/no-act (escalate_yesno) decisions stay on the LLM or a human "
             "unless Jev's accuracy there is within 2 points of gpt-5-mini.")
    L.append("")
    pr = s["pass_rule"]
    if s["dry_run"]:
        L.append("This is a dry run, so the rule is not evaluated here.")
    elif not pr["evaluable"]:
        L.append("The rule needs both the cascade and gpt5mini arms on routing and tool_select. They are not both present.")
    else:
        L.append(f"Accuracy within 2 points: {yn(pr['accuracy_within_2pts'])}. "
                 f"p50 at least 2x lower: {yn(pr['p50_at_least_2x_lower'])}. "
                 f"Cost per 1k correct lower: {yn(pr['cost_per_1k_correct_lower'])}. "
                 f"Cascade ships as the routing default: {yn(pr['cascade_ships_as_default'])}.")
    if not s["dry_run"]:
        L.append("")
        L.append(f"Jev may take act/no-act decisions: {yn(pr['jev_may_take_act_no_act'])}.")
    L.append("")
    return "\n".join(L)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python benchmarks/jev/report.py RESULTS_DIR", file=sys.stderr)
        return 2
    results_dir = Path(argv[0])
    if not (results_dir / "results.jsonl").exists():
        print(f"no results.jsonl in {results_dir}", file=sys.stderr)
        return 2
    rows = load_rows(results_dir)
    summary = build_summary(rows, load_log(results_dir))
    (results_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (results_dir / "report.md").write_text(render(summary, results_dir), encoding="utf-8")
    print(f"wrote {results_dir / 'report.md'} and {results_dir / 'summary.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
