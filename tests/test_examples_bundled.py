from __future__ import annotations

from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "examples" / "agencies"
BUNDLED_ROOT = REPO_ROOT / "src" / "fabri" / "examples" / "agencies"


def _tracked_source_paths() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "examples/agencies"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    return sorted(
        (REPO_ROOT / raw.decode()).relative_to(SOURCE_ROOT)
        for raw in result.stdout.split(b"\0")
        if raw
    )


def test_bundled_examples_match_tracked_sources() -> None:
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "sync_examples.py")],
        cwd=REPO_ROOT,
        check=True,
    )

    source_paths = _tracked_source_paths()
    bundled_paths = sorted(
        path.relative_to(BUNDLED_ROOT)
        for path in BUNDLED_ROOT.rglob("*")
        if path.is_file() and path.relative_to(BUNDLED_ROOT) != Path("__init__.py")
    )
    assert bundled_paths == source_paths

    for relative in source_paths:
        assert (BUNDLED_ROOT / relative).read_bytes() == (SOURCE_ROOT / relative).read_bytes()

    assert (BUNDLED_ROOT / "bug-triage-crew" / "workspace" / ".gitignore").is_file()
