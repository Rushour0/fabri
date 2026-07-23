from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

from fabri.company import company_org
from fabri.service.http_server import serve_http
from fabri.service.service import FabriService


def test_company_org_and_http_endpoint(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "company_3level" / "company.toml"
    org = company_org(fixture)

    assert org["root_id"] == "ceo"
    nodes = {node["id"]: node for node in org["nodes"]}
    assert nodes["vp_eng"]["children"] == ["bugs", "writer"]
    assert nodes["bugs"]["kind"] == "crew"
    assert nodes["bugs"]["agency"] is not None
    assert nodes["bugs"]["children"] == ["bugs__bug-specialist"]
    assert nodes["bugs__bug-specialist"] == {
        "id": "bugs__bug-specialist",
        "title": "bug-specialist",
        "kind": "role",
        "report_to": "bugs",
        "agency": None,
        "children": [],
    }
    for node in org["nodes"]:
        for child_id in node["children"]:
            assert nodes[child_id]["report_to"] == node["id"]

    service = FabriService(home_root=tmp_path / "runs")
    server = serve_http(service, host="127.0.0.1", port=0, company=org)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        connection = http.client.HTTPConnection(host, port)
        connection.request("GET", "/company")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == {"company": org}
    finally:
        server.shutdown()
        server.server_close()
        service.close()


def test_company_org_keeps_missing_agency_as_flat_crew(tmp_path: Path) -> None:
    company_path = tmp_path / "company.toml"
    company_path.write_text(
        "[company]\n"
        "name = 'missing-agency'\n"
        "memory_namespace = 'missing_agency'\n\n"
        "[[node]]\n"
        "id = 'ceo'\n"
        "report_to = ''\n\n"
        "[[node]]\n"
        "id = 'crew'\n"
        "report_to = 'ceo'\n"
        "agency = 'does-not-exist'\n"
    )

    org = company_org(company_path)

    assert [node["id"] for node in org["nodes"]] == ["ceo", "crew"]
    assert org["nodes"][0]["children"] == ["crew"]
    assert org["nodes"][1]["children"] == []
