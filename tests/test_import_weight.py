"""Import-weight regression tests.

Every fabri subprocess (the manager, each specialist via
tools/agent_runner_tool.py, and hosts that embed fabri.service) pays for the
modules `import fabri.cli` drags in. litellm and qdrant_client used to load at
import time and cost several seconds and ~200 MB RSS per process. These tests
check module presence in a fresh interpreter, not RSS or timing, so they stay
stable across machines.
"""

import json
import subprocess
import sys

import pytest

HEAVY = ("litellm", "qdrant_client", "torch", "transformers", "sentence_transformers")


def _modules_after(code: str) -> set[str]:
    probe = code + "\nimport json, sys\nprint(json.dumps(sorted(sys.modules)))\n"
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return set(json.loads(out.stdout.strip().splitlines()[-1]))


@pytest.mark.parametrize(
    "entry_point",
    ["fabri.cli", "fabri.tools.agent_runner_tool", "fabri.service"],
)
def test_entry_point_does_not_import_heavy_modules(entry_point):
    loaded = _modules_after(f"import {entry_point}")
    assert entry_point in loaded
    heavy = sorted(m for m in HEAVY if m in loaded)
    assert heavy == [], f"import {entry_point} loaded heavy modules: {heavy}"


def test_pricing_a_snapshot_model_does_not_import_litellm():
    loaded = _modules_after(
        "from fabri.core.llm import LLMUsage\n"
        "from fabri.pricing import cost_for\n"
        "assert cost_for(LLMUsage(input_tokens=1000, output_tokens=1000, model='claude-sonnet-4-6'))\n"
    )
    assert "litellm" not in loaded


def test_snapshot_matches_installed_litellm():
    """The snapshot is a cache of litellm's answer, so it must agree with it.

    If this fails after a litellm upgrade, regenerate _LITELLM_SNAPSHOT from
    `_litellm_rates_for` for the listed ids.
    """
    pytest.importorskip("litellm")
    from fabri import pricing

    drift = {}
    for model, rates in pricing._LITELLM_SNAPSHOT.items():
        live = pricing._litellm_rates_for(model)
        if live is None or any(abs(a - b) > 1e-9 for a, b in zip(rates, live)):
            drift[model] = (rates, live)
    assert drift == {}, f"snapshot differs from litellm: {drift}"


def test_unknown_to_snapshot_still_uses_litellm():
    """A model outside the snapshot falls through to the lazily loaded litellm map."""
    pytest.importorskip("litellm")
    from fabri import pricing

    model = "claude-sonnet-4-6-20991231"  # date-suffixed: not an exact snapshot key
    assert model not in pricing._LITELLM_SNAPSHOT
    assert pricing._rates_for(model) == pricing._litellm_rates_for(model)
    assert pricing._load_litellm() is not None
