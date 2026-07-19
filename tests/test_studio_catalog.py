from __future__ import annotations

import http.client
import json
import shutil
import sys
import threading
from pathlib import Path

import pytest

from fabri.catalog import catalog_listing, load_catalog
from fabri.config import load_config
from fabri.service.http_server import serve_http
from fabri.service.service import FabriService


def _make_catalog(tmp_path: Path) -> Path:
    fixtures = Path(__file__).parent / "fixtures"
    catalog_dir = tmp_path / "rosters"
    shutil.copytree(fixtures / "registry_agency", catalog_dir / "agencies" / "sample-agency")
    shutil.copytree(fixtures / "company_3level", catalog_dir / "companies" / "acme-eng")
    (catalog_dir / "index.json").write_text(json.dumps({
        "agencies": [{
            "name": "sample-agency", "title": "Sample Agency", "tagline": "A tiny fixture.",
            "category": "testing", "deliverable": "fixture", "entry": "agent.openai.yaml",
            "agents": 1, "tools": [], "max_cost_usd": 1, "provider": "openai",
            "self_improving": False, "cogs_reported": True, "path": "agencies/sample-agency",
        }],
        "companies": [{
            "name": "acme-eng", "title": "Acme Engineering",
            "positioning": "A compact engineering company for reliable delivery.",
            "max_cost_usd": 5, "node_count": 4,
            "member_agencies": ["bug-crew", "writer-crew"], "path": "companies/acme-eng",
        }],
        "rosters": [],
    }))
    return catalog_dir


def _fake_builder(_: str, __: Path, ___: str, ____: Path) -> list[str]:
    return [sys.executable, "-c", "print('{}')"]


def test_catalog_preinstalls_lists_and_binds_selected_template(tmp_path: Path) -> None:
    catalog = load_catalog(_make_catalog(tmp_path), tmp_path / "work")

    assert set(catalog) == {"sample-agency", "acme-eng"}
    for item in catalog.values():
        assert Path(item["config"]).is_file()
        assert load_config(str(item["config"]))
    company_config = load_config(str(catalog["acme-eng"]["config"]))
    assert company_config["memory"]["sqlite_path"] == str(
        (Path.cwd() / ".fabri" / "acme_eng.db").resolve()
    )

    listing = catalog_listing(catalog)
    assert listing["agencies"] == [{
        **catalog["sample-agency"]["meta"], "ref": "sample-agency", "kind": "agency"
    }]
    assert listing["companies"] == [{
        **catalog["acme-eng"]["meta"], "ref": "acme-eng", "kind": "company",
        "org": catalog["acme-eng"]["org"],
    }]

    service = FabriService(
        catalog=catalog, home_root=tmp_path / "runs", command_builder=_fake_builder
    )
    try:
        session_id = service.submit("test", catalog_ref="sample-agency")
        run_config = (tmp_path / "runs" / session_id / "run.yaml").read_text()
        assert "name: sample-parent" in run_config
        with pytest.raises(KeyError, match="unknown catalog_ref 'missing'"):
            service.submit("test", catalog_ref="missing")
    finally:
        service.close()


def test_catalog_http_endpoint(tmp_path: Path) -> None:
    catalog = load_catalog(_make_catalog(tmp_path), tmp_path / "work")
    service = FabriService(catalog=catalog, home_root=tmp_path / "runs")
    server = serve_http(service, host="127.0.0.1", port=0, catalog=catalog)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        connection = http.client.HTTPConnection(host, port)
        connection.request("GET", "/catalog")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == {"catalog": catalog_listing(catalog)}
    finally:
        server.shutdown()
        server.server_close()
        service.close()
