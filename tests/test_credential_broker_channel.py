"""Credential broker: process mode fails closed without peer credentials, and
the channel transport answers container runs with no host socket."""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

import pytest

from fabri.service import credential_broker as cb
from fabri.service.connection_store import ConnectionStore
from fabri.service.credential_broker import BindError, CredentialBroker

from tests.test_credential_broker import (  # noqa: F401 -- fake_dns is an autouse fixture
    TOKEN,
    FakeListener,
    Upstream,
    _broker,
    _call_socket,
    _line,
    _store,
    _write_config,
    fake_dns,
)


@pytest.fixture
def home():
    path = Path(tempfile.mkdtemp(prefix="fabri-brkc-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _channel(broker: CredentialBroker, home: Path, sid: str, listener=None, name: str = "run.yaml"):
    config = _write_config(home, name=name)
    run_home = home / "runs" / sid[:8]
    binding = broker.bind(session_id=sid, config_path=config, run_home=run_home, actor=None,
                          listener=listener, transport="channel")
    return binding, run_home


def _get_line(binding, key=None) -> bytes:
    return json.dumps({"v": 1, "key": key or binding.key, "op": "http", "source": "ck_get", "tool": "ck_get",
                       "path_params": {"list_id": "1"}}).encode()


# ---------------------------------------------------------------- fail closed


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("missing", ["uid", "pid", "both"])
def test_missing_peer_creds_refused(home, monkeypatch, platform, missing):
    _store(home)
    broker, binding, upstream = _broker(home)
    try:
        monkeypatch.setattr(cb.sys, "platform", platform)
        if missing in ("uid", "both"):
            monkeypatch.setattr(cb, "peer_uid", lambda conn: None)
        else:
            monkeypatch.setattr(cb, "peer_uid", lambda conn: os.getuid())
        if missing in ("pid", "both"):
            monkeypatch.setattr(cb, "peer_pid", lambda conn: None)
        else:
            monkeypatch.setattr(cb, "peer_pid", lambda conn: os.getpid())
        payload = _get_line(binding)
        assert _call_socket(binding.socket_path, payload + b"\n") == b""
        assert upstream.requests == []
    finally:
        broker.close()


def test_unknown_platform_refuses(home, monkeypatch):
    _store(home)
    broker, binding, upstream = _broker(home)
    try:
        monkeypatch.setattr(cb.sys, "platform", "sunos5")
        assert cb.peer_uid(socket.socket(socket.AF_UNIX)) is None
        assert _call_socket(binding.socket_path, _get_line(binding) + b"\n") == b""
        assert upstream.requests == []
    finally:
        broker.close()


def test_real_peer_creds_still_admit_the_run(home):
    if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
        pytest.skip("peer credentials need linux or darwin")
    _store(home)
    broker, binding, upstream = _broker(home)
    try:
        reply = json.loads(_call_socket(binding.socket_path, _get_line(binding) + b"\n"))
        assert reply["ok"] is True
        assert len(upstream.requests) == 1
    finally:
        broker.close()


# ---------------------------------------------------------------- channel transport


def test_channel_bind_creates_no_socket(home):
    _store(home)
    broker = CredentialBroker(install_db=home / "installs.db", transport=Upstream().transport)
    try:
        before = {p for p in Path(tempfile.gettempdir()).glob(f"fabri-brk-{os.getuid()}/*")}
        binding, run_home = _channel(broker, home, "sess-chan-0001")
        assert binding is not None
        assert binding.transport == "channel"
        assert binding.socket_path == cb.CHANNEL_SOCKET_PATH
        assert binding.env() == {cb.SOCKET_ENV: "/run/fabri/brk.sock", cb.KEY_ENV: binding.key}
        assert not run_home.exists() or not any(p.is_socket() for p in run_home.rglob("*"))
        after = {p for p in Path(tempfile.gettempdir()).glob(f"fabri-brk-{os.getuid()}/*")}
        assert after == before
        assert broker._runs["sess-chan-0001"].server is None
        assert broker._runs["sess-chan-0001"].thread is None
    finally:
        broker.close()


def test_channel_correct_request_resolves_token_on_that_call(home, monkeypatch):
    _store(home)
    calls: list[tuple[str, str]] = []
    real = ConnectionStore.get_token

    def counting(self, provider, handle, *args, **kwargs):
        calls.append((provider, handle))
        return real(self, provider, handle, *args, **kwargs)

    monkeypatch.setattr(ConnectionStore, "get_token", counting)
    upstream = Upstream()
    broker = CredentialBroker(install_db=home / "installs.db", transport=upstream.transport)
    try:
        binding, _ = _channel(broker, home, "sess-chan-0001")
        assert calls == []
        reply = broker.handle_channel_request("sess-chan-0001", _get_line(binding))
        assert reply["ok"] is True, reply
        assert calls == [("ck", "acme")]
        assert upstream.requests[0].headers["authorization"] == f"Bearer {TOKEN}"
        # A rotated token is picked up by the next call: nothing is cached.
        _store(home, token="tok-ck-NEXT-1111111111")
        broker.handle_channel_request("sess-chan-0001", _get_line(binding))
        assert calls == [("ck", "acme"), ("ck", "acme")]
        assert upstream.requests[1].headers["authorization"] == "Bearer tok-ck-NEXT-1111111111"
    finally:
        broker.close()


def test_channel_request_for_a_with_b_key_is_refused(home):
    _store(home)
    upstream = Upstream()
    broker = CredentialBroker(install_db=home / "installs.db", transport=upstream.transport)
    try:
        binding_a, _ = _channel(broker, home, "sess-chan-aaaa", name="a.yaml")
        binding_b, _ = _channel(broker, home, "sess-chan-bbbb", name="b.yaml")
        assert binding_a.key != binding_b.key
        reply = broker.handle_channel_request("sess-chan-aaaa", _get_line(binding_a, key=binding_b.key))
        assert reply["ok"] is False
        assert reply["result"]["status"] == "refused"
        assert upstream.requests == []
    finally:
        broker.close()


def test_channel_request_for_unknown_or_socket_session_is_refused(home):
    _store(home)
    broker, socket_binding, upstream = _broker(home)
    try:
        assert broker.handle_channel_request("never-bound", _get_line(socket_binding))["result"]["status"] \
            == "refused"
        # A socket-bound run cannot be driven through the channel entry.
        reply = broker.handle_channel_request(socket_binding.session_id, _get_line(socket_binding))
        assert reply["result"]["status"] == "refused"
        assert upstream.requests == []
    finally:
        broker.close()


def test_channel_client_gone_drops_the_grant(home):
    _store(home)
    gone = {"v": False}

    def hang_up_then_approve(question):
        gone["v"] = True
        return "approve"

    upstream = Upstream()
    broker = CredentialBroker(install_db=home / "installs.db", transport=upstream.transport)
    try:
        binding, _ = _channel(broker, home, "sess-chan-0001", listener=FakeListener([hang_up_then_approve]))
        line = _line(binding, op="http", source="ck_send", tool="ck_send", path_params={"campaign_id": "c"},
                     json={})
        reply = broker.handle_channel_request("sess-chan-0001", line, client_gone=lambda: gone["v"])
        assert reply["result"]["status"] == "refused"
        assert "went away" in reply["error"]
        assert upstream.requests == []
        assert len(broker._runs["sess-chan-0001"].grants) == 0
    finally:
        broker.close()


def test_channel_release_touches_no_host_path(home, monkeypatch):
    _store(home)
    broker = CredentialBroker(install_db=home / "installs.db", transport=Upstream().transport)
    unlinked: list[str] = []
    real_unlink = Path.unlink

    def spy(self, *args, **kwargs):
        unlinked.append(str(self))
        return real_unlink(self, *args, **kwargs)

    binding, _ = _channel(broker, home, "sess-chan-0001")
    monkeypatch.setattr(Path, "unlink", spy)
    broker.release("sess-chan-0001")
    assert cb.CHANNEL_SOCKET_PATH not in unlinked
    reply = broker.handle_channel_request("sess-chan-0001", _get_line(binding))
    assert reply["result"]["status"] == "refused"


def test_bind_rejects_unknown_transport(home):
    broker = CredentialBroker(install_db=home / "installs.db", transport=Upstream().transport)
    with pytest.raises(BindError):
        broker.bind(session_id="s-1", config_path=_write_config(home), run_home=home / "r", actor=None,
                    listener=None, transport="tcp")


@pytest.mark.parametrize("probe", ["raises", "not_callable", "returns_none"])
def test_channel_broken_client_gone_probe_counts_as_gone(home, probe):
    _store(home)
    upstream = Upstream()
    broker = CredentialBroker(install_db=home / "installs.db", transport=upstream.transport)
    try:
        binding, _ = _channel(broker, home, "sess-chan-0001", listener=FakeListener(["approve"]))
        line = _line(binding, op="http", source="ck_send", tool="ck_send", path_params={"campaign_id": "c"},
                     json={})

        def boom():
            raise RuntimeError("pipe dead")

        gone = {"raises": boom, "not_callable": "yes", "returns_none": lambda: None}[probe]
        reply = broker.handle_channel_request("sess-chan-0001", line, client_gone=gone)
        assert reply["result"]["status"] == "refused"
        assert "went away" in reply["error"]
        assert upstream.requests == []
        run = broker._runs["sess-chan-0001"]
        assert len(run.grants) == 0
        assert run.pending_approvals == 0
        assert run.slots._value == cb.MAX_INFLIGHT_PER_RUN
    finally:
        broker.close()


@pytest.mark.parametrize("sid", [None, 123, ["sess-chan-0001"], {"a": 1}, b"sess-chan-0001", ""])
def test_channel_non_str_session_id_is_refused(home, sid):
    _store(home)
    upstream = Upstream()
    broker = CredentialBroker(install_db=home / "installs.db", transport=upstream.transport)
    try:
        binding, _ = _channel(broker, home, "sess-chan-0001")
        reply = broker.handle_channel_request(sid, _get_line(binding))  # type: ignore[arg-type]
        assert reply["ok"] is False
        assert reply["result"]["status"] == "refused"
        assert upstream.requests == []
    finally:
        broker.close()
