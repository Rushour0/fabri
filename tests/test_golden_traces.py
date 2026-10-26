"""A run with no hooks must produce the same normalized trace as before run
hooks existed. See tests/golden_scenarios.py for the scripted runs."""
import pytest

from tests.golden_scenarios import SCENARIOS, golden_path, regen_requested, run_normalized


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_trace_matches_golden(name, tmp_path, monkeypatch):
    monkeypatch.setenv("FABRI_HOME", str(tmp_path))
    got = run_normalized(name, tmp_path)
    path = golden_path(name)
    if regen_requested():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(got)
    assert got == path.read_text()
