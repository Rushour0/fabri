"""The Gmail connector: the catalog entry, the expansion, the host floor, the
rfc822 send and draft tools on the local path, and send through the
credential broker with one approval card that shows the message as text.
Offline: every HTTP call goes to an httpx.MockTransport."""
from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
from email import policy as email_policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from fabri.service.connection_store import ConnectionStore
from fabri.service.credential_broker import CredentialBroker, RequestNotShowable, describe_request
from fabri.setups import clear_cache
from fabri.setups.lint import TOOL_COUNT_WARN
from fabri.setups.resolver import resolve_setup
from fabri.setups.tool_entries import add_entry, connector_tool_entry
from fabri.setups.toolset import resolve_tools
from fabri.tools.connector_catalog import gmail
from fabri.tools.connectors import CONNECTORS, connector_entry, list_connectors, request_floor
from fabri.tools.credential_broker_client import BrokerClient
from fabri.tools.mail_message import build_message
from fabri.tools.policy import ToolPolicy
from fabri.tools.registry import ToolRegistry
from fabri.tools.sources import build_source_tools, register_tool_sources

TOKEN = "ya29.gmail-test-token-0123456789"
UPLOAD = "https://gmail.googleapis.com/upload/gmail/v1/users/me"
MESSAGE = {"to": "Ann <ann@example.com>, bob@example.org", "cc": "cy@example.net",
           "subject": "Q3 numbers", "body": "Hi both,\nthe numbers are attached.\nRushikesh"}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    clear_cache()

    def getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr("fabri.tools.security.ssrf.socket.getaddrinfo", getaddrinfo)
    monkeypatch.delenv("FABRI_FETCH_ALLOW_PRIVATE", raising=False)
    for name in list(os.environ):
        if name.startswith("FABRI_CRED_"):
            monkeypatch.delenv(name, raising=False)
    yield
    clear_cache()


class Upstream:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"id": "m1", "threadId": "t1", "labelIds": ["SENT"]})


def _parse(content: bytes):
    return BytesParser(policy=email_policy.default).parsebytes(content)


def _layer(tmp_path: Path, entry: dict, name: str = "mail") -> Path:
    layer = tmp_path / name
    (layer / "tools").mkdir(parents=True)
    (layer / "tools" / "tools.yaml").write_text(yaml.safe_dump(entry, sort_keys=False), encoding="utf-8")
    return layer


def _resolve(layer: Path, transport=None):
    return resolve_tools(resolve_setup([layer]), use_cache=False, catalog=False, transport=transport)


def _tool(decls, name, **kwargs):
    for decl in decls:
        for tool in build_source_tools(decl, **kwargs):
            if tool.manifest.name == name:
                return tool
    raise AssertionError(name)


# ---------------------------------------------------------------- entry and expansion


def test_entry_loads_with_oauth_and_user_scope():
    entry = CONNECTORS["gmail"]
    assert entry.auth_kind == "oauth" and entry.scope_default == "user" and entry.kinds == ("http",)
    assert entry.oauth is not None and entry.oauth.pkce and entry.oauth.loopback
    assert entry.oauth.scopes_default == ("openid", "email", "https://www.googleapis.com/auth/gmail.send",
                                          "https://www.googleapis.com/auth/gmail.labels")
    assert entry.oauth.scopes_optional["readonly"] == "https://www.googleapis.com/auth/gmail.readonly"
    assert entry.oauth.extra_authorize == {"access_type": "offline", "prompt": "consent"}
    assert entry.oauth.client_id_env == "GOOGLE_CLIENT_ID"
    assert gmail.HOSTS == ("gmail.googleapis.com",)
    row = next(r for r in list_connectors() if r["name"] == "gmail")
    assert row["oauth"] and row["oauth_loopback"] and row["scope_default"] == "user"


def test_expand_default_and_all_tools():
    default = connector_entry("gmail", workspace="${actor}")
    assert default["allow"] == ["gmail_labels_list", "gmail_messages_send"]
    assert list(CONNECTORS["gmail"].default_allow) == default["allow"]
    send = default["sources"][1]
    assert send["url"] == UPLOAD + "/messages/send" and send["method"] == "POST"
    assert send["query"] == {"uploadType": "media"} and send["body_format"] == "rfc822"
    assert send["headers"] == {"Authorization": {"secret_ref": "gmail:${actor}", "prefix": "Bearer "}}
    assert send["input_schema"]["required"] == ["to", "subject", "body"]
    wide = connector_entry("gmail", all_tools=True)
    assert wide["allow"] == ["gmail_labels_list", "gmail_messages_send", "gmail_drafts_create",
                             "gmail_messages_search", "gmail_message_get"]
    for source in wide["sources"]:
        assert source["approval_mode"] == "tighten" and source["catalog"] == "gmail"
        assert source["headers"]["Authorization"] == {"env": "GMAIL_TOKEN", "prefix": "Bearer "}


def test_tool_count_under_lint_limit():
    assert len(connector_entry("gmail", all_tools=True)["sources"]) <= TOOL_COUNT_WARN


def test_default_policy_accepts_the_entry_at_add_time(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TOKEN", TOKEN)
    layer = tmp_path / "company"
    (layer / "tools").mkdir(parents=True)
    report = add_entry(layer, connector_tool_entry("gmail", all_tools=True), policy=ToolPolicy())
    assert report.written and not report.errors, report.errors
    assert not [f for f in report.lint if f["severity"] == "error"], report.lint
    assert len(report.tools) == 5


# ---------------------------------------------------------------- approval floors


def test_send_and_draft_are_always_approve_and_cannot_be_lowered(tmp_path):
    entry = connector_entry("gmail", all_tools=True)
    for source in entry["sources"]:
        if source["name"].endswith(("_messages_send", "_drafts_create")):
            source["approval"] = "read"
            source["name"] = source["name"].replace("gmail_", "mail_")
            del source["approval_mode"], source["catalog"]
    entry["allow"] = [s["name"] for s in entry["sources"]]
    tools = _resolve(_layer(tmp_path, entry)).tools
    assert tools["mail_messages_send"].approval == "always_approve"
    assert tools["mail_drafts_create"].approval == "always_approve"
    assert tools["gmail_labels_list"].approval == "read"
    assert tools["gmail_message_get"].approval == "read"


def test_read_tools_floor_by_host(tmp_path):
    entry = connector_entry("gmail", all_tools=True)
    entry["sources"].append({"kind": "http", "name": "gmail_peek", "method": "GET", "approval": "read",
                             "url": "https://gmail.googleapis.com/gmail/v1/users/me/messages/x/modify"})
    entry["sources"].append({"kind": "http", "name": "gmail_trash", "method": "POST", "approval": "read",
                             "url": "https://gmail.googleapis.com/gmail/v1/users/me/messages/x/trash"})
    entry["allow"] = [s["name"] for s in entry["sources"]]
    tools = _resolve(_layer(tmp_path, entry)).tools
    assert tools["gmail_peek"].approval == "always_approve"
    assert tools["gmail_trash"].approval == "always_approve"
    assert request_floor(UPLOAD + "/messages/send", "POST") == ("gmail", "always_approve")
    assert request_floor(UPLOAD + "/drafts", "POST") == ("gmail", "always_approve")
    assert request_floor("https://gmail.googleapis.com/gmail/v1/users/me/messages/abc", "GET") == ("gmail", None)
    assert gmail.minimum("x_messages_send", "GET") == "always_approve"
    assert gmail.minimum("gmail_labels_list", "DELETE") == "always_approve"
    assert gmail.minimum("gmail_labels_list", "GET") is None


# ---------------------------------------------------------------- the local path


def test_send_builds_rfc822_body_and_posts_with_resolved_header(monkeypatch):
    monkeypatch.setenv("GMAIL_TOKEN", TOKEN)
    upstream = Upstream()
    tool = _tool(connector_entry("gmail")["sources"], "gmail_messages_send", transport=upstream.transport)
    out = tool.handler(dict(MESSAGE))
    assert out["ok"] is True, out
    (sent,) = upstream.requests
    assert sent.method == "POST" and str(sent.url) == UPLOAD + "/messages/send?uploadType=media"
    assert sent.headers["content-type"] == "message/rfc822"
    assert sent.headers["authorization"] == "Bearer " + TOKEN
    msg = _parse(sent.content)
    assert str(msg["To"]) == "Ann <ann@example.com>, bob@example.org"
    assert str(msg["Cc"]) == "cy@example.net" and str(msg["Subject"]) == "Q3 numbers"
    assert msg.get_content().replace("\r\n", "\n").startswith("Hi both,\nthe numbers are attached.")
    assert TOKEN not in json.dumps(out)


def test_draft_posts_to_the_upload_drafts_endpoint(monkeypatch):
    monkeypatch.setenv("GMAIL_TOKEN", TOKEN)
    upstream = Upstream()
    tool = _tool(connector_entry("gmail", all_tools=True)["sources"], "gmail_drafts_create",
                 transport=upstream.transport)
    assert tool.handler({"to": "ann@example.com", "subject": "Draft", "body": "not yet"})["ok"] is True
    (sent,) = upstream.requests
    assert str(sent.url) == UPLOAD + "/drafts?uploadType=media"
    assert sent.headers["content-type"] == "message/rfc822"
    assert str(_parse(sent.content)["Subject"]) == "Draft"


def test_message_get_renders_the_id_and_stays_a_read(monkeypatch):
    monkeypatch.setenv("GMAIL_TOKEN", TOKEN)
    upstream = Upstream()
    tool = _tool(connector_entry("gmail", all_tools=True)["sources"], "gmail_message_get",
                 transport=upstream.transport)
    assert tool.manifest.approval == "read"
    assert tool.handler({"id": "18c2abc0f", "format": "metadata"})["ok"] is True
    assert str(upstream.requests[0].url) == ("https://gmail.googleapis.com/gmail/v1/users/me/messages/18c2abc0f"
                                             "?format=metadata")
    assert tool.handler({"id": "../labels"})["ok"] is False and len(upstream.requests) == 1


@pytest.mark.parametrize("args, field", [
    ({"to": "ann@example.com\r\nBcc: eve@example.com", "subject": "s", "body": "b"}, "to"),
    ({"to": "ann@example.com", "cc": "x@example.com\nBcc: eve@example.com", "subject": "s", "body": "b"}, "cc"),
    ({"to": "ann@example.com", "subject": "hi\r\nBcc: eve@example.com", "body": "b"}, "subject"),
    ({"to": "ann@example.com", "subject": "hi Bcc: eve@example.com", "body": "b"}, "subject"),
    ({"to": "", "subject": "s", "body": "b"}, "to"),
    ({"to": "not an address", "subject": "s", "body": "b"}, "to"),
    ({"to": "ann@example.com", "subject": "s"}, "body"),
    ({"to": "ann@example.com", "subject": "s", "body": "b", "from": "boss@example.com"}, "unknown"),
    # RFC 2047 encoded words are refused before decoding could hide a line break.
    ({"to": "ann@example.com", "subject": "=?utf-8?q?ok=0D=0ABcc=3A_eve=40example.com?=", "body": "b"},
     "subject"),
    ({"to": "=?utf-8?q?Ann=0D=0A?= <ann@example.com>", "subject": "s", "body": "b"}, "to"),
    ({"to": "ann@example.com", "cc": "=?utf-8?b?ZXZl?= <cy@example.net>", "subject": "s", "body": "b"}, "cc"),
    ({"to": "ann@example.com", "bcc": ["=?iso-8859-1?q?x?= <cy@example.net>"], "subject": "s", "body": "b"},
     "bcc"),
    ({"to": "ann@example.com", "subject": "plain =?utf-8?b?QmNj?= tail", "body": "b"}, "subject"),
])
def test_send_refuses_bad_messages_before_any_request(monkeypatch, args, field):
    monkeypatch.setenv("GMAIL_TOKEN", TOKEN)
    upstream = Upstream()
    tool = _tool(connector_entry("gmail")["sources"], "gmail_messages_send", transport=upstream.transport)
    out = tool.handler(args)
    assert out["ok"] is False and field in out["error"]
    assert "eve@example.com" not in out["error"]
    assert upstream.requests == []


# ---------------------------------------------------------------- through the broker


class FakeListener:
    def __init__(self, answers=()):
        self.answers = list(answers)
        self.asked: list[dict] = []

    def ask_local(self, *, question_id, question, options, default, kind="ask_user"):
        self.asked.append({"question_id": question_id, "question": question, "kind": kind})
        pending = SimpleNamespace(event=threading.Event(), reply=None)
        answer = self.answers.pop(0) if self.answers else None
        if answer is not None:
            pending.reply = {"question_id": question_id, "answer": answer, "selected_option": answer}
            pending.event.set()
        return pending

    def forget(self, question_id):
        pass


@pytest.fixture
def home():
    path = Path(tempfile.mkdtemp(prefix="fabri-gm-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _serve(home: Path, listener, *, all_tools=False):
    """A broker bound to a run whose config holds the Gmail connector for
    the user ``u1``, plus a registry built the way a run under fabri serve
    builds it (broker client, no stored token in this process)."""
    store = ConnectionStore(home / "installs.db")
    store.upsert(provider="gmail", handle="u1", access_token=TOKEN, scope_kind="user")
    sources = connector_entry("gmail", workspace="${actor}", all_tools=all_tools)["sources"]
    config = home / "run.yaml"
    config.write_text(yaml.safe_dump({"tools": {"policy": {"allowed_kinds": ["http"]}, "sources": sources}}))
    upstream = Upstream()
    broker = CredentialBroker(install_db=home / "installs.db", transport=upstream.transport)
    binding = broker.bind(session_id="sess-gmail1", config_path=config, run_home=home / "runs" / "sessgmai",
                          actor="u1", listener=listener, run_pid=os.getpid())
    asked_here: list[str] = []
    registry = ToolRegistry([], approver=lambda name, args, manifest: asked_here.append(name) or True)
    client = BrokerClient(binding.socket_path, binding.key, approval_timeout_s=5.0)
    errors = register_tool_sources(registry, sources, broker=client,
                                   policy=ToolPolicy.from_data({"allowed_kinds": ["http"]}))
    assert errors == []
    return broker, binding, upstream, registry, asked_here


def test_send_under_the_broker_asks_once_and_the_card_shows_the_message(home):
    listener = FakeListener(["approve"])
    broker, _binding, upstream, registry, asked_here = _serve(home, listener)
    try:
        assert registry.tools["gmail_messages_send"].approval == "always_approve"
        out = registry.invoke("gmail_messages_send", dict(MESSAGE))
        assert out["ok"] is True, out
        assert asked_here == []           # the registry did not ask as well
        assert len(listener.asked) == 1   # exactly one approval prompt
        card = listener.asked[0]["question"]
        assert "gmail_messages_send needs your approval (class always_approve)" in card
        assert "POST https://gmail.googleapis.com/upload/gmail/v1/users/me/messages/send" in card
        assert '[["uploadType", "media"]]' in card
        assert "To: Ann <ann@example.com>, bob@example.org" in card
        assert "Cc: cy@example.net" in card
        assert "Subject: Q3 numbers" in card
        assert "Hi both,\nthe numbers are attached.\nRushikesh" in card
        assert TOKEN not in card and "Authorization" not in card
        (sent,) = upstream.requests
        assert sent.headers["authorization"] == "Bearer " + TOKEN
        assert sent.headers["content-type"] == "message/rfc822"
        assert sent.url.params["uploadType"] == "media"
        assert str(_parse(sent.content)["Subject"]) == "Q3 numbers"
        assert TOKEN not in json.dumps(out)
    finally:
        broker.close()


def test_draft_under_the_broker_asks_once_and_a_decline_sends_nothing(home):
    listener = FakeListener(["deny", "approve"])
    broker, _binding, upstream, registry, asked_here = _serve(home, listener, all_tools=True)
    try:
        args = {"to": "ann@example.com", "subject": "Café plan", "body": "Draft only, déjà vu."}
        declined = registry.invoke("gmail_drafts_create", args)
        assert declined["ok"] is False and upstream.requests == []
        assert registry.invoke("gmail_drafts_create", args)["ok"] is True
        assert asked_here == [] and len(listener.asked) == 2
        card = listener.asked[1]["question"]
        assert "/upload/gmail/v1/users/me/drafts" in card
        assert "Subject: Café plan" in card and "Draft only, déjà vu." in card
        (sent,) = upstream.requests
        assert str(sent.url) == UPLOAD + "/drafts?uploadType=media"
        # A read needs no approval and asks nothing.
        assert registry.invoke("gmail_labels_list", {})["ok"] is True
        assert len(listener.asked) == 2
    finally:
        broker.close()


def test_broker_refuses_a_mail_request_it_did_not_build(home):
    listener = FakeListener(["approve"] * 4)
    broker, binding, upstream, _registry, _asked = _serve(home, listener)

    def line(**fields):
        payload = {"v": 1, "key": binding.key, "op": "http", "source": "gmail_messages_send",
                   "tool": "gmail_messages_send", "method": "POST", **fields}
        return broker._handle_line(binding, json.dumps(payload).encode())

    try:
        injected = line(json={"to": "ann@example.com", "subject": "x\r\nBcc: eve@example.com", "body": "b"})
        assert injected["ok"] is False and "subject" in injected["error"]
        extra_query = line(params={"uploadType": "media", "fields": "raw"}, json=MESSAGE)
        assert extra_query["ok"] is False and "query" in extra_query["error"]
        ctype = line(json=MESSAGE, extra_headers={"Content-Type": "application/json"})
        assert ctype["ok"] is False and "Content-Type" in ctype["error"]
        form = line(form={"to": "ann@example.com"})
        assert form["ok"] is False
        assert upstream.requests == [] and listener.asked == []
    finally:
        broker.close()


def test_card_refuses_a_mail_body_it_cannot_show_exactly():
    multipart = (b"To: ann@example.com\r\nSubject: s\r\nMIME-Version: 1.0\r\n"
                 b"Content-Type: multipart/alternative; boundary=b\r\n\r\n--b\r\n"
                 b"Content-Type: text/plain\r\n\r\nshown\r\n--b\r\nContent-Type: text/html\r\n\r\n"
                 b"<p>hidden</p>\r\n--b--\r\n")
    extra_header = b"To: ann@example.com\r\nReply-To: eve@example.com\r\nSubject: s\r\n\r\nbody\r\n"
    for content in (multipart, extra_header):
        request = httpx.Request("POST", UPLOAD + "/messages/send", content=content,
                                headers={"Content-Type": "message/rfc822"})
        with pytest.raises(RequestNotShowable):
            describe_request("t", "s", "always_approve", "gmail", request, {}, exact=True)
        assert "cannot show" in describe_request("t", "s", "always_approve", "gmail", request, {})
    good = httpx.Request("POST", UPLOAD + "/messages/send", content=build_message(MESSAGE),
                         headers={"Content-Type": "message/rfc822"})
    text = describe_request("t", "s", "always_approve", "gmail", good, {}, exact=True)
    assert "mail message (message/rfc822):" in text and "To: Ann <ann@example.com>, bob@example.org" in text


def test_build_turns_email_package_errors_into_message_errors():
    from fabri.tools.mail_message import MessageError

    for args in ({"to": '"Ann\u202e" <ann@example.com>', "subject": "s", "body": "b"},
                 {"to": "ann@example.com", "subject": "s", "body": "b\ud800"}):
        with pytest.raises(MessageError):
            build_message(args)


def test_card_revalidates_decoded_headers():
    from fabri.tools.mail_message import MessageNotShowable, describe_message

    for subject in (b"=?utf-8?q?ok=0D=0ABcc=3A_eve=40example.com?=", b"=?utf-8?q?a=E2=80=AEb?=",
                    b"=?utf-8?q?=3D=3Futf-8=3Fq=3Fx=3F=3D?="):
        content = (b"To: ann@example.com\r\nSubject: " + subject + b"\r\nMIME-Version: 1.0\r\n"
                   b"Content-Type: text/plain; charset=utf-8\r\n\r\nbody\r\n")
        with pytest.raises(MessageNotShowable):
            describe_message(content)
        request = httpx.Request("POST", UPLOAD + "/messages/send", content=content,
                                headers={"Content-Type": "message/rfc822"})
        with pytest.raises(RequestNotShowable):
            describe_request("t", "s", "always_approve", "gmail", request, {}, exact=True)
    # A long non-ASCII subject is encoded by build_message and decodes back unchanged.
    subject = "Résumé update " * 30
    text = describe_message(build_message({"to": "ann@example.com", "subject": subject, "body": "b"}))
    assert "Subject: " + subject.strip() in text or "Subject: " + subject in text


def test_card_refuses_a_mail_message_that_redaction_would_change(home):
    listener = FakeListener(["approve"])
    broker, _binding, upstream, registry, _asked = _serve(home, listener)
    try:
        for args in ({"to": "ann@example.com", "subject": "key ghp_abcdefghijklmnopqrstuvwxyz0123456789",
                      "body": "b"},
                     {"to": "ann@example.com", "subject": "s",
                      "body": "hello\nAuthorization: Bearer abcdefghijklmnop0123456789\nbye"}):
            out = registry.invoke("gmail_messages_send", args)
            assert out["ok"] is False
            assert "redact" in out["error"] and "exactly what Gmail receives" in out["error"]
            assert "ghp_" not in out["error"] and "abcdefghijklmnop" not in out["error"]
        assert upstream.requests == [] and listener.asked == []
    finally:
        broker.close()
    # Outside an approval card the message is still shown, redacted.
    content = build_message({"to": "ann@example.com", "subject": "s",
                             "body": "key ghp_abcdefghijklmnopqrstuvwxyz0123456789"})
    request = httpx.Request("POST", UPLOAD + "/messages/send", content=content,
                            headers={"Content-Type": "message/rfc822"})
    with pytest.raises(RequestNotShowable):
        describe_request("t", "s", "always_approve", "gmail", request, {}, exact=True)
    shown = describe_request("t", "s", "always_approve", "gmail", request, {})
    assert "ghp_abcdefghijklmnop" not in shown and "[redacted]" in shown


def test_compose_is_classed_restricted_and_the_default_set_has_no_restricted_scope():
    restricted = {gmail._SCOPE + name for name in ("gmail.readonly", "gmail.compose", "gmail.modify")}
    assert set(gmail.SCOPES_OPTIONAL.values()) == restricted
    assert not restricted & set(gmail.SCOPES_DEFAULT)
    doc = (Path(__file__).resolve().parents[1] / "docs" / "connectors" / "gmail.md").read_text(encoding="utf-8")
    assert "`compose` (`gmail.compose`): drafts. A restricted scope." in doc
    assert "sensitive scope" not in doc.split("## Scopes", 1)[1].split("##", 1)[0].lower().replace(
        "sensitive scopes such as `gmail.send`", "")


# ---------------------------------------------------------------- identity


def test_identity_reads_the_email_and_never_echoes_the_token():
    seen = {}

    def get_json(url, headers):
        seen.update(url=url, headers=dict(headers))
        return {"email": "Ann.Lee+work@example.com", "sub": "123"}

    handle, display = gmail.identity({"access_token": TOKEN}, get_json)
    assert seen["url"] == "https://openidconnect.googleapis.com/v1/userinfo"
    assert seen["headers"] == {"Authorization": "Bearer " + TOKEN}
    assert display == "Ann.Lee+work@example.com" and handle == "ann.leework"
    with pytest.raises(ValueError) as exc:
        gmail.identity({"access_token": TOKEN}, lambda url, headers: {"sub": "123"})
    assert TOKEN not in str(exc.value)
