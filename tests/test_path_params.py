"""Path parameters cannot move a call off the path its approval floor allowed."""
from __future__ import annotations

import httpx
import pytest

from fabri.tools.http_call import PathParamError, render_path_value, send
from fabri.tools.openapi import operation_handler, parse_operations

BASE = "https://api.cakemail.dev"

DOC = {
    "openapi": "3.0.0",
    "paths": {
        "/lists/{list_id}": {
            "get": {
                "operationId": "getList",
                "parameters": [{"name": "list_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {"200": {"description": "ok"}},
            }
        }
    },
}


def _handler(calls):
    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handle))
    (op,) = parse_operations(DOC)
    return operation_handler(op, base_url=BASE, client=client)


@pytest.mark.parametrize("value", ["..", ".", "a/b", "%2e%2e", "%2F", "a%2Fb", "a\\b", "%252e%252e", "%5c"])
def test_unsafe_path_value_refused_and_nothing_sent(value):
    calls: list[httpx.Request] = []
    out = _handler(calls)({"list_id": value})
    assert out["ok"] is False
    assert "list_id" in out["error"]
    assert calls == []


def test_empty_path_value_refused():
    calls: list[httpx.Request] = []
    out = _handler(calls)({"list_id": ""})
    assert out["ok"] is False
    assert calls == []


def test_normal_id_is_sent():
    calls: list[httpx.Request] = []
    out = _handler(calls)({"list_id": "123"})
    assert out["ok"] is True
    assert len(calls) == 1
    assert calls[0].url.raw_path == b"/lists/123"


def test_value_with_spaces_is_encoded():
    calls: list[httpx.Request] = []
    out = _handler(calls)({"list_id": "my list.v2"})
    assert out["ok"] is True
    assert calls[0].url.raw_path == b"/lists/my%20list.v2"


def test_render_path_value_rejects_dot_segments():
    with pytest.raises(PathParamError):
        render_path_value("x", "..")
    assert render_path_value("x", "a b") == "a%20b"


def _client(calls):
    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    return httpx.Client(transport=httpx.MockTransport(handle))


def test_send_refuses_dot_segment_url():
    # Covers callers that render the URL themselves, such as http sources.
    calls: list[httpx.Request] = []
    out = send(_client(calls), "GET", f"{BASE}/lists/..")
    assert out["ok"] is False
    assert calls == []


def test_send_refuses_rendered_path_outside_the_floor():
    calls: list[httpx.Request] = []
    # The template is a curated read, the rendered path is not.
    out = send(_client(calls), "GET", f"{BASE}/lists/1/contacts/2/secret/x",
               floor_url=f"{BASE}/lists/{{list_id}}")
    assert out["ok"] is False
    assert "outside" in out["error"]
    assert calls == []


def test_send_allows_rendered_path_inside_the_floor():
    calls: list[httpx.Request] = []
    out = send(_client(calls), "GET", f"{BASE}/lists/42", floor_url=f"{BASE}/lists/{{list_id}}")
    assert out["ok"] is True
    assert len(calls) == 1


def test_send_skips_floor_check_for_unowned_host():
    calls: list[httpx.Request] = []
    out = send(_client(calls), "GET", "https://example.com/a/b", floor_url="https://example.com/{x}")
    assert out["ok"] is True


def test_http_kind_path_params_are_checked_and_floored(tmp_path):
    import httpx
    from fabri.tools.sources import build_source_tools
    sent = []
    transport = httpx.MockTransport(lambda r: (sent.append(str(r.url)), httpx.Response(200, json={}))[1])
    decl = {"name": "ck_list", "kind": "http", "method": "GET",
            "url": "https://api.cakemail.dev/lists/{list_id}"}
    (tool,) = build_source_tools(decl, transport=transport)
    for bad in ("..", "a/b", "%2e%2e", "%2F"):
        out = tool.handler({"list_id": bad})
        assert out.get("ok") is False, bad
    assert sent == []
    assert tool.handler({"list_id": "123"}).get("ok") is True
    assert sent == ["https://api.cakemail.dev/lists/123"]


@pytest.mark.parametrize(
    "value",
    [
        "a／b",  # full-width slash
        "．．",  # two full-width dots
        "．.",  # mixed full-width and ASCII dot
        "․․",  # one-dot leader pair
        "a％b",  # full-width percent sign
        "a\x00b",  # control character
        "a​b",  # zero-width space (format character)
        "a b",  # line separator
        "a b",  # paragraph separator
    ],
)
def test_render_path_value_rejects_unicode_tricks(value):
    with pytest.raises(PathParamError):
        render_path_value("id", value)


def test_render_path_value_keeps_normal_unicode_names():
    assert render_path_value("id", "Zoë-München_1.0~x") == "Zo%C3%AB-M%C3%BCnchen_1.0~x"
    assert render_path_value("id", "abc-1._~") == "abc-1._~"


def test_division_slash_is_percent_encoded_not_folded():
    # NFKC leaves U+2215 alone, so no proxy folds it to "/". It is a symbol,
    # not a letter or digit, so it is percent-encoded like any other symbol.
    assert render_path_value("id", "a\u2215b") == "a%E2%88%95b"


def test_percent_and_nfkc_in_any_order_cannot_make_a_separator():
    import pytest
    from fabri.tools.http_call import PathParamError, render_path_value
    for bad in ("%zz％2f..％2fsecret", "％2e％2e", "%25／", "a％" + "2Fb"):
        with pytest.raises(PathParamError):
            render_path_value("id", bad)
    assert render_path_value("id", "100%-sure") == "100%25-sure"
