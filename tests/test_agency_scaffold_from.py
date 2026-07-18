from pathlib import Path

from fabri.agency_registry import resolve_source
from fabri.agency_scaffold import _slug, agency_next_steps, write_template


def test_resolve_and_scaffold_local_registry_agency(
    tmp_path: Path,
    monkeypatch,
) -> None:
    files, readme, entry = resolve_source("tests/fixtures/registry_agency")

    assert {"agency.toml", "agent.openai.yaml", "workspace/.gitignore"} <= files.keys()
    assert readme
    assert entry == "agent.openai.yaml"
    assert "README.md" not in files

    monkeypatch.chdir(tmp_path)
    agency_dir = tmp_path / "my-crew"
    write_template(
        agency_dir,
        files,
        readme,
        run_from=str(Path.cwd()),
        slug=_slug("my-crew"),
    )

    parent_config = (agency_dir / "agent.openai.yaml").read_text()
    rendered_readme = (agency_dir / "README.md").read_text()
    assert (agency_dir / "workspace/.gitignore").is_file()
    assert "my_crew_parent" in parent_config
    for token in ("__AGENCY_ROOT__", "__AGENCY_SLUG__", "__RUN_FROM__"):
        assert token not in parent_config
        assert token not in rendered_readme

    assert str(agency_dir / "agent.openai.yaml") in agency_next_steps(
        "my-crew", tmp_path, entry
    )


def test_parse_github_source() -> None:
    from fabri.agency_registry import _parse_github_source

    assert _parse_github_source("gh:owner/repo/path/to/agency@v1") == (
        "owner/repo",
        "path/to/agency",
        "v1",
    )
