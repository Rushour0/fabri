"""fabri.style: the plain-writing rules and the AI-tell counter."""

from __future__ import annotations

import yaml

from fabri.agency_templates import TEMPLATES
from fabri.style import PLAIN_WRITING_RULES, slop_report


def test_clean_text_scores_zero():
    text = "Moved the chart to the top of the canvas. The weekly totals now read 41, 38 and 52."
    report = slop_report(text)
    assert report.total == 0
    assert report.words == len(text.split())


def test_each_pattern_is_counted():
    text = (
        "Great question! Let's delve into this robust, seamless setup — it's not just a chart, "
        "it's a story. It's worth noting the “new” layout. \U0001F680 Let me know if you need more."
    )
    report = slop_report(text)
    for name in ("opener", "banned_word", "dash", "false_contrast", "filler", "curly_quote", "emoji", "closer"):
        assert report.counts.get(name), name
    assert report.counts["banned_word"] == 3
    assert report.per_100_words > 0


def test_opener_only_checked_on_first_line():
    assert slop_report("Done.\nCertainly the chart moved.").counts.get("opener") is None
    assert slop_report("Certainly! Done.").counts["opener"] == 1


def test_rules_contain_no_tells_themselves():
    # The rules name banned words on purpose; strip that line before checking.
    body = "\n".join(l for l in PLAIN_WRITING_RULES.splitlines() if "delve" not in l and "harness" not in l)
    report = slop_report(body)
    assert report.counts.get("dash") is None
    assert report.counts.get("emoji") is None


def test_jev_router_specialists_carry_the_rules():
    files = TEMPLATES["jev-router"]["FILES"]
    for name in ("chat.openai.yaml", "canvas.openai.yaml", "tasks.openai.yaml"):
        cfg = yaml.safe_load(files[name])
        prefix = cfg["agent"]["system_prompt_prefix"]
        assert "Do not use em dashes" in prefix
        assert cfg["agent"]["system_prompt"].strip()
