"""Resolve agency templates from local directories or GitHub repositories."""

from __future__ import annotations

import subprocess
import tempfile
import tomllib
from pathlib import Path


_DEFAULT_ENTRY = "agent.openai.yaml"


def _parse_github_source(source: str) -> tuple[str, str, str | None]:
    """Return owner/repo, template subpath, and optional ref for a gh: source."""
    spec = source.removeprefix("gh:")
    if "@" in spec:
        spec, ref = spec.rsplit("@", 1)
        if not ref:
            raise ValueError("GitHub source ref cannot be empty")
    else:
        ref = None

    parts = [part for part in spec.split("/") if part]
    if len(parts) < 3:
        raise ValueError(
            "GitHub source must be gh:owner/repo/subpath[@ref]"
        )
    return "/".join(parts[:2]), "/".join(parts[2:]), ref


def _read_agency_directory(agency_dir: Path) -> tuple[dict[str, str], str, str]:
    try:
        metadata = tomllib.loads((agency_dir / "agency.toml").read_text())
    except FileNotFoundError as exc:
        raise ValueError(f"registry agency is missing agency.toml: {agency_dir}") from exc
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"could not read registry agency metadata: {agency_dir}") from exc

    agency = metadata.get("agency", {})
    if not isinstance(agency, dict):
        raise ValueError(f"registry agency metadata has invalid [agency] table: {agency_dir}")
    entry = agency.get("entry", _DEFAULT_ENTRY)
    if not isinstance(entry, str) or not entry:
        raise ValueError(f"registry agency metadata has invalid entry: {agency_dir}")

    files: dict[str, str] = {}
    readme = ""
    try:
        for path in agency_dir.rglob("*"):
            relative = path.relative_to(agency_dir)
            if (
                ".git" in relative.parts
                or "__pycache__" in relative.parts
                or path.name == ".DS_Store"
                or path.suffix == ".pyc"
                or not path.is_file()
            ):
                continue
            content = path.read_text()
            if relative.as_posix() == "README.md":
                readme = content
            else:
                files[relative.as_posix()] = content
    except (OSError, UnicodeDecodeError) as exc:
        raise OSError(f"could not read registry agency files: {agency_dir}") from exc

    return files, readme, entry


def resolve_source(source: str) -> tuple[dict[str, str], str, str]:
    """Return ``(files, readme, entry)`` resolved fully into memory."""
    local_dir = Path(source)
    if local_dir.is_dir():
        return _read_agency_directory(local_dir)
    if not source.startswith("gh:"):
        raise ValueError(f"registry source is not a directory: {source}")

    repo, subpath, ref = _parse_github_source(source)
    with tempfile.TemporaryDirectory() as temp_dir:
        clone_dir = Path(temp_dir) / "repository"
        command = ["git", "clone", "--depth", "1"]
        if ref is not None:
            command.extend(["--branch", ref])
        command.extend([f"https://github.com/{repo}", str(clone_dir)])
        try:
            subprocess.run(command, check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise OSError(f"could not clone registry source {source}: {exc}") from exc

        agency_dir = clone_dir / subpath
        if not agency_dir.is_dir():
            raise ValueError(f"registry agency directory not found: {subpath}")
        return _read_agency_directory(agency_dir)
