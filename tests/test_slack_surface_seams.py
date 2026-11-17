"""The Slack surface's seams: a shared delivery store, an injected run
submitter, poster and secret resolver, channel policy, watched channels,
thread feedback and button clicks. Every seam defaults to the old behaviour.
No test here touches the network."""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from types import SimpleNamespace

import pytest

from fabri.service import slack_notify
from fabri.service.surfaces import pipeline
from fabri.service.surfaces import slack as slack_surface
from fabri.service.surfaces.delivery_store import DeliveryStore, InMemoryDeliveryStore
from fabri.service.surfaces.registry import SurfaceRegistry
from fabri.service.surfaces.slack import (
    DefaultSlackSecrets,
    SlackAdapter,
    SlackPoster,
    SlackSecrets,
)
from fabri.service.surfaces.types import (
    Dispatch,
    Feedback,
    HitlAnswer,
    Ignore,
    Interaction,
    ReplyTarget,
    TenantRef,
)

SECRET = "seam-secret"
BOT_USER = "U_BOT"


# --- helpers ------------------------------------------------------------------


def _sign(body: bytes, *, secret: str = SECRET, ts: str | None = None) -> dict:
    ts = ts or str(int(time.time()))
    base = b"v0:" + ts.encode() + b":" + body
    sig = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig}


def _event_body(event: dict, *, event_id: str = "Ev1", team_id: str = "T1", **extra) -> bytes:
    return json.dumps(
        {"type": "event_callback", "event_id": event_id, "team_id": team_id,
         "event": event, **extra}
    ).encode()


def _form_body(payload: dict) -> bytes:
    return urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()


def _click(**overrides) -> dict:
    payload = {
        "type": "block_actions",
        "trigger_id": "trig-1",
        "team": {"id": "T1"},
        "user": {"id": "U_HUMAN"},
        "channel": {"id": "C1"},
        "container": {"type": "message", "message_ts": "50.0", "channel_id": "C1",
                      "thread_ts": "40.0"},
        "message": {"ts": "50.0", "thread_ts": "40.0"},
        "actions": [{"action_id": "approve", "value": "yes", "action_ts": "51.0"}],
        "state": {"values": {"b1": {"note": {"type": "plain_text_input", "value": "lgtm"}}}},
        "response_url": "https://hooks.slack.invalid/x",
    }
    payload.update(overrides)
    return payload


def _mention(channel: str = "C1", ts: str = "10.0", text: str = "<@U_BOT> do it",
             user: str = "U_HUMAN") -> dict:
    return {"type": "app_mention", "channel": channel, "ts": ts, "text": text, "user": user}


def _message(channel: str = "C1", ts: str = "10.0", text: str = "hello",
             user: str = "U_HUMAN", **extra) -> dict:
    return {"type": "message", "channel": channel, "ts": ts, "text": text, "user": user, **extra}


class RecordingPoster:
    def __init__(self) -> None:
        self.posts: list[tuple] = []
        self.updates: list[tuple] = []

    def post(self, token_cfg, channel, text, thread_ts, blocks):
        self.posts.append((token_cfg.get("bot_token"), channel, text, thread_ts, blocks))
        return True

    def update(self, token_cfg, channel, ts, text, blocks):
        self.updates.append((token_cfg.get("bot_token"), channel, ts, text, blocks))
        return True


class RecordingSubmitter:
    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, adapter, service, decision) -> None:
        self.calls.append(decision)


class FakeSecrets:
    def __init__(self, signing: str | None = SECRET, token: str | None = "xoxb-resolved") -> None:
        self.signing = signing
        self.token = token
        self.token_calls: list = []

    def signing_secret(self):
        return self.signing

    def bot_token(self, team_id):
        self.token_calls.append(team_id)
        return self.token


class SqliteDeliveryStore:
    """A durable store the way a host would build one: a UNIQUE key and an
    insert that reports whether it won."""

    def __init__(self, path: str) -> None:
        self.path = path
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS deliveries ("
                " surface TEXT NOT NULL, delivery_id TEXT NOT NULL,"
                " UNIQUE(surface, delivery_id))"
            )

    def claim(self, surface: str, delivery_id: str) -> bool:
        with sqlite3.connect(self.path) as conn:
            try:
                conn.execute(
                    "INSERT INTO deliveries (surface, delivery_id) VALUES (?, ?)",
                    (surface, delivery_id),
                )
            except sqlite3.IntegrityError:
                return False
        return True


class Service:
    """Minimal service: a catalog, no install store unless given."""

    def __init__(self, catalog=None, install_store=None, **attrs) -> None:
        self.catalog = catalog if catalog is not None else {"triage": {}}
        self.install_store = install_store
        self.submits: list = []
        self.answers: list = []
        self.done = threading.Event()
        for key, value in attrs.items():
            setattr(self, key, value)

    def submit(self, task, overrides=None, *, catalog_ref=None, origin=None, **_):
        self.submits.append((task, catalog_ref))
        return "session-1"

    def result(self, session_id):
        self.done.set()
        return {"final_text": "Done", "success": True}

    def answer(self, session_id, question_id, answer, selected=None):
        self.answers.append((session_id, question_id, answer, selected))


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    pipeline.reset_state()
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    yield
    pipeline.reset_state()


@pytest.fixture
def no_network(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("network call attempted")

    monkeypatch.setattr(urllib.request, "urlopen", boom)


# --- protocols ----------------------------------------------------------------


def test_defaults_satisfy_their_protocols(tmp_path):
    assert isinstance(InMemoryDeliveryStore(), DeliveryStore)
    assert isinstance(SqliteDeliveryStore(str(tmp_path / "d.db")), DeliveryStore)
    assert isinstance(slack_surface.DefaultSlackPoster(), SlackPoster)
    assert isinstance(DefaultSlackSecrets({}), SlackSecrets)
    assert isinstance(pipeline.DefaultSubmitter(), pipeline.RunSubmitter)


def test_in_memory_store_keeps_the_last_512_per_surface():
    store = InMemoryDeliveryStore()
    assert store.claim("slack", "a") is True
    assert store.claim("slack", "a") is False
    assert store.claim("github", "a") is True
    for i in range(512):
        store.claim("slack", f"id-{i}")
    assert store.claim("slack", "a") is True  # fell off the end


def test_is_duplicate_still_works_and_reset_clears_the_default_store():
    assert pipeline._is_duplicate("slack", "x") is False
    assert pipeline._is_duplicate("slack", "x") is True
    assert pipeline._is_duplicate("slack", None) is False
    pipeline.reset_state()
    assert pipeline._is_duplicate("slack", "x") is False


# --- two receivers, one store -------------------------------------------------


def test_two_receivers_sharing_a_durable_store_submit_once(tmp_path, no_network):
    store = SqliteDeliveryStore(str(tmp_path / "deliveries.db"))
    submitter = RecordingSubmitter()
    receivers = []
    for _ in range(2):
        service = Service(surface_delivery_store=store, surface_submitter=submitter)
        receivers.append((SlackAdapter({}, service, poster=RecordingPoster()), service))

    body = _event_body(_mention(), event_id="Ev-shared")
    for adapter, service in receivers:
        assert pipeline.dispatch(adapter, service, body, _sign(body)) == (200, "", {})

    assert len(submitter.calls) == 1
    assert isinstance(submitter.calls[0], Dispatch)


def test_kwargs_win_over_service_attributes(tmp_path, no_network):
    service_submitter, kwarg_submitter = RecordingSubmitter(), RecordingSubmitter()
    service = Service(surface_submitter=service_submitter)
    adapter = SlackAdapter({}, service, poster=RecordingPoster())
    store = SqliteDeliveryStore(str(tmp_path / "d.db"))
    body = _event_body(_mention(), event_id="Ev-kw")
    pipeline.dispatch(adapter, service, body, _sign(body),
                      delivery_store=store, submitter=kwarg_submitter)
    pipeline.dispatch(adapter, service, body, _sign(body),
                      delivery_store=store, submitter=kwarg_submitter)
    assert len(kwarg_submitter.calls) == 1 and service_submitter.calls == []
    # The default in-memory store never saw it.
    assert pipeline._is_duplicate("slack", "Ev-kw") is False


# --- default path -------------------------------------------------------------


def test_default_path_unchanged_with_a_recording_poster(no_network):
    service = Service(catalog={})
    poster = RecordingPoster()
    adapter = SlackAdapter({"enabled": True}, service, poster=poster)
    body = _event_body(_mention(text="<@U_BOT> summarize"), event_id="Ev-default")

    assert pipeline.dispatch(adapter, service, body, _sign(body)) == (200, "", {})
    assert service.done.wait(1)
    deadline = time.time() + 1
    while len(poster.posts) < 2 and time.time() < deadline:
        time.sleep(0.01)
    assert service.submits == [("summarize", None)]
    assert poster.posts[0] == (None, "C1", "On it...", "10.0", None)
    assert poster.posts[1][2] == "Done"

    # In-memory dedupe: a retry does nothing.
    assert pipeline.dispatch(adapter, service, body, _sign(body)) == (200, "", {})
    time.sleep(0.02)
    assert service.submits == [("summarize", None)]
    assert pipeline._is_duplicate("slack", "Ev-default") is True


def test_default_poster_calls_slack_notify_with_the_old_signature(monkeypatch):
    calls = []

    def old_style(cfg, text, channel, thread_ts=None):
        calls.append((text, channel, thread_ts))
        return True

    monkeypatch.setattr(slack_surface, "post_slack_message", old_style)
    adapter = SlackAdapter({"enabled": True})
    target = ReplyTarget(TenantRef("slack"), {"channel": "C1", "thread_ts": "1.0"})
    assert adapter.deliver_error(target, "nope")
    assert calls == [("nope", "C1", "1.0")]


def test_default_submitter_ignores_feedback_and_interaction():
    submitter = pipeline.DefaultSubmitter(run_in_background=False)
    target = ReplyTarget(TenantRef("slack"), {"channel": "C1", "thread_ts": "1.0"})
    submitter(SlackAdapter({}), Service(), Feedback(target, "hi"))
    submitter(SlackAdapter({}), Service(), Interaction(target))


# --- injected submitter -------------------------------------------------------


def test_injected_submitter_gets_dispatch_with_meta_and_no_ack(no_network):
    submitter = RecordingSubmitter()
    poster = RecordingPoster()
    service = Service(surface_submitter=submitter)
    adapter = SlackAdapter({}, service, poster=poster)
    body = _event_body(_mention(text="<@U_BOT> run triage look at this"), event_id="Ev-meta")

    assert pipeline.dispatch(adapter, service, body, _sign(body)) == (200, "", {})
    time.sleep(0.02)
    assert poster.posts == []
    assert service.submits == []
    [decision] = submitter.calls
    assert decision.command.catalog_ref == "triage"
    assert decision.command.task == "look at this"
    assert decision.meta == {"trigger": "mention", "channel": "C1", "event_id": "Ev-meta",
                             "user": "U_HUMAN", "ts": "10.0"}


def test_catalog_and_quota_checks_still_run_before_an_injected_submitter(no_network):
    submitter = RecordingSubmitter()
    poster = RecordingPoster()
    service = Service(surface_submitter=submitter)
    adapter = SlackAdapter({}, service, poster=poster)
    body = _event_body(_mention(text="<@U_BOT> run nope do it"), event_id="Ev-unknown")
    pipeline.dispatch(adapter, service, body, _sign(body))
    assert submitter.calls == []
    assert "I don't know `nope`" in poster.posts[0][2]


# --- channel policy -----------------------------------------------------------


def _classify(cfg: dict, event: dict, **payload_extra):
    adapter = SlackAdapter(cfg, Service())
    return adapter.classify({"event_id": "Ev", "team_id": "T1", "event": event, **payload_extra})


@pytest.mark.parametrize(
    ("cfg", "acted_on"),
    [
        pytest.param({}, True, id="absent"),
        pytest.param({"channel_allowlist": []}, False, id="empty"),
        pytest.param({"channel_allowlist": ["C1"]}, True, id="member"),
        pytest.param({"channel_allowlist": ["C9"]}, False, id="non-member"),
    ],
)
def test_allowlist_applies_to_mentions_posts_feedback_and_clicks(cfg, acted_on):
    watched = {**cfg, "watched_channels": {"C1": "triage"}}
    decisions = [
        _classify(cfg, _mention()),
        _classify(watched, _message()),
        _classify(watched, _message(ts="11.0", thread_ts="10.0")),
        SlackAdapter(cfg, Service()).classify(_click()),
    ]
    expected = (Dispatch, Dispatch, Feedback, Interaction)
    for decision, kind in zip(decisions, expected, strict=True):
        assert isinstance(decision, kind if acted_on else Ignore), decision


def test_watched_channel_top_level_post_dispatches_with_mapped_ref():
    cfg = {"watched_channels": {"C1": "triage"}}
    for event in (_message(text="the build is red"), _message(text="x", thread_ts="10.0")):
        decision = _classify(cfg, event)
        assert isinstance(decision, Dispatch)
        assert decision.command.verb == "run"
        assert decision.command.catalog_ref == "triage"
        assert decision.target.locator == {"channel": "C1", "thread_ts": "10.0"}
        assert decision.meta["trigger"] == "channel_post"


def test_watched_channel_thread_reply_is_feedback_elsewhere_hitl():
    cfg = {"watched_channels": {"C1": "triage"}}
    reply = _message(ts="11.0", thread_ts="10.0", text="try again")
    decision = _classify(cfg, reply)
    assert decision == Feedback(
        ReplyTarget(TenantRef("slack", "T1"), {"channel": "C1", "thread_ts": "10.0"}),
        "try again", user="U_HUMAN", ts="11.0",
    )
    assert decision.meta["trigger"] == "thread_reply"
    elsewhere = _classify(cfg, {**reply, "channel": "C2"})
    assert elsewhere == HitlAnswer(
        ReplyTarget(TenantRef("slack", "T1"), {"channel": "C2", "thread_ts": "10.0"}),
        "try again",
    )
    # Not watched and not in a thread: ignored, as before.
    assert isinstance(_classify(cfg, _message(channel="C2")), Ignore)


@pytest.mark.parametrize(
    ("event", "extra"),
    [
        pytest.param(_message(user=BOT_USER), {}, id="bot-user-id"),
        pytest.param(_message(user="U_OTHERBOT"),
                     {"authorizations": [{"user_id": "U_OTHERBOT", "is_bot": True}]},
                     id="authorizations"),
        pytest.param(_message(bot_id="B1"), {}, id="bot-id"),
        pytest.param(_message(subtype="message_changed"), {}, id="subtype"),
        pytest.param(_message(ts="11.0", thread_ts="10.0", user=BOT_USER), {}, id="bot-reply"),
        pytest.param(_mention(user=BOT_USER), {}, id="bot-mention"),
    ],
)
def test_own_bot_and_bot_messages_are_dropped(event, extra):
    cfg = {"watched_channels": {"C1": "triage"}, "bot_user_id": BOT_USER}
    assert isinstance(_classify(cfg, event, **extra), Ignore)


def test_authorizations_human_user_is_not_mistaken_for_the_bot():
    cfg = {"watched_channels": {"C1": "triage"}}
    extra = {"authorizations": [{"user_id": "U_HUMAN", "is_bot": False}]}
    assert isinstance(_classify(cfg, _message(), **extra), Dispatch)


def test_mention_and_message_for_one_post_both_reach_the_submitter(no_network):
    """Slack sends app_mention and message for the same post with different
    event ids. Both reach the submitter; the host collapses them by job key
    (channel + ts from meta)."""
    submitter = RecordingSubmitter()
    service = Service(surface_submitter=submitter)
    adapter = SlackAdapter({"watched_channels": {"C1": "triage"}}, service,
                           poster=RecordingPoster())
    text = "<@U_BOT> the build is red"
    for event_id, event in (("Ev-m", _mention(text=text)), ("Ev-p", _message(text=text))):
        body = _event_body(event, event_id=event_id)
        pipeline.dispatch(adapter, service, body, _sign(body))
    assert [d.meta["trigger"] for d in submitter.calls] == ["mention", "channel_post"]
    assert {(d.meta["channel"], d.meta["ts"]) for d in submitter.calls} == {("C1", "10.0")}


# --- feedback through the pipeline --------------------------------------------


def test_feedback_answers_a_pending_question_first(no_network):
    submitter = RecordingSubmitter()
    poster = RecordingPoster()
    service = Service(surface_submitter=submitter)
    adapter = SlackAdapter({"watched_channels": {"C1": "triage"}}, service, poster=poster)
    target = ReplyTarget(TenantRef("slack"), {"channel": "C1", "thread_ts": "10.0"})
    pipeline.register_target("s-1", "slack", target)
    pipeline.set_pending_question("s-1", {"question_id": "q-1", "options": []})

    body = _event_body(_message(ts="11.0", thread_ts="10.0", text="ship it"), event_id="Ev-a")
    pipeline.dispatch(adapter, service, body, _sign(body))
    assert service.answers == [("s-1", "q-1", "ship it", None)]
    assert poster.posts == [(None, "C1", "Got it.", "10.0", None)]
    assert submitter.calls == []

    body = _event_body(_message(ts="12.0", thread_ts="10.0", text="also this"), event_id="Ev-b")
    pipeline.dispatch(adapter, service, body, _sign(body))
    [feedback] = submitter.calls
    assert isinstance(feedback, Feedback) and feedback.text == "also this"


# --- interactivity ------------------------------------------------------------


def _interactive_adapter(**cfg):
    submitter = RecordingSubmitter()
    service = Service(surface_submitter=submitter)
    adapter = SlackAdapter({"interactivity_enabled": True, **cfg}, service,
                           poster=RecordingPoster())
    return adapter, service, submitter


def test_signed_click_becomes_an_interaction(no_network):
    adapter, service, submitter = _interactive_adapter()
    body = _form_body(_click())
    headers = {**_sign(body), "content-type": "application/x-www-form-urlencoded"}
    assert pipeline.dispatch(adapter, service, body, headers) == (200, "", {})
    [decision] = submitter.calls
    assert isinstance(decision, Interaction)
    assert decision.target == ReplyTarget(TenantRef("slack", "T1"),
                                          {"channel": "C1", "thread_ts": "40.0"})
    assert decision.user == "U_HUMAN"
    assert decision.actions == [{"action_id": "approve", "value": "yes", "action_ts": "51.0"}]
    assert decision.state_values["b1"]["note"]["value"] == "lgtm"
    assert decision.message_ts == "50.0"
    assert decision.meta["delivery_id"] == "interaction:trig-1"


def test_click_thread_falls_back_to_message_ts():
    payload = _click(container={"message_ts": "50.0", "channel_id": "C1"}, message={"ts": "50.0"})
    decision = SlackAdapter({}).classify(payload)
    assert decision.target.locator == {"channel": "C1", "thread_ts": "50.0"}


def test_bad_signature_is_401_before_parsing(no_network):
    adapter, service, submitter = _interactive_adapter()
    # Not even parseable: a 401 proves the signature check ran first.
    body = b"payload=%7Bnot-json"
    headers = {**_sign(body, secret="wrong"), "Content-Type": "application/x-www-form-urlencoded"}
    assert pipeline.dispatch(adapter, service, body, headers)[0] == 401
    signed = {**_sign(body), "Content-Type": "application/x-www-form-urlencoded"}
    assert pipeline.dispatch(adapter, service, body, signed)[0] == 400
    assert pipeline.dispatch(adapter, service, b"payload=x", {})[0] == 401
    assert submitter.calls == []


def test_stale_click_is_401(no_network):
    adapter, service, submitter = _interactive_adapter()
    body = _form_body(_click())
    headers = _sign(body, ts=str(int(time.time()) - 600))
    assert pipeline.dispatch(adapter, service, body, headers)[0] == 401
    assert submitter.calls == []


def test_non_block_actions_payloads_are_ignored(no_network):
    adapter, service, submitter = _interactive_adapter()
    for kind in ("view_submission", "shortcut", "message_action"):
        body = _form_body({"type": kind, "trigger_id": f"t-{kind}", "team": {"id": "T1"}})
        assert pipeline.dispatch(adapter, service, body, _sign(body)) == (200, "", {})
    assert submitter.calls == []


def test_repeat_trigger_id_is_deduped(no_network):
    adapter, service, submitter = _interactive_adapter()
    body = _form_body(_click())
    pipeline.dispatch(adapter, service, body, _sign(body))
    pipeline.dispatch(adapter, service, body, _sign(body))
    assert len(submitter.calls) == 1


def test_click_without_trigger_id_dedupes_on_message_action_and_user():
    payload = _click()
    del payload["trigger_id"]
    assert SlackAdapter({}).delivery_id(payload) == "interaction:50.0:51.0:U_HUMAN"


def test_default_submitter_ignores_clicks_and_returns_200(no_network):
    service = Service()
    adapter = SlackAdapter({"interactivity_enabled": True}, service, poster=RecordingPoster())
    body = _form_body(_click())
    assert pipeline.dispatch(adapter, service, body, _sign(body)) == (200, "", {})
    assert service.submits == []


def test_interactivity_route_only_with_the_flag():
    off = SurfaceRegistry()
    off.register(SlackAdapter({}))
    assert set(off.routes()) == {"/slack/events", "/slack/events/"}
    on = SurfaceRegistry()
    adapter = on.register(SlackAdapter({"interactivity_enabled": True}))
    assert set(on.routes()) == {"/slack/events", "/slack/events/",
                                "/slack/interactivity", "/slack/interactivity/"}
    assert on.routes()["/slack/interactivity"] is adapter


def test_update_message_goes_through_the_poster():
    poster = RecordingPoster()
    adapter = SlackAdapter({}, Service(), poster=poster)
    target = ReplyTarget(TenantRef("slack"), {"channel": "C1", "thread_ts": "40.0"})
    assert adapter.update_message(target, "50.0", "Approved", [{"type": "section"}])
    assert poster.updates == [(None, "C1", "50.0", "Approved", [{"type": "section"}])]


# --- secrets ------------------------------------------------------------------


def test_injected_secrets_drive_verify_and_outbound_token(monkeypatch, no_network):
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    secrets = FakeSecrets(signing="resolver-secret", token="xoxb-from-vault")
    poster = RecordingPoster()
    service = Service(catalog={})
    adapter = SlackAdapter({}, service, poster=poster, secrets=secrets)
    body = _event_body(_mention(text="<@U_BOT> help"), event_id="Ev-sec")

    assert pipeline.dispatch(adapter, service, body, _sign(body))[0] == 401
    assert pipeline.dispatch(adapter, service, body, _sign(body, secret="resolver-secret")) == (
        200, "", {})
    assert poster.posts and poster.posts[0][0] == "xoxb-from-vault"
    assert secrets.token_calls == ["T1"]


def test_missing_resolver_secret_fails_closed(no_network):
    adapter = SlackAdapter({}, Service(), secrets=FakeSecrets(signing=None))
    body = _event_body(_mention())
    assert adapter.verify(body, _sign(body)).status == 401


def test_default_secrets_read_env_and_install_store(monkeypatch):
    store = SimpleNamespace(get_token=lambda team: "xoxb-installed" if team == "T1" else None)
    service = SimpleNamespace(install_store=store)
    monkeypatch.setenv("CUSTOM_SIGNING", "s1")
    monkeypatch.setenv("CUSTOM_BOT", "xoxb-env")
    secrets = DefaultSlackSecrets(
        {"signing_secret_env": "CUSTOM_SIGNING", "bot_token_env": "CUSTOM_BOT"}, service
    )
    assert secrets.signing_secret() == "s1"
    assert secrets.bot_token("T1") == "xoxb-installed"
    assert secrets.bot_token("T2") == "xoxb-env"
    assert DefaultSlackSecrets({"bot_token": "xoxb-cfg"}).bot_token(None) == "xoxb-cfg"


def test_health_with_an_injected_resolver_needs_no_env(monkeypatch):
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    adapter = SlackAdapter({}, SimpleNamespace(install_store=None), secrets=FakeSecrets())
    assert adapter.required_env() == [] and adapter.outbound_env() == []
    assert adapter.health()["ok"] is True
    default = SlackAdapter({}, SimpleNamespace(install_store=None))
    assert default.required_env() == ["SLACK_SIGNING_SECRET"]
    assert default.outbound_env() == [["SLACK_BOT_TOKEN"]]


# --- slack_notify -------------------------------------------------------------


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


@pytest.fixture
def captured(monkeypatch):
    requests: list = []

    def fake_urlopen(request, timeout):
        requests.append((request.full_url, json.loads(request.data)))
        return _Response({"ok": True, "ts": "9.9"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return requests


def test_post_includes_blocks_only_when_given(captured):
    cfg = {"enabled": True, "bot_token": "xoxb"}
    assert slack_notify.post_slack_message(cfg, "hi", "C1", "1.0")
    assert slack_notify.post_slack_message(cfg, "hi", "C1", blocks=[{"type": "divider"}])
    assert captured[0] == ("https://slack.com/api/chat.postMessage",
                           {"channel": "C1", "text": "hi", "thread_ts": "1.0"})
    assert captured[1][1] == {"channel": "C1", "text": "hi", "blocks": [{"type": "divider"}]}


def test_update_slack_message_posts_to_chat_update(captured):
    cfg = {"enabled": True, "bot_token": "xoxb"}
    assert slack_notify.update_slack_message(cfg, "C1", "5.0", "edited")
    assert slack_notify.update_slack_message(cfg, "C1", "5.0", "edited", [{"type": "divider"}])
    assert captured[0] == ("https://slack.com/api/chat.update",
                           {"channel": "C1", "ts": "5.0", "text": "edited"})
    assert captured[1][1]["blocks"] == [{"type": "divider"}]
    assert not slack_notify.update_slack_message({"enabled": False}, "C1", "5.0", "x")
