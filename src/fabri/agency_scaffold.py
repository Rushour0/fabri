"""Write a bundled agency template to disk."""

from __future__ import annotations

from pathlib import Path

from fabri.agency_templates import TEMPLATES

_AGENCY_ROOT = "__AGENCY_ROOT__"
_RUN_FROM = "__RUN_FROM__"


def scaffold_agency(
    name: str,
    template: str,
    dest_dir: str | Path,
) -> list[Path]:
    """Create ``template`` below ``dest_dir/name`` and return written paths."""
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError("agency name must be a single directory name")
    if template not in TEMPLATES:
        choices = ", ".join(sorted(TEMPLATES))
        raise ValueError(f"unknown agency template {template!r}; choose from: {choices}")

    agency_dir = Path(dest_dir) / name
    if agency_dir.exists():
        raise FileExistsError(f"destination already exists: {agency_dir}")

    template_data = TEMPLATES[template]
    contents = dict(template_data["FILES"])
    contents["README.md"] = template_data["README"]
    prefix = str(agency_dir)
    run_from = str(Path.cwd())
    created: list[Path] = []

    for relative_name, raw_content in contents.items():
        relative_path = Path(relative_name)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"template contains an unsafe path: {relative_name}")
        output_path = agency_dir / relative_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        content = raw_content.replace(_AGENCY_ROOT, prefix).replace(_RUN_FROM, run_from)
        output_path.write_text(content)
        created.append(output_path)

    return created


def agency_next_steps(name: str, dest: str | Path) -> str:
    """Return the commands that start a newly scaffolded agency and Studio."""
    config = Path(dest) / name / "agent.openai.yaml"
    return f"Now run:\n  fabri serve --config {config}\n  fabri studio"
