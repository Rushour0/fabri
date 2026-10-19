"""Jev decider: TypeSafe AI's non-generative decision model.

Jev does not speak chat completions, so it cannot run behind fabri's
OpenAI/OpenRouter chat backend. It takes a `state` plus typed questions and
returns probabilities over the allowed answers. Wire format::

    POST {base}/systemone
    {"model": ..., "state": ..., "questions": {key: {"type", "instructions", "criteria"}}}
    -> {"model": ..., "answers": {key: {...}}, "usage": {"input_tokens", "output_tokens", "cost"?}}

Question kinds map to wire types as choice -> "choice", score -> "score",
yesno -> "noul". A noul answer is a single P(true); we report value = P >= 0.5
and confidence = |2P - 1|. Jev's own choice and score confidence is
(p_max - 1/n) / (1 - 1/n) over n answers; with n = 2 and p_max = max(P, 1 - P)
that formula is exactly |2P - 1|, so every kind shares one 0..1 scale.

Every probability and confidence Jev sends must be a finite number in [0, 1].
Anything else (NaN, inf, negative, above 1, a string, a malformed
`probabilities`) makes that answer errored (value None), so a cascade
re-asks it. A malformed `usage` never fails the call: tokens count as 0 and
the cost comes from `usage.cost` when that is valid.

Uses stdlib `http.client` only (no new dependency). One HTTP request per
decide() call covers every question. 429 and 5xx responses are retried once
after a short backoff.

Connections are kept alive and reused: each decider holds one connection per
thread, so repeat calls skip the TCP and TLS handshake. When the server has
closed a pooled connection, the request is resent once on a fresh one; that
reconnect does not use up the 429/5xx retry. `close()` (or a `with` block)
closes every connection the decider opened. Acquires also close connections
owned by threads that have ended.
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import os
import threading
import time
import urllib.parse

from fabri.core.llm import LLMUsage
from fabri.decide.types import Answer, DecideError, Question, sanitize, split_evenly

BASE_URLS: dict[str, str] = {
    "typesafe": "https://api.typesafe.ai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}
KEY_ENVS: dict[str, str] = {
    "typesafe": "TYPESAFE_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}
_WIRE_TYPES = {"choice": "choice", "score": "score", "yesno": "noul"}
_BODY_SNIPPET = 200
# Float noise allowance when checking that a probability lies in [0, 1].
_PROB_TOLERANCE = 1e-9

# Errors that mean a reused keep-alive socket was closed by the server. Sent
# again once on a fresh connection. RemoteDisconnected is a subclass of both
# ConnectionResetError and BadStatusLine.
_STALE_CONNECTION_ERRORS = (
    http.client.RemoteDisconnected,
    BrokenPipeError,
    ConnectionResetError,
    http.client.BadStatusLine,
)

logger = logging.getLogger(__name__)
_warned_unpriced: set[str] = set()


def _open_connection(
    scheme: str, host: str, port: int | None, timeout: float
) -> http.client.HTTPConnection:
    """A new, not yet connected, HTTP(S) connection. Tests replace this to
    mock the transport."""
    if scheme == "http":
        return http.client.HTTPConnection(host, port, timeout=timeout)
    return http.client.HTTPSConnection(host, port, timeout=timeout)


class JevDecider:
    """Ask Jev every question in one request."""

    def __init__(
        self,
        model: str = "jev-1.13.0",
        provider: str = "typesafe",
        api_key_env: str | None = None,
        base_url: str | None = None,
        timeout_s: float = 10.0,
        retry_backoff_s: float = 0.5,
    ) -> None:
        if provider not in BASE_URLS:
            raise ValueError(
                f"unknown jev provider {provider!r} (expected one of {', '.join(BASE_URLS)})"
            )
        self.model = model
        self.provider = provider
        self.api_key_env = api_key_env or KEY_ENVS[provider]
        self.base_url = (base_url or BASE_URLS[provider]).rstrip("/")
        self.timeout_s = timeout_s
        self.retry_backoff_s = retry_backoff_s
        parts = urllib.parse.urlsplit(self.url)
        self._scheme = parts.scheme.lower()
        self._host = parts.hostname or ""
        try:
            self._port = parts.port
        except ValueError:
            self._port = None
            self._host = ""  # bad port: fail at request time like a bad host
        self._path = parts.path or "/"
        if parts.query:
            self._path += f"?{parts.query}"
        # One keep-alive connection per thread. `_conns` retains the owning
        # Thread object so reaping can check liveness while holding this same
        # lock; a thread id alone can be reused and is racy with enumeration.
        # `_generation` tells a thread its cached connection was closed under it.
        self._local = threading.local()
        self._lock = threading.Lock()
        self._conns: dict[threading.Thread, http.client.HTTPConnection] = {}
        self._generation = 0

    @property
    def url(self) -> str:
        return f"{self.base_url}/systemone"

    def close(self) -> None:
        """Close every pooled connection. The decider stays usable: the next
        call opens a fresh connection."""
        with self._lock:
            conns, self._conns = list(self._conns.values()), {}
            self._generation += 1
        self._close_connections(conns)

    def __enter__(self) -> JevDecider:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def build_request(self, state: object, questions: list[Question]) -> dict:
        """The JSON body for one /systemone call."""
        wire: dict[str, dict] = {}
        for q in questions:
            entry: dict = {"type": _WIRE_TYPES[q.kind], "instructions": q.instructions}
            if q.kind == "choice":
                entry["criteria"] = dict(q.options or {})
            elif q.kind == "score":
                entry["criteria"] = list(q.levels or [])
            elif q.options:
                entry["criteria"] = dict(q.options)
            wire[q.key] = entry
        return {"model": self.model, "state": state, "questions": wire}

    def decide(self, state: object, questions: list[Question]) -> dict[str, Answer]:
        if not questions:
            return {}
        _check_unique_keys(questions)
        key = os.environ.get(self.api_key_env)
        if not key:
            raise DecideError(f"jev: API key env var {self.api_key_env} is not set")
        body = json.dumps(self.build_request(state, questions)).encode("utf-8")

        t0 = time.monotonic()
        payload = self._post(body, key)
        latency_ms = (time.monotonic() - t0) * 1000.0
        try:
            return self._parse(payload, questions, latency_ms, (key,))
        except DecideError:
            raise
        except Exception as e:  # never let a surprising payload escape as a bare exception
            detail = sanitize(f"{type(e).__name__}: {e}", (key,))
            raise DecideError(f"jev: could not parse response: {detail}") from None

    def _post(self, body: bytes, key: str) -> dict:
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Connection": "keep-alive",
            "User-Agent": "fabri-jev",
        }
        for attempt in (1, 2):
            try:
                status, raw = self._send(body, headers)
            except (OSError, http.client.HTTPException) as e:
                reason = getattr(e, "reason", e)
                text = str(reason) or type(e).__name__
                raise DecideError(
                    f"jev: request to {self.url} failed: {sanitize(text, (key,))}"
                ) from None
            if 200 <= status < 300:
                break
            if attempt == 1 and (status == 429 or status >= 500):
                time.sleep(self.retry_backoff_s)
                continue
            snippet = sanitize(raw[:_BODY_SNIPPET].decode("utf-8", errors="replace"), (key,))
            hint = " (check the API key)" if status in (401, 403) else ""
            raise DecideError(f"jev: HTTP {status} from {self.url}{hint}: {snippet}") from None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise DecideError(f"jev: response is not valid JSON: {sanitize(e, (key,))}") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("answers"), dict):
            raise DecideError("jev: response has no 'answers' object")
        return payload

    def _send(self, body: bytes, headers: dict[str, str]) -> tuple[int, bytes]:
        """One POST on this thread's pooled connection: (status, body bytes).
        If the server closed a reused connection, resend once on a new one."""
        if self._scheme not in ("http", "https") or not self._host:
            raise http.client.InvalidURL(f"unsupported URL {self.url!r}")
        conn, reused = self._connection()
        try:
            return self._roundtrip(conn, body, headers)
        except _STALE_CONNECTION_ERRORS:
            if not reused:
                raise
            logger.debug("jev: pooled connection was closed by the server; reconnecting")
        conn, _ = self._connection()
        return self._roundtrip(conn, body, headers)

    def _roundtrip(
        self, conn: http.client.HTTPConnection, body: bytes, headers: dict[str, str]
    ) -> tuple[int, bytes]:
        try:
            conn.request("POST", self._path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        except BaseException:
            self._discard(conn)
            raise
        if resp.will_close:
            self._discard(conn)
        else:
            self._local.used = True
        return resp.status, raw

    def _connection(self) -> tuple[http.client.HTTPConnection, bool]:
        """This thread's connection, opening one if needed. The flag says
        whether it has already carried a request (so it may have gone stale)."""
        self._reap_dead_connections()
        local = self._local
        conn = getattr(local, "conn", None)
        if conn is not None and getattr(local, "generation", None) == self._generation:
            return conn, bool(getattr(local, "used", False))
        conn = _open_connection(self._scheme, self._host, self._port, self.timeout_s)
        replaced: http.client.HTTPConnection | None = None
        owner = threading.current_thread()
        with self._lock:
            replaced = self._conns.get(owner)
            self._conns[owner] = conn
            local.generation = self._generation
        if replaced is not None and replaced is not conn:
            self._close_connections([replaced])
        local.conn = conn
        local.used = False
        return conn, False

    def _discard(self, conn: http.client.HTTPConnection) -> None:
        """Close a connection and forget it, so the next call opens a new one."""
        if getattr(self._local, "conn", None) is conn:
            self._local.conn = None
            self._local.used = False
        with self._lock:
            owner = threading.current_thread()
            if self._conns.get(owner) is conn:
                del self._conns[owner]
        self._close_connections([conn])

    def _reap_dead_connections(self) -> None:
        """Close connections owned by threads that have already terminated."""
        with self._lock:
            stale = [owner for owner in self._conns if not owner.is_alive()]
            conns = [self._conns.pop(owner) for owner in stale]
        self._close_connections(conns)

    @staticmethod
    def _close_connections(conns: list[http.client.HTTPConnection]) -> None:
        for conn in conns:
            try:
                conn.close()
            except Exception:  # pragma: no cover - close never matters
                pass

    def _parse(
        self, payload: dict, questions: list[Question], latency_ms: float, secrets: tuple[str, ...]
    ) -> dict[str, Answer]:
        model = sanitize(str(payload.get("model") or self.model), secrets)
        usage = payload.get("usage")
        usage_ok = usage is None or isinstance(usage, dict)
        if not isinstance(usage, dict):
            usage = {}
        raw_in, raw_out = usage.get("input_tokens"), usage.get("output_tokens")
        in_tok, out_tok = _tokens(raw_in), _tokens(raw_out)
        usage_ok = usage_ok and all(v is None or _tokens(v, None) is not None for v in (raw_in, raw_out))
        total_cost, cost_known = _call_cost(
            usage, in_tok, out_tok, model, sanitize(self.model, secrets), tokens_ok=usage_ok, secrets=secrets
        )

        n = len(questions)
        in_shares = split_evenly(in_tok, n)
        out_shares = split_evenly(out_tok, n)
        answers_raw: dict = payload["answers"]
        out: dict[str, Answer] = {}
        for i, q in enumerate(questions):
            answer = _parse_answer(q, answers_raw.get(q.key))
            if answer.error is not None:
                answer.error = sanitize(answer.error, secrets)
            answer.latency_ms = latency_ms
            answer.input_tokens = in_shares[i]
            answer.output_tokens = out_shares[i]
            answer.cost_usd = total_cost / n
            answer.cost_known = cost_known
            answer.model = model
            out[q.key] = answer
        return out


def _parse_answer(q: Question, raw: object) -> Answer:
    """Turn one wire answer into an Answer. A missing or malformed answer
    becomes an errored Answer (value None) rather than an exception, so a
    cascade can re-ask just that question."""
    answer = Answer(key=q.key, kind=q.kind, value=None, source="jev")
    if not isinstance(raw, dict):
        answer.error = "jev returned no answer for this question"
        return answer
    try:
        if q.kind == "yesno":
            p = _probability(raw["noul"], "noul")
            answer.value = p >= 0.5
            answer.probabilities = {"true": p, "false": 1.0 - p}
            # (p_max - 1/2) / (1 - 1/2) with p_max = max(p, 1 - p).
            answer.confidence = abs(2.0 * p - 1.0)
            return answer
        raw_probs = raw.get("probabilities")
        allowed = q.allowed_values()
        if q.kind == "score":
            if not isinstance(raw_probs, dict):
                raise ValueError(f"probabilities must be an object, got {type(raw_probs).__name__}")
            probs = _score_probabilities(raw_probs, raw.get("legend"), allowed)
            value = max(probs, key=probs.__getitem__)
        else:
            value = raw["choice"]
        if value not in allowed:
            answer.error = f"jev answer has invalid {type(value).__name__}; not one of the allowed options"
            return answer
        if q.kind == "choice":
            if not isinstance(raw_probs, dict):
                raise ValueError(f"probabilities must be an object, got {type(raw_probs).__name__}")
            probs = _choice_probabilities(raw_probs, allowed)
        if probs[value] != max(probs.values()):
            raise ValueError("reported choice is not a most-probable option")
        conf = raw.get("confidence")
        confidence = _probability(conf, "confidence") if conf is not None else _normalized_confidence(
            probs[value], len(allowed)
        )
        answer.value = value
        answer.probabilities = probs
        answer.confidence = confidence
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        answer.value = None
        answer.probabilities = {}
        answer.confidence = None
        answer.error = f"jev answer is malformed: {e}"
    return answer


def _choice_probabilities(raw_probs: dict, allowed: list[str] | list[bool]) -> dict[str, float]:
    """Validate choice labels before retaining any provider-controlled text."""
    labels = set(allowed)
    unknown = sum(1 for label in raw_probs if not isinstance(label, str) or label not in labels)
    if unknown:
        raise ValueError(f"probabilities contain {unknown} unrecognized label(s)")
    probs = {label: _probability(probability, "probability") for label, probability in raw_probs.items()}
    return _validate_distribution(probs, labels)


def _score_probabilities(
    raw_probs: dict, raw_legend: object, allowed: list[str] | list[bool]
) -> dict[str, float]:
    """Map score indexes to declared levels without retaining provider labels."""
    levels = list(allowed)
    unknown = 0
    if raw_legend is not None:
        if not isinstance(raw_legend, dict):
            raise ValueError(f"legend must be an object, got {type(raw_legend).__name__}")
        for index, label in raw_legend.items():
            if _level_index(index, len(levels)) is None or label not in levels:
                unknown += 1

    probs: dict[str, float] = {}
    for index, probability in raw_probs.items():
        level_index = _level_index(index, len(levels))
        if level_index is None:
            unknown += 1
            continue
        level = levels[level_index]
        if level in probs:
            unknown += 1
            continue
        probs[level] = _probability(probability, "probability")
    if unknown:
        raise ValueError(f"score response contains {unknown} unrecognized label(s)")
    return _validate_distribution(probs, set(levels))


def _level_index(label: object, count: int) -> int | None:
    """A JSON score-map index, constrained to the question's level range."""
    if isinstance(label, bool):
        return None
    if isinstance(label, int):
        index = label
    elif isinstance(label, str) and label.isdecimal():
        index = int(label)
    else:
        return None
    return index if 0 <= index < count else None


def _validate_distribution(probs: dict[str, float], labels: set[object]) -> dict[str, float]:
    """Require a near-normalized distribution, including safe partial maps."""
    total = sum(probs.values())
    missing = labels - set(probs)
    if missing and abs(total - 1.0) > 1e-6:
        raise ValueError("incomplete probabilities must sum to 1")
    if abs(total - 1.0) > 0.02:
        raise ValueError("probabilities must sum to 1")
    return probs


def _probability(x: object, what: str) -> float:
    """A finite number in [0, 1]. Values within float noise of the bounds are
    clamped; anything else raises ValueError."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise ValueError(f"{what} must be a number, got {type(x).__name__}")
    p = float(x)
    if not math.isfinite(p) or p < -_PROB_TOLERANCE or p > 1.0 + _PROB_TOLERANCE:
        raise ValueError(f"{what} must be a finite number in [0, 1], got {type(x).__name__}")
    return min(1.0, max(0.0, p))


def _normalized_confidence(p_max: float | None, n: int) -> float | None:
    """Normalize a top probability against the uniform baseline for n options."""
    if p_max is None:
        return None
    if n == 1:
        return 1.0
    confidence = (p_max - 1.0 / n) / (1.0 - 1.0 / n)
    return min(1.0, max(0.0, confidence))


def _tokens(x: object, default: int | None = 0) -> int | None:
    """A token count from `usage`. Anything that is not a non-negative
    integer (an integral float is fine) gives `default`."""
    if isinstance(x, bool):
        return default
    if isinstance(x, int):
        return x if x >= 0 else default
    if isinstance(x, float) and math.isfinite(x) and x >= 0 and x.is_integer():
        return int(x)
    return default


def _call_cost(
    usage: dict,
    in_tok: int,
    out_tok: int,
    model: str,
    requested: str,
    tokens_ok: bool = True,
    secrets: tuple[str, ...] = (),
) -> tuple[float, bool]:
    """(cost, known). Prefer the provider-reported `usage.cost` (OpenRouter
    sends it), else price the tokens through fabri.pricing under the returned
    model id, then the requested id. Unpriced -> (0.0, False), with one
    warning per model id. When the token counts were malformed (`tokens_ok`
    False) and there is no valid reported cost, the cost is unknown too."""
    reported = usage.get("cost")
    if (
        isinstance(reported, (int, float))
        and not isinstance(reported, bool)
        and math.isfinite(reported)
        and reported >= 0
    ):
        return float(reported), True
    if not tokens_ok:
        return 0.0, False
    from fabri.pricing import cost_for

    for m in (model, requested):
        cost = cost_for(LLMUsage(input_tokens=in_tok, output_tokens=out_tok, model=m))
        if cost is not None:
            return cost, True
    if model not in _warned_unpriced:
        _warned_unpriced.add(model)
        logger.warning(
            "jev: cannot price model %r; its cost is unknown and recorded as 0.0",
            sanitize(model, secrets),
        )
    return 0.0, False


def _check_unique_keys(questions: list[Question]) -> None:
    seen: set[str] = set()
    for q in questions:
        if q.key in seen:
            raise ValueError(f"duplicate question key {q.key!r}")
        seen.add(q.key)
