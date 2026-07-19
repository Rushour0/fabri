"""Install and describe a roster catalog for Fabri Studio."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path

from fabri.agency_registry import resolve_source
from fabri.agency_scaffold import write_template
from fabri.company import company_org, compile_company

_LOG = logging.getLogger("fabri")


def load_catalog(catalog_dir: str | Path, work_dir: Path) -> dict[str, dict]:
    """Pre-install each roster entry and return its runnable configuration.

    A broken agency or company is logged and omitted so the rest of a roster
    remains available to Studio.
    """
    source_dir = Path(catalog_dir)
    try:
        index = json.loads((source_dir / "index.json").read_text())
    except FileNotFoundError as exc:
        raise ValueError(f"catalog index not found: {source_dir / 'index.json'}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read catalog index: {source_dir / 'index.json'}") from exc

    if not isinstance(index, dict):
        raise ValueError("catalog index must be a JSON object")
    catalog: dict[str, dict] = {}
    _install_agencies(index.get("agencies", []), source_dir, work_dir, catalog)
    _install_companies(index.get("companies", []), source_dir, work_dir, catalog)
    return catalog


def _install_agencies(
    entries: object, source_dir: Path, work_dir: Path, catalog: dict[str, dict]
) -> None:
    if not isinstance(entries, list):
        _LOG.warning("catalog agencies must be a list; skipping agencies")
        return
    for meta in entries:
        name = _entry_name(meta, "agency")
        if name is None:
            continue
        if name in catalog:
            _LOG.warning("skipping duplicate catalog ref %r", name)
            continue
        try:
            files, readme, entry = resolve_source(str(source_dir / "agencies" / name))
            agency_dir = work_dir / "agencies" / name
            entry_path = Path(entry)
            if entry_path.is_absolute() or ".." in entry_path.parts:
                raise ValueError(f"registry agency has an unsafe entry path: {entry!r}")
            write_template(
                agency_dir,
                files,
                readme,
                run_from=str(Path.cwd()),
                slug=name,
            )
            config = agency_dir / entry_path
            if not config.is_file():
                raise ValueError(f"agency entry config not found: {config}")
        except (OSError, ValueError) as exc:
            _LOG.warning("skipping catalog agency %r: %s", name, exc)
            continue
        catalog[name] = {"kind": "agency", "config": config, "meta": dict(meta), "org": None}


def _install_companies(
    entries: object, source_dir: Path, work_dir: Path, catalog: dict[str, dict]
) -> None:
    if not isinstance(entries, list):
        _LOG.warning("catalog companies must be a list; skipping companies")
        return
    for meta in entries:
        name = _entry_name(meta, "company")
        if name is None:
            continue
        if name in catalog:
            _LOG.warning("skipping duplicate catalog ref %r", name)
            continue
        company_toml = source_dir / "companies" / name / "company.toml"
        try:
            config = compile_company(company_toml, work_dir / "companies")
            org = company_org(company_toml)
        except (OSError, ValueError) as exc:
            _LOG.warning("skipping catalog company %r: %s", name, exc)
            continue
        catalog[name] = {"kind": "company", "config": config, "meta": dict(meta), "org": org}


def _entry_name(meta: object, kind: str) -> str | None:
    if not isinstance(meta, dict):
        _LOG.warning("skipping catalog %s with non-object metadata", kind)
        return None
    name = meta.get("name")
    if not isinstance(name, str) or not name or Path(name).name != name:
        _LOG.warning("skipping catalog %s with invalid name %r", kind, name)
        return None
    return name


def catalog_listing(catalog: Mapping[str, dict]) -> dict:
    """Return only display-safe catalog metadata for the Studio API."""
    listing: dict[str, list[dict]] = {"agencies": [], "companies": []}
    for ref, item in catalog.items():
        kind = item["kind"]
        display = {**item["meta"], "ref": ref, "kind": kind}
        if kind == "company":
            display["org"] = item["org"]
        listing["agencies" if kind == "agency" else "companies"].append(display)
    return listing
