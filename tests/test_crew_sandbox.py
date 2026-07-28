import os
import re
from pathlib import Path

import pytest

from fabri.agency_scaffold import write_template
from fabri.runtime import build_tools


def test_override_wins_over_config_sandbox_root(tmp_path, monkeypatch):
    cfg_root = tmp_path / "cfg-root"
    override_root = tmp_path / "override-root"
    manifest_dir = tmp_path / "manifests"
    cfg_root.mkdir()
    override_root.mkdir()
    manifest_dir.mkdir()

    monkeypatch.setenv("FABRI_SANDBOX_ROOT_OVERRIDE", str(override_root))

    registry = build_tools(
        {
            "sandbox_root": str(cfg_root),
            "manifest_dir": str(manifest_dir),
            "enabled": None,
        }
    )

    assert os.environ["FABRI_SANDBOX_ROOT_OVERRIDE"] == str(override_root)
    assert registry.sandbox_root == str(Path(override_root).resolve())


def test_falls_back_to_config_when_override_unset(tmp_path, monkeypatch):
    cfg_root = tmp_path / "cfg-root"
    manifest_dir = tmp_path / "manifests"
    cfg_root.mkdir()
    manifest_dir.mkdir()

    monkeypatch.delenv("FABRI_SANDBOX_ROOT_OVERRIDE", raising=False)

    registry = build_tools(
        {
            "sandbox_root": str(cfg_root),
            "manifest_dir": str(manifest_dir),
            "enabled": None,
        }
    )

    assert registry.sandbox_root == str(Path(cfg_root).resolve())


def test_empty_override_falls_back_to_config(tmp_path, monkeypatch):
    cfg_root = tmp_path / "cfg-root"
    manifest_dir = tmp_path / "manifests"
    cfg_root.mkdir()
    manifest_dir.mkdir()

    monkeypatch.setenv("FABRI_SANDBOX_ROOT_OVERRIDE", "")

    registry = build_tools(
        {
            "sandbox_root": str(cfg_root),
            "manifest_dir": str(manifest_dir),
            "enabled": None,
        }
    )

    assert registry.sandbox_root == str(Path(cfg_root).resolve())


def test_materialize_leaves_no_placeholder_tokens(tmp_path):
    agency_dir = tmp_path / "agency"
    run_from = str(tmp_path / "runfrom")
    slug = "bug-triage-crew"
    files = {
        "manager.yaml": (
            "root: __AGENCY_ROOT__\n"
            "run_from: __RUN_FROM__\n"
            "slug: __AGENCY_SLUG__\n"
        ),
        "specialist.yaml": (
            "sandbox: __AGENCY_ROOT__\n"
            "name: __AGENCY_SLUG__\n"
        ),
    }
    readme = (
        "# __AGENCY_SLUG__ crew, run from __RUN_FROM__ at __AGENCY_ROOT__"
    )

    written_paths = write_template(
        agency_dir=agency_dir,
        files=files,
        readme=readme,
        run_from=run_from,
        slug=slug,
    )

    placeholder_tokens = (
        "__AGENCY_ROOT__",
        "__RUN_FROM__",
        "__AGENCY_SLUG__",
    )
    for path in written_paths:
        text = path.read_text(encoding="utf-8")
        for token in placeholder_tokens:
            assert token not in text
        assert re.search(r"__[A-Z][A-Z0-9_]*__", text) is None

    manager_text = (agency_dir / "manager.yaml").read_text(encoding="utf-8")
    assert str(agency_dir) in manager_text
    assert run_from in manager_text
    assert slug in manager_text


def test_override_reaches_every_role_registry(tmp_path, monkeypatch):
    roster = Path(
        "/Users/rushour0/gba/fabri-rosters/agencies/bug-triage-crew"
    )
    if not roster.is_dir():
        pytest.skip("rosters checkout not present")

    override_root = tmp_path / "override-root"
    manager_cfg_root = tmp_path / "manager-config-root"
    specialist_cfg_root = tmp_path / "specialist-config-root"
    manifest_dir = tmp_path / "manifests"
    override_root.mkdir()
    manager_cfg_root.mkdir()
    specialist_cfg_root.mkdir()
    manifest_dir.mkdir()

    monkeypatch.setenv("FABRI_SANDBOX_ROOT_OVERRIDE", str(override_root))

    manager_registry = build_tools(
        {
            "sandbox_root": str(manager_cfg_root),
            "manifest_dir": str(manifest_dir),
            "enabled": None,
        }
    )
    specialist_registry = build_tools(
        {
            "sandbox_root": str(specialist_cfg_root),
            "manifest_dir": str(manifest_dir),
            "enabled": None,
        }
    )

    expected_root = str(Path(override_root).resolve())
    assert manager_registry.sandbox_root == expected_root
    assert specialist_registry.sandbox_root == expected_root
