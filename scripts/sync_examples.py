"""Synchronize tracked example agencies into fabri's wheel package tree."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "examples" / "agencies"
DEST_ROOT = REPO_ROOT / "src" / "fabri" / "examples" / "agencies"
PACKAGE_STUB = DEST_ROOT / "__init__.py"


def _clear_generated_content() -> None:
    DEST_ROOT.mkdir(parents=True, exist_ok=True)
    for path in DEST_ROOT.iterdir():
        if path == PACKAGE_STUB:
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def _tracked_source_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "examples/agencies"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    return [REPO_ROOT / path.decode() for path in result.stdout.split(b"\0") if path]


def main() -> None:
    _clear_generated_content()
    PACKAGE_STUB.touch(exist_ok=True)

    copied = 0
    for source in _tracked_source_files():
        relative = source.relative_to(SOURCE_ROOT)
        destination = DEST_ROOT / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied += 1
    print(f"Synced {copied} tracked example agency files to {DEST_ROOT}")


if __name__ == "__main__":
    main()
