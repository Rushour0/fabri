"""Synchronize the built Fabri Studio assets into the wheel package tree."""

from __future__ import annotations

from pathlib import Path
import shutil


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "examples" / "studio" / "dist"
DEST_ROOT = REPO_ROOT / "src" / "fabri" / "service" / "studio_assets"
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


def main() -> None:
    if not SOURCE_ROOT.is_dir():
        print(
            f"Studio build not found at {SOURCE_ROOT}; skipping asset sync. "
            "Run the Studio build first when preparing a wheel."
        )
        return

    _clear_generated_content()
    PACKAGE_STUB.touch(exist_ok=True)
    shutil.copytree(SOURCE_ROOT, DEST_ROOT, dirs_exist_ok=True)
    print(f"Synced Fabri Studio assets from {SOURCE_ROOT} to {DEST_ROOT}")


if __name__ == "__main__":
    main()
