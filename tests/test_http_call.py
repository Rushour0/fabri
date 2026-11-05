"""The split of http_call.send into prepare, request_hash, check_extra_headers
and send_prepared, which the credential broker uses to hash a request before
approval and send that same request after it."""
from __future__ import annotations

import subprocess
import sys

import httpx
import pytest

from fabri.tools.credential_store import CredentialNotFoundError
from fabri.tools.http_call import (
    HOP_BY_HOP_AND_AUTH,
    MAX_EXTRA_HEADER_BYTES,
    MAX_EXTRA_HEADERS,
    HeaderSpecError,
    check_extra_headers,
    prepare,
    request_hash,
    send,
    send_prepared,
)

BASE = "https://api.cakemail.dev"
# A made up value shaped like a token. Tests never print it: every check
# against it is a plain boolean so a failure shows no value.
FAKE_TOKEN = "ck_pat_" + "q" * 40


class DictStore:
    def __init__(self, values: dict[tuple[str, str], str]) -> None:
        self.values = values
        self.calls = 0

    def get(self, provider: str, handle: str) -> str:
        self.calls += 1
        try:
            return self.values[(provider, handle)]
        except KeyError:
            raise CredentialNotFoundError(f"no credential for {provider}") from None


def _recording_client(calls: list[httpx.Request], status: int = 200, body=None, headers=None) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json=body if body is not None else {"ok": True})

    return httpx.Client(transport=httpx.MockTransport(handle), headers=headers)


def _wire(request: httpx.Request) -> tuple:
    return (request.method, str(request.url), request.content,
            sorted((k.lower(), v) for k, v in request.headers.items()))


@pytest.mark.parametrize("kwargs", [
    {"params": {"b": 1, "a": "x y", "skip": None}, "json_body": {"email": "a@b.c", "n": [1, 2]}},
    {"form_body": {"name": "list one", "tag": "a&b"}},
    {"params": {"page": 2}},
    {},
])
def test_prepare_matches_send_bytes(kwargs):
    spec = {"X-Api-Version": "2", "Accept": "application/json"}
    via_send: list[httpx.Request] = []
    via_split: list[httpx.Request] = []
    via_plain: list[httpx.Request] = []
    method = "POST" if ("json_body" in kwargs or "form_body" in kwargs) else "get"
    url = f"{BASE}/lists/42/contacts"

    out1 = send(_recording_client(via_send), method, url, headers=spec, extra_headers={"X-Dry-Run": "true"},
                timeout_s=7.0, **kwargs)
    client = _recording_client(via_split)
    prepared = prepare(method, url, client=client, **kwargs)
    out2 = send_prepared(client, prepared, headers=spec, extra_headers={"X-Dry-Run": "true"}, timeout_s=7.0)
    # Without a client, as the broker builds it: the client's default headers
    # are filled in at send time, so the wire is the same.
    client3 = _recording_client(via_plain)
    out3 = send_prepared(client3, prepare(method, url, **kwargs), headers=spec,
                         extra_headers={"X-Dry-Run": "true"}, timeout_s=7.0)

    assert out1 == out2 == out3
    assert len(via_send) == len(via_split) == len(via_plain) == 1
    assert _wire(via_send[0]) == _wire(via_split[0]) == _wire(via_plain[0])
    assert via_send[0].extensions["timeout"] == via_split[0].extensions["timeout"]
    assert via_split[0].extensions["timeout"] == {"connect": 7.0, "read": 7.0, "write": 7.0, "pool": 7.0}
    assert via_plain[0].extensions["timeout"] == {"connect": 7.0, "read": 7.0, "write": 7.0, "pool": 7.0}
    assert prepared.method == method.upper()
    if "skip" in kwargs.get("params", {}):
        assert "skip" not in str(prepared.url)


def test_prepare_without_client_has_no_client_defaults():
    request = prepare("post", f"{BASE}/lists", json_body={"a": 1})
    assert request.method == "POST"
    assert "user-agent" not in request.headers
    assert "authorization" not in request.headers
    assert request.headers["content-type"] == "application/json"
    assert request.content == b'{"a":1}'


def test_request_hash_covers_method_url_body_and_extra_headers():
    base = prepare("POST", f"{BASE}/lists/42", params={"a": 1}, json_body={"x": 1})
    extra = {"X-Dry-Run": "true", "X-Trace": "abc"}
    h = request_hash(base, extra)
    assert len(h) == 64 and int(h, 16) >= 0

    # Same inputs, same hash; header name case and mapping order do not matter.
    again = prepare("POST", f"{BASE}/lists/42", params={"a": 1}, json_body={"x": 1})
    assert request_hash(again, {"x-trace": "abc", "X-DRY-RUN": "true"}) == h

    # Each part changes the hash.
    assert request_hash(prepare("PUT", f"{BASE}/lists/42", params={"a": 1}, json_body={"x": 1}), extra) != h
    assert request_hash(prepare("POST", f"{BASE}/lists/43", params={"a": 1}, json_body={"x": 1}), extra) != h
    assert request_hash(prepare("POST", f"{BASE}/lists/42", params={"a": 2}, json_body={"x": 1}), extra) != h
    assert request_hash(prepare("POST", f"{BASE}/lists/42", params={"a": 1}, json_body={"x": 2}), extra) != h
    assert request_hash(base, {"X-Dry-Run": "false", "X-Trace": "abc"}) != h
    assert request_hash(base, {"X-Dry-Run": "true"}) != h
    assert request_hash(base) != h
    assert request_hash(base, None) == request_hash(base, {})

    # The hash binds the URL as sent, so param order is part of it.
    ab = prepare("GET", f"{BASE}/x", params={"a": 2, "b": 1})
    ba = prepare("GET", f"{BASE}/x", params={"b": 1, "a": 2})
    assert str(ab.url) != str(ba.url)
    assert request_hash(ab) != request_hash(ba)

    # Moving bytes between parts cannot produce the same hash.
    split1 = prepare("GET", f"{BASE}/x")
    assert request_hash(split1, {"a": "b:c"}) != request_hash(split1, {"a:b": "c"})


def test_request_hash_is_stable_across_processes():
    code = (
        "from fabri.tools.http_call import prepare, request_hash\n"
        "r = prepare('POST', 'https://api.cakemail.dev/lists/42', params={'a': 1}, json_body={'x': [1, 2]})\n"
        "print(request_hash(r, {'X-Dry-Run': 'true'}))\n"
    )
    other = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                           env={"PYTHONHASHSEED": "12345", "PATH": "/usr/bin:/bin"})
    here = request_hash(prepare("POST", f"{BASE}/lists/42", params={"a": 1}, json_body={"x": [1, 2]}),
                        {"X-Dry-Run": "true"})
    assert other.stdout.strip() == here


def test_check_extra_headers_rules():
    assert check_extra_headers(None) == {}
    assert check_extra_headers({}) == {}
    ok = check_extra_headers({"X-Dry-Run": "true", "Idempotency-Key": "k-1", "X-Tab": "a\tb"})
    # The caller's case is kept.
    assert ok == {"X-Dry-Run": "true", "Idempotency-Key": "k-1", "X-Tab": "a\tb"}

    for name in HOP_BY_HOP_AND_AUTH:
        with pytest.raises(HeaderSpecError):
            check_extra_headers({name.upper(): "v"})
        with pytest.raises(HeaderSpecError):
            check_extra_headers({name.title(): "v"})

    # A header the bound source declares cannot be overridden, in any case.
    with pytest.raises(HeaderSpecError, match="already sets"):
        check_extra_headers({"x-account": "2"}, declared=["X-Account"])
    assert check_extra_headers({"X-Other": "2"}, declared=["X-Account"]) == {"X-Other": "2"}

    for bad_name in ["X Bad", "X:Bad", "", "X-Ä", "X\r\nInjected"]:
        with pytest.raises(HeaderSpecError):
            check_extra_headers({bad_name: "v"})
    with pytest.raises(HeaderSpecError):
        check_extra_headers({7: "v"})

    secret_like = "zz" + "9" * 30
    for bad_value in [f"a\r\nAuthorization: {secret_like}", f"{secret_like}\n", f"{secret_like}\x00", "a\x01b", "a\x7fb"]:
        with pytest.raises(HeaderSpecError) as info:
            check_extra_headers({"X-Note": bad_value})
        shown = str(info.value)
        assert "X-Note" in shown
        leaked = secret_like in shown
        assert not leaked
    with pytest.raises(HeaderSpecError):
        check_extra_headers({"X-Note": 5})

    with pytest.raises(HeaderSpecError, match="more than once"):
        check_extra_headers({"X-Note": "1", "x-note": "2"})

    many = {f"X-H{i}": "v" for i in range(MAX_EXTRA_HEADERS)}
    assert len(check_extra_headers(many)) == MAX_EXTRA_HEADERS
    many["X-One-More"] = "v"
    with pytest.raises(HeaderSpecError, match="too many"):
        check_extra_headers(many)

    big_value = "v" * (MAX_EXTRA_HEADER_BYTES - len("X-Big"))
    assert check_extra_headers({"X-Big": big_value}) == {"X-Big": big_value}
    with pytest.raises(HeaderSpecError, match="larger than"):
        check_extra_headers({"X-Big": big_value + "v"})

    with pytest.raises(HeaderSpecError, match="mapping"):
        check_extra_headers([("X-A", "1")])  # type: ignore[arg-type]


def test_send_prepared_resolves_redacts_and_hints_401():
    store = DictStore({("cakemail", "main"): FAKE_TOKEN})
    spec = {"Authorization": {"secret_ref": "cakemail:main", "prefix": "Bearer "}}
    seen_auth: list[bool] = []

    def echo(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("authorization") == "Bearer " + FAKE_TOKEN)
        return httpx.Response(401, json={"you_sent": request.headers.get("authorization"),
                                         "raw": FAKE_TOKEN})

    client = httpx.Client(transport=httpx.MockTransport(echo))
    prepared = prepare("GET", f"{BASE}/lists/42")
    out = send_prepared(client, prepared, headers=spec, store=store, tool_name="ck_get_list")

    assert seen_auth == [True]
    assert store.calls == 1
    assert out["ok"] is False
    assert out["result"]["code"] == 401
    assert out["error"] == "HTTP 401; the stored cakemail credential was refused, run fabri connect cakemail"
    leaked = FAKE_TOKEN in repr(out)
    assert not leaked
    # The prepared request is not changed: it never holds the credential.
    assert "authorization" not in prepared.headers

    # A 2xx body that echoes the value is redacted too.
    ok_client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"echo": r.headers.get("authorization")})))
    ok = send_prepared(ok_client, prepare("GET", f"{BASE}/lists/42"), headers=spec, store=store)
    assert ok["ok"] is True
    leaked = FAKE_TOKEN in repr(ok)
    assert not leaked

    # A missing credential is an error that names the header only, and nothing is sent.
    calls: list[httpx.Request] = []
    missing = send_prepared(_recording_client(calls), prepare("GET", f"{BASE}/lists/42"),
                            headers={"Authorization": {"secret_ref": "cakemail:other"}}, store=store)
    assert missing["ok"] is False
    assert "Authorization" in missing["error"]
    assert calls == []

    # headers=None sends no credential at all.
    bare_calls: list[httpx.Request] = []
    bare = send_prepared(_recording_client(bare_calls), prepare("GET", f"{BASE}/lists/42"),
                         extra_headers={"X-Dry-Run": "true"}, timeout_s=3)
    assert bare["ok"] is True
    assert "authorization" not in bare_calls[0].headers
    assert bare_calls[0].headers["x-dry-run"] == "true"
    assert bare_calls[0].extensions["timeout"] == {"connect": 3, "read": 3, "write": 3, "pool": 3}

    # A 401 with no secret_ref spec keeps the plain error.
    plain_401 = send_prepared(_recording_client([], status=401), prepare("GET", f"{BASE}/x"))
    assert plain_401["error"] == "HTTP 401"


def test_send_prepared_maps_transport_errors():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    out = send_prepared(httpx.Client(transport=httpx.MockTransport(boom)), prepare("GET", f"{BASE}/x"))
    assert out == {"ok": False, "error": "request failed: ConnectError",
                   "result": {"status": "error", "code": None}}


def test_send_prepared_refuses_dot_segment_and_floor_drift():
    calls: list[httpx.Request] = []
    client = _recording_client(calls)

    # httpx drops literal dot segments while parsing, but keeps encoded ones,
    # which a server may decode. send_prepared refuses those.
    for path in ["/lists/%2e%2e/secret", "/lists/%2E/x", "/lists/.%2e/x", "/lists/%252e%252e/x"]:
        out = send_prepared(client, prepare("GET", BASE + path))
        assert out["ok"] is False, path
        assert "dot segment" in out["error"]
    # send refuses the literal form before httpx can normalize it away.
    out = send(client, "GET", f"{BASE}/lists/../secret")
    assert out["ok"] is False and "dot segment" in out["error"]
    assert calls == []

    # The floor: a curated read template rendered to a path the catalog does
    # not list as a read is refused before any credential is resolved.
    store = DictStore({("cakemail", "main"): FAKE_TOKEN})
    spec = {"Authorization": {"secret_ref": "cakemail:main"}}
    drift = send_prepared(client, prepare("GET", f"{BASE}/lists/1/contacts/2/secret/x"), headers=spec,
                          store=store, floor_url=f"{BASE}/lists/{{list_id}}")
    assert drift["ok"] is False
    assert "outside" in drift["error"]
    assert calls == []
    assert store.calls == 0

    # Inside the floor it is sent.
    inside = send_prepared(client, prepare("GET", f"{BASE}/lists/42"), headers=spec, store=store,
                           floor_url=f"{BASE}/lists/{{list_id}}")
    assert inside["ok"] is True
    assert len(calls) == 1
    assert calls[0].url.raw_path == b"/lists/42"


def test_send_keeps_old_order_when_the_request_cannot_be_built():
    calls: list[httpx.Request] = []
    # A header spec error is still reported ahead of a bad URL.
    out = send(_recording_client(calls), "GET", "https://a.test/\x00",
               headers={"X-Key": {"env": "FABRI_TEST_SURELY_UNSET_ENV"}})
    assert out["ok"] is False
    assert "X-Key" in out["error"]
    # With headers fine, the build error is raised as before the split.
    with pytest.raises(httpx.InvalidURL):
        send(_recording_client(calls), "GET", "https://a.test/\x00")
    assert calls == []
