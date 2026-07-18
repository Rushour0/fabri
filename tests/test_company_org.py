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
