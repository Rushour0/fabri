"""Compile a declarative company TOML into nested fabri agent configs."""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

from fabri.agency_registry import resolve_source
from fabri.agency_scaffold import write_template


class CompanyError(ValueError):
    """Raised when a company.toml does not describe one rooted tree."""


def load_company(path: str | Path) -> dict:
    """Parse and validate a company TOML, returning its unchanged data."""
    company_path = Path(path)
    try:
        data = tomllib.loads(company_path.read_text())
    except FileNotFoundError as exc:
        raise CompanyError(f"company file not found: {company_path}") from exc
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise CompanyError(f"could not read company TOML: {company_path}: {exc}") from exc

    company = data.get("company")
    nodes = data.get("node")
    if not isinstance(company, dict):
        raise CompanyError("company.toml must contain a [company] table")
    if not isinstance(company.get("name"), str) or not company["name"]:
        raise CompanyError("company.name must be a non-empty string")
    if not isinstance(company.get("memory_namespace"), str) or not company["memory_namespace"]:
        raise CompanyError("company.memory_namespace must be a non-empty string")
    if "max_cost_usd" in company and not isinstance(company["max_cost_usd"], (int, float)):
        raise CompanyError("company.max_cost_usd must be a number")
    if not isinstance(nodes, list) or not nodes:
        raise CompanyError("company.toml must contain at least one [[node]]")

    by_id: dict[str, dict] = {}
    for index, node in enumerate(nodes, start=1):
        if not isinstance(node, dict):
            raise CompanyError(f"node {index} must be a table")
        node_id = node.get("id")
        report_to = node.get("report_to")
        if not isinstance(node_id, str) or not node_id:
            raise CompanyError(f"node {index}.id must be a non-empty string")
        if not isinstance(report_to, str):
            raise CompanyError(f"node {node_id!r}.report_to must be a string")
        if node_id in by_id:
            raise CompanyError(f"node {node_id!r} is declared more than once (multiple parents)")
        if "agency" in node and (not isinstance(node["agency"], str) or not node["agency"]):
            raise CompanyError(f"node {node_id!r}.agency must be a non-empty string")
        if "prompt" in node and not isinstance(node["prompt"], str):
            raise CompanyError(f"node {node_id!r}.prompt must be a string")
        if "title" in node and not isinstance(node["title"], str):
            raise CompanyError(f"node {node_id!r}.title must be a string")
        by_id[node_id] = node

    roots = [node for node in nodes if node["report_to"] == ""]
    if len(roots) != 1:
        raise CompanyError(f"company must have exactly one root; found {len(roots)}")
    for node in nodes:
        parent = node["report_to"]
        if parent and parent not in by_id:
            raise CompanyError(
                f"node {node['id']!r} reports to unknown node {parent!r}"
            )

    children: dict[str, list[str]] = {node_id: [] for node_id in by_id}
    for node in nodes:
        if node["report_to"]:
            children[node["report_to"]].append(node["id"])

    for node in nodes:
        if "agency" in node and children[node["id"]]:
            raise CompanyError(f"leaf node {node['id']!r} has reports")

    root_id = roots[0]["id"]
    visited: set[str] = set()
    active: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in active:
            raise CompanyError(f"company report_to graph contains a cycle at {node_id!r}")
        if node_id in visited:
            return
        active.add(node_id)
        for child_id in children[node_id]:
            visit(child_id)
        active.remove(node_id)
        visited.add(node_id)

    visit(root_id)
    # A disconnected cycle cannot be reached from the root, but it is still a
    # cycle and deserves that more useful diagnostic than merely "unreachable".
    for node_id in by_id:
        if node_id not in visited:
            visit(node_id)
    if len(visited) != len(by_id):
        missing = sorted(set(by_id) - visited)
        raise CompanyError(f"company report_to graph is not a tree; unreachable nodes: {', '.join(missing)}")
    return data


def company_org(path: str | Path) -> dict:
    """Return a UI-ready organization chart for a validated company TOML."""
    data = load_company(path)
    company = data["company"]
    nodes: list[dict] = data["node"]
    children: dict[str, list[str]] = {node["id"]: [] for node in nodes}
    for node in nodes:
        if node["report_to"]:
            children[node["report_to"]].append(node["id"])

    return {
        "name": company["name"],
        "title": company.get("title") or company["name"],
        "positioning": company.get("positioning", ""),
        "max_cost_usd": company.get("max_cost_usd"),
        "root_id": next(node["id"] for node in nodes if node["report_to"] == ""),
        "nodes": [
            {
                "id": node["id"],
                "title": node.get("title") or node["id"],
                "kind": "crew" if "agency" in node else "manager",
                "report_to": node["report_to"],
                "agency": Path(node["agency"]).name if "agency" in node else None,
                "children": children[node["id"]],
            }
            for node in nodes
        ],
    }


def _entry_path(agency_dir: Path, entry: str) -> Path:
    relative = Path(entry)
    if relative.is_absolute() or ".." in relative.parts:
        raise CompanyError(f"registry agency has an unsafe entry path: {entry!r}")
    return agency_dir / relative


def compile_company(path: str | Path, dest_dir: str | Path) -> Path:
    """Install leaf agencies and write manager configs; return the root config."""
    company_path = Path(path).resolve()
    data = load_company(company_path)
    company = data["company"]
    nodes: list[dict] = data["node"]
    by_id = {node["id"]: node for node in nodes}
    children: dict[str, list[dict]] = {node_id: [] for node_id in by_id}
    for node in nodes:
        if node["report_to"]:
            children[node["report_to"]].append(node)

    output_dir = (Path(dest_dir).resolve() / company["name"])
    if output_dir.exists():
        raise FileExistsError(f"destination already exists: {output_dir}")
    output_dir.mkdir(parents=True)
    namespace = company["memory_namespace"]

    config_paths: dict[str, Path] = {}
    for node in nodes:
        if "agency" not in node:
            continue
        source = node["agency"]
        if not source.startswith("gh:"):
            source_path = Path(source)
            if not source_path.is_absolute():
                source = str(company_path.parent / source_path)
        files, readme, entry = resolve_source(source)
        agency_dir = output_dir / "agencies" / node["id"]
        write_template(
            agency_dir,
            files,
            readme,
            run_from=str(output_dir),
            slug=f"{namespace}_{node['id']}",
        )
        config_paths[node["id"]] = _entry_path(agency_dir, entry).resolve()

    root_id = next(node["id"] for node in nodes if node["report_to"] == "")

    def write_manager(node_id: str) -> Path:
        node = by_id[node_id]
        child_entries = []
        for child in children[node_id]:
            child_id = child["id"]
            child_config = config_paths.get(child_id) or write_manager(child_id)
            child_entries.append({
                "name": child_id,
                "description": child.get("title", child_id),
                "config": str(child_config),
            })
        agent = {
            "name": node_id,
            "max_steps": 10,
            "system_prompt": node.get(
                "prompt", f"You manage {node.get('title', node_id)}. Delegate to your reports and synthesize their work."
            ),
        }
        if node_id == root_id and "max_cost_usd" in company:
            agent["max_cost_usd"] = company["max_cost_usd"]
        config = {
            "agent": agent,
            "llm": {
                "provider": "openai",
                "model": "gpt-5.6-terra",
                "max_tokens": 1024,
                "api_key_env": "OPENAI_API_KEY",
            },
            "tools": {
                "manifest_dir": ["builtin"],
                "enabled": [child["id"] for child in children[node_id]],
                "result_format": "toon",
                "agents": child_entries,
            },
            "memory": {
                "backend": "sqlite",
                "collection": f"{namespace}_{node_id}",
                "sqlite_path": str(output_dir / ".fabri" / f"{namespace}.db"),
                "top_k": 3,
            },
        }
        config_path = (output_dir / f"{node_id}.yaml").resolve()
        config_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True))
        config_paths[node_id] = config_path
        return config_path

    return write_manager(root_id)


def company_next_steps(root_config: str | Path) -> str:
    """Return the commands for running a compiled company."""
    return f"Now run:\n  fabri serve --config {root_config}\n  fabri studio"
