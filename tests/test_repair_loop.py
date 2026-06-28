"""B8 — bounded verify -> repair -> rerun loop (docs/design/repair-loop.md).

Fully offline: a ScriptedLLMBackend drives the agent, a fake store keeps the
run cold (no embeddings / no Qdrant), and the verifier is a tiny stdlib script
toggled by a counter file (the "sentinel"). Covers: (a) disabled => byte-for-
byte the old single run, (b) fail-then-fix succeeds within max_attempts, (c) a
persistent same-signature failure stops on no-progress, (d) changing-signature
failures stop at the max_attempts bound, and (e) the no-verify_command path that
falls back to the run's own failure outcome as the signal."""
import sys
import uuid
from pathlib import Path

from fabri import ScriptedLLMBackend, ToolRegistry, run_agent
from fabri.core.llm import LLMError, LLMResponse
from fabri.orchestrator.traces import read_trace

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "src" / "fabri" / "tools" / "examples"


class _ColdStore:
    """Minimal store stub: `count()==0` makes retrieval a no-op, so no
    embedding model loads and no Qdrant connection is attempted."""

    def count(self) -> int:
        return 0


def _tools() -> ToolRegistry:
    # Loading manifests only reads JSON; no tool is invoked in these tests.
    return ToolRegistry(EXAMPLES_DIR)


def _verify_after_n_checks(tmp_path: Path, threshold: int) -> list[str]:
    """A verifier that fails until it has been called `threshold` times, then
    passes -- the canonical 'sentinel file toggled across attempts' fixture."""
    counter = tmp_path / "checks.count"
    script = tmp_path / "verify_toggle.py"
    script.write_text(
        "import sys, pathlib\n"
        "c = pathlib.Path(sys.argv[1])\n"
        "n = (int(c.read_text()) if c.exists() else 0) + 1\n"
        "c.write_text(str(n))\n"
        "thr = int(sys.argv[2])\n"
        "if n >= thr:\n"
        "    print('ok after %d checks' % n); sys.exit(0)\n"
        "print('FAIL: sentinel missing on check %d' % n); sys.exit(1)\n"
    )
    return [sys.executable, str(script), str(counter), str(threshold)]


def _verify_constant_failure(tmp_path: Path) -> list[str]:
    script = tmp_path / "verify_const.py"
    script.write_text(
        "import sys\n"
        "print('FAIL: persistent error at module:1'); sys.exit(1)\n"
    )
    return [sys.executable, str(script)]


def _verify_changing_failure(tmp_path: Path) -> list[str]:
    """Always fails, but with a different message each call so the error
    signature changes -- exercises the max_attempts bound, not no-progress."""
    counter = tmp_path / "iter.count"
    script = tmp_path / "verify_changing.py"
    script.write_text(
        "import sys, pathlib\n"
        "c = pathlib.Path(sys.argv[1])\n"
        "n = (int(c.read_text()) if c.exists() else 0) + 1\n"
        "c.write_text(str(n))\n"
        "print('FAIL: iteration %d still broken' % n); sys.exit(1)\n"
    )
    return [sys.executable, str(script), str(counter)]


def _verify_marker_on_run(tmp_path: Path) -> tuple[list[str], Path]:
    """A verifier that records the fact it ran by touching a marker file -- lets
    a test prove the verifier was NOT consulted when repair is disabled."""
    marker = tmp_path / "verifier_ran.marker"
    script = tmp_path / "verify_marker.py"
    script.write_text(
        "import sys, pathlib\n"
        "pathlib.Path(sys.argv[1]).write_text('ran'); sys.exit(1)\n"
    )
    return [sys.executable, str(script), str(marker)], marker


def _types(session_id: str) -> list[str]:
    return [e["type"] for e in read_trace(session_id)]


# --- (a) disabled: identical to the pre-repair single run -------------------

def test_repair_disabled_is_a_plain_single_run(tmp_path):
    cmd, marker = _verify_marker_on_run(tmp_path)
    backend = ScriptedLLMBackend([LLMResponse(final_text="done")])
    result = run_agent(
        "task", backend, _tools(), _ColdStore(),
        session_id=str(uuid.uuid4()),
        repair={"enabled": False, "verify_command": cmd, "verify_cwd": str(tmp_path)},
    )
    assert result["success"] is True
    assert result["final_text"] == "done"
    types = _types(result["session_id"])
    assert types.count("start") == 1            # exactly one attempt
    assert "repair_attempt" not in types
    assert "repair_aborted" not in types
    assert not marker.exists()                  # verifier never consulted


def test_repair_none_matches_disabled(tmp_path):
    backend = ScriptedLLMBackend([LLMResponse(final_text="done")])
    result = run_agent("task", backend, _tools(), _ColdStore())
    assert result["success"] is True
    assert "repair_attempt" not in _types(result["session_id"])


# --- (b) fail then fix within max_attempts ----------------------------------

def test_repair_fails_then_fixes_within_budget(tmp_path):
    cmd = _verify_after_n_checks(tmp_path, threshold=2)
    backend = ScriptedLLMBackend([
        LLMResponse(final_text="first pass"),   # initial attempt
        LLMResponse(final_text="repaired"),     # one repair re-run
    ])
    result = run_agent(
        "task", backend, _tools(), _ColdStore(),
        session_id=str(uuid.uuid4()),
        repair={
            "enabled": True,
            "max_attempts": 2,
            "verify_command": cmd,
            "verify_cwd": str(tmp_path),
        },
    )
    assert result["success"] is True
    assert result["final_text"] == "repaired"   # the repaired run's result wins
    types = _types(result["session_id"])
    assert types.count("start") == 2            # initial + one repair
    assert types.count("repair_attempt") == 1
    assert "repair_aborted" not in types        # the fix was detected
    # verifier ran twice: once to find the failure, once to confirm the fix.
    assert (tmp_path / "checks.count").read_text() == "2"


# --- (c) persistent same-signature failure: stop on no-progress -------------

def test_repair_stops_on_no_progress(tmp_path):
    cmd = _verify_constant_failure(tmp_path)
    backend = ScriptedLLMBackend([LLMResponse(final_text=f"try {i}") for i in range(5)])
    result = run_agent(
        "task", backend, _tools(), _ColdStore(),
        session_id=str(uuid.uuid4()),
        repair={
            "enabled": True,
            "max_attempts": 3,
            "verify_command": cmd,
            "verify_cwd": str(tmp_path),
            "stop_on_no_progress": True,
        },
    )
    types = _types(result["session_id"])
    # initial + exactly one re-run, then the unchanged signature aborts before
    # the budget (3) is spent.
    assert types.count("start") == 2
    assert types.count("repair_attempt") == 1
    aborts = [e for e in read_trace(result["session_id"]) if e["type"] == "repair_aborted"]
    assert len(aborts) == 1
    assert aborts[0]["reason"] == "error_signature_unchanged"


# --- (d) changing-signature failure: stop at the max_attempts bound ---------

def test_repair_stops_at_max_attempts(tmp_path):
    cmd = _verify_changing_failure(tmp_path)
    backend = ScriptedLLMBackend([LLMResponse(final_text=f"try {i}") for i in range(6)])
    result = run_agent(
        "task", backend, _tools(), _ColdStore(),
        session_id=str(uuid.uuid4()),
        repair={
            "enabled": True,
            "max_attempts": 2,
            "verify_command": cmd,
            "verify_cwd": str(tmp_path),
            "stop_on_no_progress": True,   # never triggers: signatures differ
        },
    )
    types = _types(result["session_id"])
    assert types.count("start") == 3            # initial + two re-runs (the cap)
    assert types.count("repair_attempt") == 2
    aborts = [e for e in read_trace(result["session_id"]) if e["type"] == "repair_aborted"]
    assert len(aborts) == 1
    assert aborts[0]["reason"] == "max_attempts"


# --- (e) no verify_command: fall back to the run's own failure outcome ------

def test_repair_uses_outcome_signal_when_no_verify_command(tmp_path):
    class _BoomBackend:
        def step(self, system, messages):
            raise LLMError("provider down")

    result = run_agent(
        "task", _BoomBackend(), _tools(), _ColdStore(),
        session_id=str(uuid.uuid4()),
        repair={"enabled": True, "max_attempts": 3, "verify_command": None},
    )
    assert result["success"] is False
    assert result["outcome"] == "failed"
    types = _types(result["session_id"])
    # The failure outcome is the signal; it's identical every re-run, so the
    # loop re-runs once then aborts on the unchanged signature.
    assert types.count("start") == 2
    assert types.count("repair_attempt") == 1
    aborts = [e for e in read_trace(result["session_id"]) if e["type"] == "repair_aborted"]
    assert aborts and aborts[0]["reason"] == "error_signature_unchanged"
