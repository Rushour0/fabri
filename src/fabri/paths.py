"""Where this package writes per-run state (logs + JSONL traces).

Both used to live next to the source (`Path(__file__).parent.parent/...`),
which breaks once the package is pip-installed into site-packages: that dir is
the wrong place to write to, often read-only, and shared across every project
using the same install. Instead state is project-local: it lands under
`<home>/.fabri/`, where `<home>` is `$FABRI_HOME` if set, else the
current working directory. Sub-agent subprocesses inherit both the env var and
the cwd, so a parent run and its sub-agents always write to the same place."""
import os
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("FABRI_HOME", Path.cwd())).resolve()


def logs_dir() -> Path:
    d = home() / ".fabri" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def traces_dir() -> Path:
    d = home() / ".fabri" / "traces"
    d.mkdir(parents=True, exist_ok=True)
    return d


def locks_dir() -> Path:
    d = home() / ".fabri" / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d
