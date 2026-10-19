"""JevDecider: request body shape, noul mapping, confidence math, retry,
cost, connection reuse, and that the API key never leaks into errors. HTTP is
mocked by replacing fabri.decide.jev._open_connection with a fake transport;
only the keep-alive tests at the end talk to a local server on 127.0.0.1."""

from __future__ import annotations

import http.client
import json
import threading

import pytest

import fabri.decide.jev as jev_mod
from fabri.decide import DecideError, JevDecider, Question

SECRET = "sk-test-SECRET-123"


class _Status:
    """A scripted non-JSON outcome: an HTTP status with a raw body."""

    def __init__(self, code: int, body: bytes = b"", will_close: bool = False):
        self.code = code
        self.body = body
        self.will_close = will_close


def _http_error(code: int, body: str = "") -> _Status:
    return _Status(code, body.encode())


class _Req:
    """What one fake request carried, with the accessors the asserts use."""

    def __init__(self, conn: _FakeConn, method: str, path: str, body: bytes, headers: dict):
        self.conn = conn
        self.method = method
        self.data = body
        self.headers = {k.lower(): v for k, v in headers.items()}
        port = f":{conn.port}" if conn.port else ""
        self.full_url = f"{conn.scheme}://{conn.host}{port}{path}"

    def get_method(self) -> str:
        return self.method

    def get_header(self, name: str):
        return self.headers.get(name.lower())


class _FakeResp:
    def __init__(self, outcome: _Status):
        self.status = outcome.code
        self.will_close = outcome.will_close
        self._raw = outcome.body

    def read(self) -> bytes:
        return self._raw


class _FakeConn:
    def __init__(self, transport: _FakeTransport, scheme: str, host: str, port, timeout):
        self.transport = transport
        self.scheme, self.host, self.port, self.timeout = scheme, host, port, timeout
        self.closed = False
        self.requests: list[_Req] = []
        self._pending: _Status | None = None

    def request(self, method, url, body=None, headers=None):
        req = _Req(self, method, url, body, headers or {})
        outcome = self.transport._next(req)
        self.requests.append(req)
        if isinstance(outcome, BaseException):
            raise outcome
        self._pending = outcome

    def getresponse(self):
        outcome, self._pending = self._pending, None
        return _FakeResp(outcome)

    def close(self):
        self.closed = True


class _FakeTransport:
    """Stands in for _open_connection. Each scripted outcome answers one
    request, in order: a dict is a 200 JSON body, bytes a raw 200 body, a
    _Status a status response, an exception is raised from conn.request()."""

    def __init__(self, outcomes: list):
        self.outcomes = list(outcomes)
        self.requests: list[_Req] = []
        self.connections: list[_FakeConn] = []
        self._lock = threading.Lock()

    def __call__(self, scheme, host, port, timeout):
        conn = _FakeConn(self, scheme, host, port, timeout)
        with self._lock:
            self.connections.append(conn)
        return conn

    def _next(self, req: _Req):
        with self._lock:
            self.requests.append(req)
            outcome = self.outcomes.pop(0)
        if isinstance(outcome, dict):
            return _Status(200, json.dumps(outcome).encode())
        if isinstance(outcome, bytes):
            return _Status(200, outcome)
        return outcome

    def body(self, i: int = 0) -> dict:
        return json.loads(self.requests[i].data.decode())


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", SECRET)
    return SECRET


def _install(monkeypatch, outcomes) -> _FakeTransport:
    fake = _FakeTransport(outcomes)
    fake.sleeps = []
    monkeypatch.setattr(jev_mod, "_open_connection", fake)
    monkeypatch.setattr("fabri.decide.jev.time.sleep", fake.sleeps.append)
    return fake


QUESTIONS = [
    Question("intent", "choice", "Which agent?", options={"canvas": "edit canvas", "tasks": None}),
    Question("urgency", "score", "How urgent?", levels=["low", "medium", "high"]),
    Question("risky", "yesno", "Is it risky?", options={"true": "affects others", "false": "own work"}),
    Question("plain", "yesno", "Plain yes/no?"),
]

OK_PAYLOAD = {
    "model": "jev-1.13.0-20260917",
    "answers": {
        "intent": {"choice": "canvas", "probabilities": {"canvas": 0.8, "tasks": 0.2}, "confidence": 0.8},
        # Shape captured from a live OpenRouter call: an expected index plus a legend.
        "urgency": {"type": "score", "score": 1.6, "legend": {"0": "low", "1": "medium", "2": "high"},
                    "probabilities": {"0": 0.1, "1": 0.2, "2": 0.7}, "confidence": 0.7},
        "risky": {"noul": 0.9},
        "plain": {"noul": 0.25},
    },
    "usage": {"input_tokens": 1000, "output_tokens": 40},
}


def test_request_shape_per_kind(monkeypatch, key):
    fake = _install(monkeypatch, [OK_PAYLOAD])
    JevDecider().decide("user wants to edit the canvas", QUESTIONS)

    assert len(fake.requests) == 1  # one call for all questions
    req = fake.requests[0]
    assert req.full_url == "https://api.typesafe.ai/v1/systemone"
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == f"Bearer {SECRET}"
    assert req.get_header("Content-type") == "application/json"
    body = fake.body()
    assert body["model"] == "jev-1.13.0"
    assert body["state"] == "user wants to edit the canvas"
    qs = body["questions"]
    assert qs["intent"] == {"type": "choice", "instructions": "Which agent?",
                            "criteria": {"canvas": "edit canvas", "tasks": None}}
    assert qs["urgency"] == {"type": "score", "instructions": "How urgent?",
                             "criteria": ["low", "medium", "high"]}
    assert qs["risky"] == {"type": "noul", "instructions": "Is it risky?",
                           "criteria": {"true": "affects others", "false": "own work"}}
    assert qs["plain"] == {"type": "noul", "instructions": "Plain yes/no?"}


def test_structured_state_is_sent_as_json(monkeypatch, key):
    fake = _install(monkeypatch, [OK_PAYLOAD])
    JevDecider().decide({"messages": ["hi"]}, QUESTIONS)
    assert fake.body()["state"] == {"messages": ["hi"]}


def test_openrouter_provider_uses_its_base_and_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    fake = _install(monkeypatch, [OK_PAYLOAD])
    JevDecider(model="typesafe/jev-1.13", provider="openrouter").decide("s", QUESTIONS)
    assert fake.requests[0].full_url == "https://openrouter.ai/api/v1/systemone"
    assert fake.requests[0].get_header("Authorization") == "Bearer or-key"
    assert fake.body()["model"] == "typesafe/jev-1.13"


def test_base_url_override(monkeypatch, key):
    fake = _install(monkeypatch, [OK_PAYLOAD])
    JevDecider(base_url="http://localhost:9/v9/").decide("s", QUESTIONS)
    assert fake.requests[0].full_url == "http://localhost:9/v9/systemone"


def test_answer_values_and_confidence_math(monkeypatch, key):
    _install(monkeypatch, [OK_PAYLOAD])
    out = JevDecider().decide("s", QUESTIONS)

    assert out["intent"].value == "canvas"
    assert out["intent"].confidence == pytest.approx(0.8)
    assert out["intent"].probabilities == {"canvas": 0.8, "tasks": 0.2}
    assert out["urgency"].value == "high"
    assert out["urgency"].confidence == pytest.approx(0.7)

    risky = out["risky"]
    assert risky.kind == "yesno"
    assert risky.value is True
    assert risky.confidence == pytest.approx(0.8)  # |2*0.9 - 1|
    assert risky.probabilities == pytest.approx({"true": 0.9, "false": 0.1})

    plain = out["plain"]
    assert plain.value is False
    assert plain.confidence == pytest.approx(0.5)  # |2*0.25 - 1|

    for a in out.values():
        assert a.source == "jev"
        assert a.error is None
        assert a.model == "jev-1.13.0-20260917"  # returned id kept


def test_noul_exactly_half_is_true_with_zero_confidence(monkeypatch, key):
    payload = {"model": "jev-1.13.0", "answers": {"q": {"noul": 0.5}}, "usage": {}}
    _install(monkeypatch, [payload])
    a = JevDecider().decide("s", [Question("q", "yesno", "?")])["q"]
    assert a.value is True
    assert a.confidence == pytest.approx(0.0)


def test_cost_from_pricing_when_usage_has_no_cost(monkeypatch, key):
    _install(monkeypatch, [OK_PAYLOAD])
    out = JevDecider().decide("s", QUESTIONS)
    total = sum(a.cost_usd for a in out.values())
    # 1000 input tokens at $0.042/MTok, output free
    assert total == pytest.approx(1000 * 0.042 / 1_000_000, abs=1e-9)
    assert sum(a.input_tokens for a in out.values()) == 1000
    assert sum(a.output_tokens for a in out.values()) == 40


def test_reported_usage_cost_wins(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    payload = {
        "model": "typesafe/jev-1.13-20260917",
        "answers": {"intent": {"type": "choice", "choice": "canvas",
                               "probabilities": {"canvas": 1, "tasks": 0}, "confidence": 1}},
        "usage": {"input_tokens": 343, "output_tokens": 38, "cost": 0.000014406},
        "id": "gen-dec-1",
        "provider": "TypeSafe",
    }
    _install(monkeypatch, [payload])
    out = JevDecider(model="typesafe/jev-1.13", provider="openrouter").decide("s", QUESTIONS[:1])
    a = out["intent"]
    assert a.cost_usd == pytest.approx(0.000014406)
    assert a.model == "typesafe/jev-1.13-20260917"
    assert a.confidence == 1.0


def test_retries_once_on_429(monkeypatch, key):
    fake = _install(monkeypatch, [_http_error(429, "slow down"), OK_PAYLOAD])
    out = JevDecider().decide("s", QUESTIONS)
    assert len(fake.requests) == 2
    assert out["intent"].value == "canvas"


def test_retries_once_on_5xx_then_fails(monkeypatch, key):
    fake = _install(monkeypatch, [_http_error(503), _http_error(502, "bad gateway")])
    with pytest.raises(DecideError, match="HTTP 502"):
        JevDecider().decide("s", QUESTIONS)
    assert len(fake.requests) == 2


def test_no_retry_on_400(monkeypatch, key):
    fake = _install(monkeypatch, [_http_error(400, "bad question")])
    with pytest.raises(DecideError, match="HTTP 400"):
        JevDecider().decide("s", QUESTIONS)
    assert len(fake.requests) == 1


def test_key_never_in_error_text(monkeypatch, key):
    # The provider echoes the key back in its error body; it must be redacted.
    _install(monkeypatch, [_http_error(401, f"invalid key {SECRET}")])
    with pytest.raises(DecideError) as exc:
        JevDecider().decide("s", QUESTIONS)
    assert SECRET not in str(exc.value)
    assert "401" in str(exc.value)


def test_key_never_in_network_error_text(monkeypatch, key):
    _install(monkeypatch, [ConnectionRefusedError(f"refused for {SECRET}")])
    with pytest.raises(DecideError) as exc:
        JevDecider().decide("s", QUESTIONS)
    assert SECRET not in str(exc.value)


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    _install(monkeypatch, [])
    with pytest.raises(DecideError, match="TYPESAFE_API_KEY"):
        JevDecider().decide("s", QUESTIONS)


def test_invalid_json_response(monkeypatch, key):
    _install(monkeypatch, [b"<html>oops</html>"])
    with pytest.raises(DecideError, match="not valid JSON"):
        JevDecider().decide("s", QUESTIONS)


def test_score_index_is_mapped_through_legend(monkeypatch, key):
    # Live Jev sends an expected index (2.96 here), not a level name. The most
    # likely level wins, so value and confidence describe the same level.
    payload = {"model": "typesafe/jev-1.13-20260917", "answers": {"u": {
        "type": "score", "score": 2.96, "legend": {"0": "low", "1": "medium", "2": "high", "3": "critical"},
        "probabilities": {"0": 0, "1": 0, "2": 0.04, "3": 0.96}, "confidence": 0.96}},
        "usage": {"input_tokens": 315, "output_tokens": 17, "cost": 1.323e-05}}
    _install(monkeypatch, [payload])
    q = Question("u", "score", "How urgent?", levels=["low", "medium", "high", "critical"])
    out = JevDecider().decide("payments down", [q])["u"]
    assert out.value == "critical"
    assert out.confidence == pytest.approx(0.96)
    assert out.probabilities == {"low": 0.0, "medium": 0.0, "high": 0.04, "critical": 0.96}
    assert out.cost_usd == pytest.approx(1.323e-05)


def test_missing_or_out_of_set_answer_is_errored_not_raised(monkeypatch, key):
    payload = {"model": "jev-1.13.0", "answers": {"intent": {"choice": "nope", "confidence": 0.9}},
               "usage": {"input_tokens": 10, "output_tokens": 0}}
    _install(monkeypatch, [payload])
    out = JevDecider().decide("s", QUESTIONS[:2])
    assert out["intent"].value is None and "not one of" in out["intent"].error
    assert out["urgency"].value is None and out["urgency"].error


def test_unknown_provider_rejected():
    with pytest.raises(ValueError, match="unknown jev provider"):
        JevDecider(provider="nope")


# ---------------------------------------------------------------- review fixes


def _one(monkeypatch, question, raw_answer, usage=None, model="jev-1.13.0"):
    payload = {"model": model, "answers": {question.key: raw_answer},
               "usage": {"input_tokens": 10, "output_tokens": 0} if usage is None else usage}
    _install(monkeypatch, [payload])
    return JevDecider().decide("s", [question])[question.key]


YESNO = Question("q", "yesno", "?")
CHOICE = Question("c", "choice", "Which?", options={"canvas": None, "tasks": None})


@pytest.mark.parametrize("noul", [1.5, -0.1, float("nan"), float("inf"), "0.9", None, True, [0.9]])
def test_noul_must_be_finite_probability(monkeypatch, key, noul):
    a = _one(monkeypatch, YESNO, {"noul": noul})
    assert a.value is None
    assert a.error and "malformed" in a.error
    assert a.confidence is None and a.probabilities == {}


def test_bad_noul_is_escalated_by_cascade(monkeypatch, key):
    from fabri.decide import Answer, CascadeDecider

    class _Fallback:
        def decide(self, state, questions):
            return {q.key: Answer(key=q.key, kind=q.kind, value=False, source="llm") for q in questions}

    _install(monkeypatch, [{"model": "jev-1.13.0", "answers": {"q": {"noul": 7}}, "usage": {}}])
    out = CascadeDecider(JevDecider(), _Fallback()).decide("s", [YESNO])
    assert out["q"].escalated is True and out["q"].value is False


@pytest.mark.parametrize("raw", [
    {"choice": "canvas", "probabilities": {"canvas": 1.4, "tasks": 0.0}},
    {"choice": "canvas", "probabilities": {"canvas": float("nan"), "tasks": 0.1}},
    {"choice": "canvas", "probabilities": {"canvas": 0.9, "tasks": 0.1}, "confidence": 1.2},
    {"choice": "canvas", "probabilities": {"canvas": 0.9, "tasks": 0.1}, "confidence": float("-inf")},
    {"choice": "canvas", "probabilities": {"canvas": 0.9, "tasks": 0.1}, "confidence": "high"},
])
def test_choice_probabilities_and_confidence_must_be_finite_in_unit_interval(monkeypatch, key, raw):
    a = _one(monkeypatch, CHOICE, raw)
    assert a.value is None and "malformed" in a.error


def test_score_probabilities_validated(monkeypatch, key):
    q = Question("u", "score", "How urgent?", levels=["low", "high"])
    a = _one(monkeypatch, q, {"score": 0.9, "legend": {"0": "low", "1": "high"},
                              "probabilities": {"0": -0.5, "1": 1.5}})
    assert a.value is None and "malformed" in a.error


def test_probability_float_noise_is_clamped(monkeypatch, key):
    a = _one(monkeypatch, CHOICE, {"choice": "canvas", "probabilities": {"canvas": 1.0 + 1e-12, "tasks": -1e-12},
                                   "confidence": 1.0 + 1e-12})
    assert a.error is None
    assert a.probabilities == {"canvas": 1.0, "tasks": 0.0}
    assert a.confidence == 1.0


def test_probabilities_not_a_dict_errors_the_answer(monkeypatch, key):
    a = _one(monkeypatch, CHOICE, {"choice": "canvas", "probabilities": [0.9, 0.1], "confidence": 0.8})
    assert a.value is None and "probabilities must be an object" in a.error


def test_attribute_error_in_answer_is_caught(monkeypatch, key):
    q = Question("u", "score", "How urgent?", levels=["low", "high"])
    # A list legend has no .items(): AttributeError must become an errored answer.
    a = _one(monkeypatch, q, {"score": "low", "legend": ["low", "high"], "probabilities": {"0": 0.9}})
    assert a.value is None and "malformed" in a.error


def test_unexpected_parse_failure_is_decide_error(monkeypatch, key):
    def boom(q, raw):
        raise RuntimeError("surprise")

    monkeypatch.setattr("fabri.decide.jev._parse_answer", boom)
    _install(monkeypatch, [OK_PAYLOAD])
    with pytest.raises(DecideError, match="could not parse response: RuntimeError: surprise"):
        JevDecider().decide("s", QUESTIONS)


@pytest.mark.parametrize("usage", ["oops", ["input_tokens", 3], 12])
def test_usage_not_a_dict_does_not_raise(monkeypatch, key, usage):
    a = _one(monkeypatch, YESNO, {"noul": 0.9}, usage=usage)
    assert a.value is True and a.error is None
    assert a.input_tokens == 0 and a.output_tokens == 0
    assert a.cost_usd == 0.0 and a.cost_known is False


def test_non_int_tokens_count_as_zero_and_reported_cost_still_used(monkeypatch, key):
    a = _one(monkeypatch, YESNO, {"noul": 0.9},
             usage={"input_tokens": "lots", "output_tokens": 2.5, "cost": 0.0003})
    assert a.error is None
    assert a.input_tokens == 0 and a.output_tokens == 0
    assert a.cost_usd == pytest.approx(0.0003) and a.cost_known is True


@pytest.mark.parametrize("cost", [float("nan"), -1.0, True, "0.1"])
def test_invalid_reported_cost_falls_back_to_pricing(monkeypatch, key, cost):
    a = _one(monkeypatch, YESNO, {"noul": 0.9}, usage={"input_tokens": 1000, "output_tokens": 0, "cost": cost})
    assert a.cost_usd == pytest.approx(1000 * 0.042 / 1_000_000, abs=1e-9)
    assert a.cost_known is True


@pytest.mark.parametrize("p", [0.0, 0.1, 0.25, 0.5, 0.7, 0.95, 1.0])
def test_yesno_confidence_matches_jev_choice_formula_at_n2(monkeypatch, key, p):
    a = _one(monkeypatch, YESNO, {"noul": p})
    n = 2
    p_max = max(p, 1 - p)
    assert a.confidence == pytest.approx((p_max - 1 / n) / (1 - 1 / n))
    assert a.confidence == pytest.approx(abs(2 * p - 1))


def test_yesno_threshold_08_means_p_at_least_09(monkeypatch, key):
    from fabri.decide import CascadeDecider

    cascade = CascadeDecider(JevDecider(), JevDecider(), thresholds={"yesno": 0.8})
    for p, escalate in ((0.9, False), (0.1, False), (0.89, True), (0.11, True)):
        a = _one(monkeypatch, YESNO, {"noul": p})
        assert cascade.needs_fallback(YESNO, a) is escalate, p


def test_template_readme_states_yesno_threshold_as_probability():
    from fabri.agency_templates.jev_router import README

    assert "0.8\n  for yes/no, which means P >= 0.9" in README


def test_unpriced_model_warns_once_and_marks_cost_unknown(monkeypatch, key, caplog):
    import logging

    monkeypatch.setattr(jev_mod, "_warned_unpriced", set())
    payload = {"model": "mystery-jev-9", "answers": {"q": {"noul": 0.9}},
               "usage": {"input_tokens": 100, "output_tokens": 0}}
    _install(monkeypatch, [payload, payload])
    decider = JevDecider(model="mystery-jev")
    with caplog.at_level(logging.WARNING, logger="fabri.decide.jev"):
        a1 = decider.decide("s", [YESNO])["q"]
        a2 = decider.decide("s", [YESNO])["q"]
    for a in (a1, a2):
        assert a.cost_usd == 0.0 and a.cost_known is False
    warnings = [r for r in caplog.records if r.name == "fabri.decide.jev"]
    assert len(warnings) == 1
    assert "mystery-jev-9" in warnings[0].getMessage()


# ---------------------------------------------------------- connection reuse


YESNO_OK = {"model": "jev-1.13.0", "answers": {"q": {"noul": 0.9}}, "usage": {}}


def test_open_connection_picks_class_by_scheme():
    https = jev_mod._open_connection("https", "api.typesafe.ai", None, 3.0)
    http_ = jev_mod._open_connection("http", "localhost", 9, 3.0)
    assert type(https) is http.client.HTTPSConnection
    assert type(http_) is http.client.HTTPConnection
    assert https.timeout == 3.0 and http_.port == 9


def test_transport_gets_scheme_host_port_and_timeout(monkeypatch, key):
    fake = _install(monkeypatch, [YESNO_OK, YESNO_OK])
    JevDecider(timeout_s=2.5).decide("s", [YESNO])
    JevDecider(base_url="http://localhost:9/v9/").decide("s", [YESNO])
    a, b = fake.connections
    assert (a.scheme, a.host, a.port, a.timeout) == ("https", "api.typesafe.ai", None, 2.5)
    assert (b.scheme, b.host, b.port) == ("http", "localhost", 9)
    assert fake.requests[1].full_url == "http://localhost:9/v9/systemone"


def test_sends_keep_alive_header(monkeypatch, key):
    fake = _install(monkeypatch, [YESNO_OK])
    JevDecider().decide("s", [YESNO])
    assert fake.requests[0].get_header("Connection") == "keep-alive"


def test_connection_reused_across_calls_on_one_thread(monkeypatch, key):
    fake = _install(monkeypatch, [YESNO_OK] * 3)
    d = JevDecider()
    for _ in range(3):
        assert d.decide("s", [YESNO])["q"].value is True
    assert len(fake.connections) == 1
    assert len(fake.connections[0].requests) == 3
    assert not fake.connections[0].closed


def test_separate_connection_per_thread(monkeypatch, key):
    fake = _install(monkeypatch, [YESNO_OK] * 8)
    d = JevDecider()
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def work():
        try:
            barrier.wait()
            d.decide("s", [YESNO])
            d.decide("s", [YESNO])
        except BaseException as e:  # surfaced below
            errors.append(e)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(fake.connections) == 4
    assert all(len(c.requests) == 2 for c in fake.connections)  # each thread reused its own


def test_response_asking_to_close_drops_the_connection(monkeypatch, key):
    closing = _Status(200, json.dumps(YESNO_OK).encode(), will_close=True)
    fake = _install(monkeypatch, [closing, YESNO_OK])
    d = JevDecider()
    d.decide("s", [YESNO])
    d.decide("s", [YESNO])
    assert len(fake.connections) == 2
    assert fake.connections[0].closed


@pytest.mark.parametrize("stale", [
    http.client.RemoteDisconnected("Remote end closed connection without response"),
    BrokenPipeError(32, "Broken pipe"),
    ConnectionResetError(54, "Connection reset by peer"),
    http.client.BadStatusLine(""),
])
def test_reconnects_once_when_server_closed_pooled_connection(monkeypatch, key, stale):
    fake = _install(monkeypatch, [YESNO_OK, stale, YESNO_OK])
    d = JevDecider()
    d.decide("s", [YESNO])
    out = d.decide("s", [YESNO])
    assert out["q"].value is True
    assert len(fake.connections) == 2
    assert fake.connections[0].closed and not fake.connections[1].closed
    assert fake.sleeps == []  # a reconnect is not the 429/5xx retry


def test_reconnect_does_not_use_up_the_429_retry(monkeypatch, key):
    fake = _install(monkeypatch, [YESNO_OK, BrokenPipeError(32, "Broken pipe"), _http_error(429), YESNO_OK])
    d = JevDecider()
    d.decide("s", [YESNO])
    assert d.decide("s", [YESNO])["q"].value is True
    assert len(fake.sleeps) == 1
    assert len(fake.requests) == 4


def test_stale_error_on_fresh_connection_is_not_resent(monkeypatch, key):
    fake = _install(monkeypatch, [ConnectionResetError(54, f"reset {SECRET}")])
    with pytest.raises(DecideError, match="request to https://api.typesafe.ai/v1/systemone failed") as exc:
        JevDecider().decide("s", [YESNO])
    assert SECRET not in str(exc.value)
    assert len(fake.requests) == 1


def test_second_stale_error_fails(monkeypatch, key):
    fake = _install(monkeypatch, [YESNO_OK, ConnectionResetError(54, "reset"), ConnectionResetError(54, "reset")])
    d = JevDecider()
    d.decide("s", [YESNO])
    with pytest.raises(DecideError, match="failed: .*reset"):
        d.decide("s", [YESNO])
    assert len(fake.requests) == 3


def test_timeout_is_a_decide_error(monkeypatch, key):
    _install(monkeypatch, [TimeoutError("timed out")])
    with pytest.raises(DecideError, match="failed: timed out"):
        JevDecider().decide("s", [YESNO])


def test_unsupported_scheme_is_a_decide_error(monkeypatch, key):
    fake = _install(monkeypatch, [])
    with pytest.raises(DecideError, match="request to ftp://x/systemone failed"):
        JevDecider(base_url="ftp://x").decide("s", [YESNO])
    assert fake.connections == []


def test_close_closes_every_connection_and_decider_stays_usable(monkeypatch, key):
    fake = _install(monkeypatch, [YESNO_OK] * 3)
    d = JevDecider()
    d.decide("s", [YESNO])
    t = threading.Thread(target=d.decide, args=("s", [YESNO]))
    t.start()
    t.join()
    assert len(fake.connections) == 2
    d.close()
    assert all(c.closed for c in fake.connections)
    d.close()  # idempotent
    d.decide("s", [YESNO])  # reopens rather than reusing the closed one
    assert len(fake.connections) == 3 and not fake.connections[2].closed


def test_context_manager_closes(monkeypatch, key):
    fake = _install(monkeypatch, [YESNO_OK])
    with JevDecider() as d:
        assert isinstance(d, JevDecider)
        d.decide("s", [YESNO])
        assert not fake.connections[0].closed
    assert fake.connections[0].closed


# ------------------------------------------- real sockets on a local server


def _local_server(close_after_each: bool):
    """An HTTP/1.1 server on 127.0.0.1 that answers /v1/systemone and records
    the client port of every request (same port = same TCP connection)."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    ports: list[int] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            ports.append(self.client_address[1])
            raw = json.dumps(YESNO_OK).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()  # no "Connection: close": the client thinks it may reuse
            self.wfile.write(raw)
            self.wfile.flush()
            if close_after_each:
                self.close_connection = True  # idle close, as a server timeout would

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, ports


def test_real_keep_alive_reuses_one_socket(key):
    server, ports = _local_server(close_after_each=False)
    try:
        with JevDecider(base_url=f"http://127.0.0.1:{server.server_port}/v1", timeout_s=5) as d:
            for _ in range(3):
                assert d.decide("s", [YESNO])["q"].value is True
        assert len(ports) == 3 and len(set(ports)) == 1
    finally:
        server.shutdown()
        server.server_close()


def test_real_reconnect_after_server_closes(key):
    import time as _time

    server, ports = _local_server(close_after_each=True)
    try:
        with JevDecider(base_url=f"http://127.0.0.1:{server.server_port}/v1", timeout_s=5) as d:
            d.decide("s", [YESNO])
            _time.sleep(0.05)  # let the server's FIN arrive
            assert d.decide("s", [YESNO])["q"].value is True
        assert len(ports) == 2 and len(set(ports)) == 2
    finally:
        server.shutdown()
        server.server_close()
