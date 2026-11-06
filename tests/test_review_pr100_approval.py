"""PR #100 review fixes: outbound posts (comments, reviews, shares) need a
human on every call, and an approval card that cannot show the arguments in
full denies the call instead of showing a cut card."""
from __future__ import annotations

import io
import json
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

from fabri.tools.approval import mcp_approval, safe_approval
from fabri.tools.approver import (
    CARD_CAP,
    REDACTED,
    ArgsTooLong,
    SocketApprover,
    TtyApprover,
    describe_call,
    fits_card,
)
from fabri.tools.manifest_schema import ToolManifest
from fabri.tools.registry import ToolRegistry
from fabri.tools.result import tool_ok


# --- Finding 1: outbound posts are always_approve -------------------------

@pytest.mark.parametrize("name", [
    "create_comment",               # Linear
    "add_issue_comment",            # GitHub / Linear
    "create_pull_request_review",   # GitHub
    "create_review_comment",        # GitHub
    "submit_pending_pull_request_review",
    "post",                         # Slack-style
    "share",
    "forward",
    "reply",
    "reply_to_thread",
    "createComment",
    "share_file",
])
def test_mcp_outbound_posts_are_always_approve(name):
    assert mcp_approval(name) == "always_approve"


@pytest.mark.parametrize("name", ["search_issues", "get_issue", "list_issues"])
def test_mcp_plain_reads_stay_write(name):
    # MCP tools are never read by default.
    assert mcp_approval(name) == "write"


@pytest.mark.parametrize("name", [
    "list_comments", "get_comment", "get_pull_request_reviews", "fetch_review",
    "get_and_post_comment", "list_then_share",
])
def test_mcp_read_verb_does_not_lift_outbound_gate(name):
    # The name is a claim; "get_and_post_comment" posts. No read-verb
    # exception, and the catalog cannot lower a class, so reads of comments
    # and reviews also ask every call.
    assert mcp_approval(name) == "always_approve"


@pytest.mark.parametrize("name", ["create_issue", "update_issue", "add_label"])
def test_mcp_plain_writes_unchanged(name):
    assert mcp_approval(name) == "write"


def test_mcp_read_verb_does_not_hide_old_sensitive_words():
    assert mcp_approval("list_and_send_messages") == "always_approve"
    assert mcp_approval("get_then_delete_comment") == "always_approve"


@pytest.mark.parametrize("method,names", [
    ("POST", ("createComment", "/repos/{o}/{r}/issues/{n}/comments")),
    ("POST", ("create_review", "/repos/{o}/{r}/pulls/{n}/reviews")),
    ("POST", ("chat_share", "/files/share")),
    ("PUT", ("update_post", "/posts/{id}")),
    ("PATCH", ("edit", "/comments/{id}")),
])
def test_http_outbound_posts_are_always_approve(method, names):
    assert safe_approval(method, *names) == "always_approve"


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "get"])
def test_http_reads_stay_read(method):
    assert safe_approval(method, "listComments", "/issues/{n}/comments", "List comments") == "read"
    assert safe_approval(method, "get_review", "/reviews/{id}") == "read"


@pytest.mark.parametrize("method,names", [
    ("POST", ("get_and_post_comment",)),
    ("POST", ("list_comments", "/issues/{n}/comments")),
    ("PUT", ("get_review", "/reviews/{id}")),
    ("POST", ("create_post",)),
    ("POST", ("create_post", "/blog")),
    ("POST", ("submit_post", "/submit")),
    ("PUT", ("create_key_keys_post", "/keys")),  # suffix is not this call's method
    ("POST", ("create_key_keys_post",)),         # no path to match the id against
])
def test_http_non_read_methods_gate_outbound_words(method, names):
    assert safe_approval(method, *names) == "always_approve"


def test_http_method_suffix_in_operation_id_gates():
    # No name-shape guessing: a FastAPI-style id ending in "_post" gates like
    # a name that means a post ("create_blog_post"). The cost is a few
    # generated Cakemail ids (create dkim key, deactivate dkim key, create
    # PAT) that now ask every call.
    assert safe_approval("POST", "create_blog_post", "/blog") == "always_approve"
    assert safe_approval("POST", "create_dkim_key_brands_default_dkim_post", "/brands/default/dkim") == "always_approve"
    assert safe_approval("POST", "deactivate_dkim_key_brands_default_dkim__id__deactivate_post",
                         "/brands/default/dkim/{id}/deactivate") == "always_approve"
    assert safe_approval("POST", "create_pat_endpoint_users_self_pats_post", "/users/self/pats") == "always_approve"
    assert safe_approval("POST", "create_post_posts_post", "/posts") == "always_approve"
    assert safe_approval("POST", "create_key_keys_post", "https://api.example.com/keys") == "always_approve"
    # Without a gated word the method alone gives write.
    assert safe_approval("POST", "create_key", "/keys") == "write"


# --- Finding 3: no cut approval cards -------------------------------------

def _manifest() -> ToolManifest:
    return ToolManifest(
        name="send_email", description="send", command=[],
        input_schema={"type": "object", "properties": {}}, output_schema={},
        approval="always_approve", title="Send an email",
    )


class FakeTty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _long_args() -> dict:
    # A benign prefix with the real ask past the cap.
    return {"to": "a@example.com", "body": "hello " * 4000 + "ALSO BCC everyone@corp.example"}


def test_cap_fits_a_real_email():
    args = {"to": "a@example.com", "subject": "Weekly update", "body": "word " * 3000}
    assert CARD_CAP >= 20000
    assert fits_card(args)
    text = describe_call("send_email", args, _manifest())
    assert text.endswith(json.dumps(args, indent=2, sort_keys=True))
    assert "more chars" not in text


def test_describe_call_raises_over_cap():
    args = _long_args()
    assert not fits_card(args)
    with pytest.raises(ArgsTooLong) as exc:
        describe_call("send_email", args, _manifest())
    assert "arguments too long to show in full" in str(exc.value)
    assert "the call was not made" in str(exc.value)


def test_cap_counts_redacted_text():
    # A long secret is redacted before the length check, so it does not
    # push a short call over the cap.
    args = {"api_key": "k" * (CARD_CAP * 2), "to": "a@example.com"}
    assert fits_card(args)
    assert REDACTED in describe_call("send_email", args, _manifest())


def test_tty_approver_denies_without_prompting():
    stdin, stderr = FakeTty("y\n"), FakeTty()
    approver = TtyApprover(stdin=stdin, stderr=stderr)
    with pytest.raises(ArgsTooLong):
        approver("send_email", _long_args(), _manifest())
    assert stderr.getvalue() == ""
    assert stdin.read() == "y\n"  # the "y" was never consumed


def test_socket_approver_denies_without_connecting():
    d = tempfile.mkdtemp(prefix="fabri-pr100-", dir="/tmp")
    try:
        path = str(Path(d) / "ask.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        srv.setblocking(False)
        try:
            with pytest.raises(ArgsTooLong):
                SocketApprover(path, timeout_s=2)("send_email", _long_args(), _manifest())
            with pytest.raises(BlockingIOError):
                srv.accept()  # nobody connected
        finally:
            srv.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.mark.parametrize("make", ["tty", "socket"])
def test_registry_refuses_long_call_with_clear_error(tmp_path, make):
    calls: list[dict] = []
    if make == "tty":
        approver = TtyApprover(stdin=FakeTty("y\n"), stderr=FakeTty())
    else:
        approver = SocketApprover(str(tmp_path / "missing.sock"), timeout_s=1)
    reg = ToolRegistry(tmp_path, approver=approver)
    reg.register_callable(_manifest(), lambda a: calls.append(a) or tool_ok({}))
    result = reg.invoke("send_email", _long_args())
    assert result["ok"] is False
    assert "arguments too long to show in full" in result["error"]
    assert "the call was not made" in result["error"]
    assert calls == []
    assert reg.usage["send_email"]["refused"] == 1


def test_registry_short_call_still_asks(tmp_path):
    calls: list[dict] = []
    reg = ToolRegistry(tmp_path, approver=TtyApprover(stdin=FakeTty("y\n"), stderr=FakeTty()))
    reg.register_callable(_manifest(), lambda a: calls.append(a) or tool_ok({}))
    assert reg.invoke("send_email", {"to": "a@example.com"})["ok"] is True
    assert calls == [{"to": "a@example.com"}]


def test_registry_refuses_long_call_before_custom_approver(tmp_path):
    # A custom approver that never builds a card still cannot approve a call
    # whose arguments no card could show in full.
    asked: list[str] = []
    calls: list[dict] = []

    def yes(name, args, manifest):
        asked.append(name)
        return True

    reg = ToolRegistry(tmp_path, approver=yes)
    reg.register_callable(_manifest(), lambda a: calls.append(a) or tool_ok({}))
    args = {"to": "a@example.com", "body": "x" * 25000}
    assert not fits_card(args)
    result = reg.invoke("send_email", args)
    assert result["ok"] is False
    assert "arguments too long to show in full" in result["error"]
    assert "the call was not made" in result["error"]
    assert asked == []
    assert calls == []
    # A short call goes through the same approver.
    assert reg.invoke("send_email", {"to": "a@example.com"})["ok"] is True
    assert asked == ["send_email"]


# --- PR #100 third review: an always_approve batch asks first -------------

def _batch_registry(tmp_path: Path, approver) -> tuple[ToolRegistry, list[dict]]:
    examples = Path(__file__).resolve().parent.parent / "src" / "fabri" / "tools" / "examples"
    data = json.loads((examples / "batch.json").read_text())
    data["approval"] = "always_approve"
    (tmp_path / "batch.json").write_text(json.dumps(data))
    reg = ToolRegistry(tmp_path, approver=approver)
    ran: list[dict] = []
    ping = ToolManifest(name="ping", description="ping", command=[],
                        input_schema={"type": "object", "properties": {}}, output_schema={},
                        approval="write")
    reg.register_callable(ping, lambda args: (ran.append(args), tool_ok({"pong": True}))[1])
    assert reg.tools["batch"].approval == "always_approve"
    return reg, ran


def test_always_approve_batch_does_not_run_when_denied(tmp_path):
    asked: list[str] = []
    reg, ran = _batch_registry(tmp_path, lambda name, args, manifest: asked.append(name) or False)
    out = reg.invoke("batch", {"calls": [{"name": "ping", "args": {}}]})
    assert out["ok"] is False
    assert "declined" in out["error"]
    assert asked == ["batch"]
    assert ran == []
    assert reg.usage["batch"]["refused"] == 1
    assert "ping" not in reg.usage


def test_always_approve_batch_without_approver_does_not_run(tmp_path):
    reg, ran = _batch_registry(tmp_path, None)
    out = reg.invoke("batch", {"calls": [{"name": "ping", "args": {}}]})
    assert out["ok"] is False
    assert ran == []


def test_always_approve_batch_runs_when_approved(tmp_path):
    asked: list[str] = []
    reg, ran = _batch_registry(tmp_path, lambda name, args, manifest: asked.append(name) or True)
    out = reg.invoke("batch", {"calls": [{"name": "ping", "args": {"n": 1}}]})
    assert out["ok"] is True
    assert out["result"]["results"][0]["ok"] is True
    assert ran == [{"n": 1}]
    # Only the batch asked: the inner call is write, checked on its own.
    assert asked == ["batch"]
    assert reg.usage["batch"]["calls"] == 1 and reg.usage["ping"]["calls"] == 1


def test_inner_always_approve_call_still_asks_inside_batch(tmp_path):
    asked: list[str] = []
    reg, ran = _batch_registry(tmp_path, lambda name, args, manifest: asked.append(name) or name == "batch")
    reg.tools["ping"].approval = "always_approve"
    out = reg.invoke("batch", {"calls": [{"name": "ping", "args": {}}]})
    assert out["ok"] is True
    assert out["result"]["results"][0]["ok"] is False
    assert asked == ["batch", "ping"]
    assert ran == []
