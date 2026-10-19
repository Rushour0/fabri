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
closes every connection the decider opened.
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
from fabri.decide.types import Answer, DecideError, Question, split_evenly

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
        # One keep-alive connection per thread. `_conns` tracks every live one
        # so close() can reach connections owned by other threads; `_generation`
        # tells a thread its cached connection was closed under it.
        self._local = threading.local()
        self._lock = threading.Lock()
        self._conns: set[http.client.HTTPConnection] = set()
        self._generation = 0

    @property
    def url(self) -> str:
        return f"{self.base_url}/systemone"

    def close(self) -> None:
        """Close every pooled connection. The decider stays usable: the next
        call opens a fresh connection."""
        with self._lock:
            conns, self._conns = self._conns, set()
            self._generation += 1
        for conn in conns:
            try:
                conn.close()
            except Exception:  # pragma: no cover - close never matters
                pass

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
            return self._parse(payload, questions, latency_ms)
        except DecideError:
            raise
        except Exception as e:  # never let a surprising payload escape as a bare exception
            raise DecideError(f"jev: could not parse response: {type(e).__name__}: {e}") from None

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
                    f"jev: request to {self.url} failed: {_redact(text, key)}"
                ) from None
            if 200 <= status < 300:
                break
            if attempt == 1 and (status == 429 or status >= 500):
                time.sleep(self.retry_backoff_s)
                continue
            snippet = _redact(raw[:_BODY_SNIPPET].decode("utf-8", errors="replace"), key)
            hint = " (check the API key)" if status in (401, 403) else ""
            raise DecideError(f"jev: HTTP {status} from {self.url}{hint}: {snippet}") from None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise DecideError(f"jev: response is not valid JSON: {e}") from None
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
        local = self._local
        conn = getattr(local, "conn", None)
        if conn is not None and getattr(local, "generation", None) == self._generation:
            return conn, bool(getattr(local, "used", False))
        conn = _open_connection(self._scheme, self._host, self._port, self.timeout_s)
        with self._lock:
            self._conns.add(conn)
            local.generation = self._generation
        local.conn = conn
        local.used = False
        return conn, False

    def _discard(self, conn: http.client.HTTPConnection) -> None:
        """Close a connection and forget it, so the next call opens a new one."""
        if getattr(self._local, "conn", None) is conn:
            self._local.conn = None
            self._local.used = False
        with self._lock:
            self._conns.discard(conn)
        try:
            conn.close()
        except Exception:  # pragma: no cover - close never matters
            pass

    def _parse(self, payload: dict, questions: list[Question], latency_ms: float) -> dict[str, Answer]:
        model = str(payload.get("model") or self.model)
        usage = payload.get("usage")
        usage_ok = usage is None or isinstance(usage, dict)
        if not isinstance(usage, dict):
            usage = {}
        raw_in, raw_out = usage.get("input_tokens"), usage.get("output_tokens")
        in_tok, out_tok = _tokens(raw_in), _tokens(raw_out)
        usage_ok = usage_ok and all(v is None or _tokens(v, None) is not None for v in (raw_in, raw_out))
        total_cost, cost_known = _call_cost(usage, in_tok, out_tok, model, self.model, tokens_ok=usage_ok)

        n = len(questions)
        in_shares = split_evenly(in_tok, n)
        out_shares = split_evenly(out_tok, n)
        answers_raw: dict = payload["answers"]
        out: dict[str, Answer] = {}
        for i, q in enumerate(questions):
            answer = _parse_answer(q, answers_raw.get(q.key))
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
            p = _probability(raw["noul"], "noul probability")
            answer.value = p >= 0.5
            answer.probabilities = {"true": p, "false": 1.0 - p}
            # (p_max - 1/2) / (1 - 1/2) with p_max = max(p, 1 - p).
            answer.confidence = abs(2.0 * p - 1.0)
            return answer
        raw_probs = raw.get("probabilities")
        if raw_probs is None:
            raw_probs = {}
        if not isinstance(raw_probs, dict):
            raise ValueError(f"probabilities must be an object, got {type(raw_probs).__name__}")
        probs = {str(k): _probability(v, f"probability for {k!r}") for k, v in raw_probs.items()}
        if q.kind == "score":
            # Live Jev returns `score` as an expected level index (e.g. 2.96) with
            # `legend` {"0": "low", ...} and probabilities keyed by index. Report
            # the most likely level by name so it lines up with `confidence`.
            legend = {str(k): str(v) for k, v in (raw.get("legend") or {}).items()}
            if legend and probs:
                probs = {legend.get(k, k): p for k, p in probs.items()}
                value = max(probs, key=probs.__getitem__)
            else:
                value = raw["score"]
        else:
            value = raw["choice"]
        allowed = q.allowed_values()
        if value not in allowed:
            answer.error = f"jev answered {value!r}, which is not one of {allowed!r}"
            return answer
        conf = raw.get("confidence")
        confidence = _probability(conf, "confidence") if conf is not None else probs.get(value)
        answer.value = value
        answer.probabilities = probs
        answer.confidence = confidence
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        answer.value = None
        answer.probabilities = {}
        answer.confidence = None
        answer.error = f"jev answer is malformed: {e!r}"
    return answer


def _probability(x: object, what: str) -> float:
    """A finite number in [0, 1]. Values within float noise of the bounds are
    clamped; anything else raises ValueError."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise ValueError(f"{what} must be a number, got {x!r}")
    p = float(x)
    if not math.isfinite(p) or p < -_PROB_TOLERANCE or p > 1.0 + _PROB_TOLERANCE:
        raise ValueError(f"{what} must be a finite number in [0, 1], got {x!r}")
    return min(1.0, max(0.0, p))


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
    usage: dict, in_tok: int, out_tok: int, model: str, requested: str, tokens_ok: bool = True
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
        logger.warning("jev: cannot price model %r; its cost is unknown and recorded as 0.0", model)
    return 0.0, False


def _check_unique_keys(questions: list[Question]) -> None:
    seen: set[str] = set()
    for q in questions:
        if q.key in seen:
            raise ValueError(f"duplicate question key {q.key!r}")
        seen.add(q.key)


def _redact(text: str, key: str) -> str:
    return text.replace(key, "***") if key else text
