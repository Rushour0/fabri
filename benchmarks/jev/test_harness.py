"""Offline tests for the Jev decision benchmark harness. No network, no keys."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import dataset  # noqa: E402
import report  # noqa: E402
import run  # noqa: E402


@pytest.fixture(scope="module")
def items():
    return dataset.load()


def test_dataset_loads_every_family(items):
    assert {it["family"] for it in items} == set(dataset.FAMILIES)
    for it in items:
        assert set(it) == {"id", "family", "state", "question", "gold", "tags"}
        assert it["question"]["kind"] in ("choice", "score", "yesno")


def test_ids_unique(items):
    ids = [it["id"] for it in items]
    assert len(ids) == len(set(ids))


def test_gold_within_options(items):
    for it in items:
        q = it["question"]
        if q["kind"] == "choice":
            assert it["gold"] in q["options"], it["id"]
        elif q["kind"] == "score":
            assert it["gold"] in q["levels"], it["id"]
            assert sorted(q["levels"]) == sorted(dataset.LEVELS)
        else:
            assert isinstance(it["gold"], bool), it["id"]


def test_sizes_as_targeted(items):
    counts = Counter(it["family"] for it in items)
    assert dict(counts) == dataset.TARGET_SIZES
    tools = [it for it in items if it["family"] == "tool_select"]
    assert sum(it["gold"] == "none" for it in tools) >= 30
    large = [it for it in items if "large_action_set" in it["tags"]]
    assert 20 <= len(large) <= 30
    assert all(it["family"] == "routing" and len(it["question"]["options"]) >= 12 for it in large)
    gold_yes = Counter(it["gold"] for it in items if it["family"] == "escalate_yesno")
    assert gold_yes[True] > 0 and gold_yes[False] > 0
    for tag in dataset.TAGS:
        assert any(tag in it["tags"] for it in items), tag


def test_injected_text_never_points_at_gold(items):
    injected = [it for it in items if "injected_text" in it["tags"]]
    assert injected
    for it in injected:
        blob = json.dumps(it["state"]).lower()
        assert "ignore previous instructions" in blob
        if it["question"]["kind"] in ("choice", "score"):
            assert f"choose {str(it['gold']).lower()}." not in blob, it["id"]


def test_dataset_is_deterministic(tmp_path):
    dataset.write(tmp_path)
    for fam in dataset.FAMILIES:
        assert (tmp_path / f"{fam}.jsonl").read_bytes() == (dataset.DATA_DIR / f"{fam}.jsonl").read_bytes(), fam


def test_dry_run_end_to_end(tmp_path):
    out = tmp_path / "dry"
    rc = run.main(["--arms", "jev,gpt5mini,cascade", "--dry-run", "--limit", "10",
                   "--concurrency", "4", "--out", str(out)])
    assert rc == 0
    rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    assert len(rows) == 3 * 10 * len(dataset.FAMILIES)
    assert all(r["correct"] and r["error"] is None for r in rows)
    for r in rows:
        if r["arm"] == "cascade":
            assert r["jev_cost_usd"] is not None and r["fallback_cost_usd"] is not None
            assert r["cost_usd"] == pytest.approx(r["jev_cost_usd"] + r["fallback_cost_usd"])
            assert (r["fallback_cost_usd"] > 0) == bool(r["escalated"])

    assert report.main([str(out)]) == 0
    text = (out / "report.md").read_text()
    summary = json.loads((out / "summary.json").read_text())
    assert "DRY RUN" in text
    assert "\u2014" not in text  # no em dashes
    assert summary["dry_run"] is True
    for arm in ("jev", "gpt5mini", "cascade"):
        assert summary["by_arm"][arm]["accuracy"] == 1.0
        for fam in dataset.FAMILIES:
            assert summary["by_arm_family"][arm][fam]["accuracy"] == 1.0

    # resume: a second run with the same --out adds nothing
    before = (out / "results.jsonl").read_text()
    assert run.main(["--arms", "jev,gpt5mini,cascade", "--dry-run", "--limit", "10", "--out", str(out)]) == 0
    assert (out / "results.jsonl").read_text() == before


def test_dry_run_does_not_import_fabri_decide(tmp_path):
    sys.modules.pop("fabri.decide", None)
    assert run.main(["--arms", "jev", "--dry-run", "--limit", "2", "--out", str(tmp_path / "d")]) == 0
    assert "fabri.decide" not in sys.modules


def test_missing_key_skips_arm(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("PLAYBOOK_OPENAI_API_KEY", raising=False)
    rc = run.main(["--arms", "jev,gpt5mini", "--limit", "1", "--out", str(tmp_path / "live")])
    err = capsys.readouterr()
    assert rc == 1
    assert "skipping arm jev: missing env var OPENROUTER_API_KEY" in err.out
    assert "skipping arm gpt5mini: missing env var PLAYBOOK_OPENAI_API_KEY" in err.out


def test_env_file_parser_never_prints_values(tmp_path, monkeypatch, capsys):
    secret = "sk-test-DO-NOT-PRINT-123"
    env = tmp_path / "test.env"
    env.write_text(
        "# comment\n\n"
        f"OPENROUTER_API_KEY={secret}\n"
        f"export PLAYBOOK_OPENAI_API_KEY='{secret}q'\n"
        'QUOTED="a b c"\n'
        "TRAILING=value # note\n"
        "not a pair\n"
    )
    for k in ("OPENROUTER_API_KEY", "PLAYBOOK_OPENAI_API_KEY", "QUOTED", "TRAILING"):
        monkeypatch.setenv(k, "")  # registers the key so monkeypatch removes it afterwards
        monkeypatch.delenv(k)
    names = run.load_env_file(env)
    assert names == ["OPENROUTER_API_KEY", "PLAYBOOK_OPENAI_API_KEY", "QUOTED", "TRAILING"]
    import os

    assert os.environ["OPENROUTER_API_KEY"] == secret
    assert os.environ["PLAYBOOK_OPENAI_API_KEY"] == secret + "q"
    assert os.environ["QUOTED"] == "a b c"
    assert os.environ["TRAILING"] == "value"
    rc = run.main(["--arms", "jev", "--dry-run", "--limit", "1", "--env-file", str(env),
                   "--out", str(tmp_path / "e")])
    out = capsys.readouterr()
    assert rc == 0
    assert secret not in out.out and secret not in out.err
    assert "OPENROUTER_API_KEY=set" in out.out


def test_stats_helpers():
    lo, hi = report.wilson(50, 100)
    assert 0.40 < lo < 0.41 and 0.59 < hi < 0.60
    assert report.percentile([1, 2, 3, 4], 50) == 2
    assert report.percentile([], 50) is None
    assert report.ece([(1.0, True), (1.0, True)]) == pytest.approx(0.0)
    assert report.ece([(0.9, False)]) == pytest.approx(0.9)


def test_cost_split_with_real_cascade_decider():
    """The real CascadeDecider adds the primary cost into the fallback answer in
    place. The split must still come out exact. Uses stub deciders, no network."""
    decide = pytest.importorskip("fabri.decide")

    class Stub:
        def __init__(self, source, conf, cost):
            self.source, self.conf, self.cost = source, conf, cost

        def decide(self, state, questions):
            return {q.key: decide.Answer(key=q.key, kind=q.kind, value=True, confidence=self.conf,
                                         source=self.source, cost_usd=self.cost, latency_ms=5.0)
                    for q in questions}

    item = next(it for it in dataset.load(["escalate_yesno"]) if it["gold"] is True)
    arm = run.Arm("cascade", True, {}, 0)
    arm._cascade_cls = decide.CascadeDecider
    for jev_conf, escalated in ((0.3, True), (0.9, False)):
        arm._jev, arm._llm = Stub("jev", jev_conf, 0.001), Stub("llm", 0.99, 0.02)
        q = run.make_question(item, dry_run=False)
        assert isinstance(q, decide.Question)
        row = run.run_one(arm, item, run.RateLimiter(0), dry_run=False, retry_delay=0)
        assert row["escalated"] is escalated and row["correct"]
        assert row["jev_cost_usd"] == pytest.approx(0.001)
        assert row["fallback_cost_usd"] == pytest.approx(0.02 if escalated else 0.0)
        assert row["cost_usd"] == pytest.approx(0.021 if escalated else 0.001)


# ---------------------------------------------------------------- review fixes


def _row(kind, value, correct, probabilities, confidence, family="routing"):
    return {"arm": "jev", "id": f"{kind}-{value}-{confidence}", "family": family, "kind": kind,
            "tags": [], "gold": value, "value": value, "correct": correct,
            "probabilities": probabilities, "confidence": confidence}


def test_calibration_uses_probability_of_chosen_answer_not_confidence():
    # 4-way choice, p_max 0.4: Jev's normalized confidence is (0.4 - 0.25) / 0.75 = 0.2.
    # P(correct) for calibration must be 0.4.
    rows = [_row("choice", "a", True, {"a": 0.4, "b": 0.2, "c": 0.2, "d": 0.2}, 0.2),
            _row("choice", "a", False, {"a": 0.4, "b": 0.2, "c": 0.2, "d": 0.2}, 0.2)]
    cal = report.calibration(rows)
    assert cal["mean_p_chosen"] == pytest.approx(0.4)
    assert cal["mean_confidence"] == pytest.approx(0.2)
    assert cal["ece_10bin"] == pytest.approx(abs(0.5 - 0.4))  # not |0.5 - 0.2|


def test_calibration_yesno_uses_max_p_and_1_minus_p():
    # P(true) = 0.1, answer False: P(chosen) = 0.9, confidence |2p - 1| = 0.8.
    rows = [_row("yesno", False, True, {"true": 0.1, "false": 0.9}, 0.8, family="escalate_yesno")]
    cal = report.calibration(rows)
    assert cal["mean_p_chosen"] == pytest.approx(0.9)
    assert cal["at_threshold"]["0.8"]["coverage"] == 1.0
    assert cal["at_threshold"]["0.95"]["coverage"] == 0.0  # conf 0.8 < 0.95, and P = 0.9 < 0.95 too
    assert cal["ece_10bin"] == pytest.approx(0.1)
    # Without probabilities, P(chosen) is rebuilt from |2p - 1|: (1 + 0.8) / 2.
    no_probs = [dict(rows[0], probabilities=None)]
    assert report.calibration(no_probs)["mean_p_chosen"] == pytest.approx(0.9)
    assert report.p_true(no_probs[0]) == pytest.approx(0.1)


def test_threshold_coverage_is_on_p_chosen():
    # 3-way choice with p_max 0.7: confidence (0.7 - 1/3) / (2/3) = 0.55, below 0.6.
    rows = [_row("choice", "a", True, {"a": 0.7, "b": 0.2, "c": 0.1}, 0.55)]
    cal = report.calibration(rows)
    assert cal["at_threshold"]["0.6"]["coverage"] == 1.0
    assert cal["at_threshold"]["0.6"]["accuracy"] == 1.0


def test_calibration_computed_once_per_family(monkeypatch):
    calls = []
    real = report.calibration
    monkeypatch.setattr(report, "calibration", lambda rows: calls.append(len(rows)) or real(rows))
    rows = [_row("choice", "a", True, {"a": 0.9, "b": 0.1}, 0.8, family=f) for f in ("routing", "tool_select")]
    report.build_summary(rows, [])
    assert len(calls) == 3  # once for the arm, once per family


def test_report_uses_run_bool_parser():
    assert report.to_bool is run.to_bool
    assert not hasattr(report, "_to_bool")


def test_report_renders_calibration_columns(tmp_path):
    out = tmp_path / "dry"
    assert run.main(["--arms", "jev", "--dry-run", "--limit", "3", "--out", str(out)]) == 0
    assert report.main([str(out)]) == 0
    text = (out / "report.md").read_text()
    assert "mean raw conf" in text and "acc at P >= 0.6" in text
    summary = json.loads((out / "summary.json").read_text())
    assert summary["calibration"]["jev"]["n_with_p"] == 3 * len(dataset.FAMILIES)


def _stub_answer_cls():
    decide = pytest.importorskip("fabri.decide")
    return decide


def test_unpriced_answer_row_has_null_cost():
    decide = _stub_answer_cls()

    class Unpriced:
        def decide(self, state, questions):
            return {q.key: decide.Answer(key=q.key, kind=q.kind, value=True, confidence=0.9, source="jev",
                                         cost_usd=0.0, cost_known=False, model="mystery") for q in questions}

    item = next(it for it in dataset.load(["escalate_yesno"]) if it["gold"] is True)
    arm = run.Arm("jev", True, {}, 0)
    arm._jev = Unpriced()
    row = run.run_one(arm, item, run.RateLimiter(0), dry_run=False, retry_delay=0)
    assert row["error"] is None and row["correct"]
    assert row["cost_usd"] is None


class _Flaky:
    """Fails (an errored answer that still cost money) on the first call, then answers."""

    def __init__(self, decide, source, cost, conf=0.9, fail_first=True):
        self.decide_mod, self.source, self.cost, self.conf = decide, source, cost, conf
        self.calls = 0
        self.fail_first = fail_first

    def decide(self, state, questions):
        self.calls += 1
        bad = self.fail_first and self.calls == 1
        return {q.key: self.decide_mod.Answer(
            key=q.key, kind=q.kind, value=None if bad else True, confidence=None if bad else self.conf,
            source=self.source, cost_usd=self.cost, latency_ms=5.0, error="boom" if bad else None)
            for q in questions}


def test_retried_item_cost_sums_all_attempts_single_arm():
    decide = _stub_answer_cls()
    item = next(it for it in dataset.load(["escalate_yesno"]) if it["gold"] is True)
    arm = run.Arm("jev", True, {}, 0)
    arm._jev = _Flaky(decide, "jev", 0.001)
    row = run.run_one(arm, item, run.RateLimiter(0), dry_run=False, retry_delay=0)
    assert row["attempts"] == 2 and row["error"] is None
    assert row["cost_usd"] == pytest.approx(0.002)


def test_retried_item_cost_sums_all_attempts_cascade():
    decide = _stub_answer_cls()
    item = next(it for it in dataset.load(["escalate_yesno"]) if it["gold"] is True)
    arm = run.Arm("cascade", True, {}, 0)
    arm._cascade_cls = decide.CascadeDecider
    # Attempt 1: Jev errored, fallback errored too -> row error, retry.
    # Attempt 2: Jev errored again, fallback answers.
    arm._jev = _Flaky(decide, "jev", 0.001, fail_first=False)
    arm._jev.decide = lambda state, questions: {q.key: decide.Answer(
        key=q.key, kind=q.kind, value=None, source="jev", cost_usd=0.001, error="x") for q in questions}
    arm._llm = _Flaky(decide, "llm", 0.02)
    row = run.run_one(arm, item, run.RateLimiter(0), dry_run=False, retry_delay=0)
    assert row["attempts"] == 2 and row["error"] is None
    assert row["jev_cost_usd"] == pytest.approx(0.002)
    assert row["fallback_cost_usd"] == pytest.approx(0.04)
    assert row["cost_usd"] == pytest.approx(row["jev_cost_usd"] + row["fallback_cost_usd"])
