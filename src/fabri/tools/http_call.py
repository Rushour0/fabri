"""Shared HTTP plumbing for dynamic tools (OpenAPI operations and single
``http`` endpoints).

Headers are declared as specs and resolved on every call, so a rotated secret
is picked up without a rebuild:

    headers:
      Authorization: {secret_ref: "billing:prod", prefix: "Bearer "}
      X-Api-Key: {env: SERVICE_API_KEY}
      Accept: application/json        # a plain string is a literal value

Header values are never logged and never appear in error messages.

Every response maps to fabri's tool result shape:

- 2xx: ``{ok: true, result: {status: "ok", code, data}}``
- 409: ``{ok: false, error: "conflict", result: {status: "conflict", code: 409, data}}``
- other 4xx and 5xx: ``{ok: false, error: "HTTP <code>", result: {status: "error", code, data}}``
- transport failure: ``{ok: false, error: ..., result: {status: "error", code: null}}``
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx

from fabri.tools.credential_store import CredentialStore, CredentialStoreError
from fabri.tools.result import ERROR_KEY, tool_error, tool_ok
from fabri.tools.secret_refs import resolve_secret

logger = logging.getLogger("fabri.tools.http")

# Response bodies larger than this are cut before they reach the model.
MAX_BODY_CHARS = 20_000
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class HeaderSpecError(ValueError):
    """A header spec is malformed. The message never holds a header value."""


def check_header_specs(specs: Mapping[str, Any] | None) -> None:
    """Validate header specs without resolving them."""
    for header, spec in (specs or {}).items():
        if isinstance(spec, str):
            continue
        if not isinstance(spec, Mapping):
            raise HeaderSpecError(f"header {header}: spec must be a string or a mapping")
        keys = {"secret_ref", "env", "value"} & set(spec)
        if len(keys) != 1:
            raise HeaderSpecError(
                f"header {header}: give exactly one of secret_ref, env or value"
            )
        if "env" in spec and not _ENV_NAME.fullmatch(str(spec["env"])):
            raise HeaderSpecError(f"header {header}: env must be an environment variable name")
        if "secret_ref" in spec and str(spec["secret_ref"]).count(":") != 1:
            raise HeaderSpecError(f"header {header}: secret_ref must look like provider:handle")
        unknown = set(spec) - {"secret_ref", "env", "value", "prefix"}
        if unknown:
            raise HeaderSpecError(f"header {header}: unknown keys {sorted(unknown)}")


def secret_ref_provider(specs: Mapping[str, Any] | None) -> str | None:
    """The provider (text before the colon) of the first header spec that
    names a ``secret_ref``, or None."""
    for spec in (specs or {}).values():
        if isinstance(spec, Mapping) and spec.get("secret_ref"):
            return str(spec["secret_ref"]).split(":", 1)[0]
    return None


def credential_refused_hint(provider: str) -> str:
    """The plain text shown when a vendor answers 401 to a stored credential."""
    return f"the stored {provider} credential was refused, run fabri connect {provider}"


def resolve_headers(
    specs: Mapping[str, Any] | None, store: CredentialStore | None = None
) -> dict[str, str]:
    """Resolve header specs to literal values. Raises HeaderSpecError with
    the header name only (never the value) when a source is missing."""
    out: dict[str, str] = {}
    for header, spec in (specs or {}).items():
        if isinstance(spec, str):
            out[header] = spec
            continue
        if not isinstance(spec, Mapping):
            raise HeaderSpecError(f"header {header}: spec must be a string or a mapping")
        prefix = str(spec.get("prefix") or "")
        if "secret_ref" in spec:
            try:
                value = resolve_secret(str(spec["secret_ref"]), store)
            except CredentialStoreError as exc:
                raise HeaderSpecError(f"header {header}: credential unavailable ({exc})") from None
        elif "env" in spec:
            value = os.environ.get(str(spec["env"]), "")
            if not value:
                raise HeaderSpecError(
                    f"header {header}: environment variable {spec['env']} is not set"
                )
        elif "value" in spec:
            value = str(spec["value"])
        else:
            raise HeaderSpecError(f"header {header}: give secret_ref, env or value")
        out[header] = prefix + value
    return out


def dry_run_headers(spec: Mapping[str, Any] | str | None) -> dict[str, str]:
    """``dry_run_header: X-Dry-Run`` or ``{name: X-Dry-Run, value: "1"}``."""
    if not spec:
        return {}
    if isinstance(spec, str):
        return {spec: "true"}
    return {str(spec["name"]): str(spec.get("value", "true"))}


class PathParamError(ValueError):
    """A path parameter value could leave the path segment it fills."""


def render_path_value(name: str, value: Any) -> str:
    """Encode one path parameter value for its URL segment.

    The value is refused when it is empty, ``.`` or ``..``, or when it holds
    ``/`` or ``\\`` or decodes (one or more rounds of percent decoding) to
    any of those. Such a value could turn ``/lists/{id}`` into another path
    after the client or the server normalizes the URL, which would dodge the
    approval floor that was checked against the template."""
    text = str(value)
    seen = text
    for _ in range(5):
        if seen in ("", ".", "..") or "/" in seen or "\\" in seen:
            raise PathParamError(f"path parameter {name!r} is not allowed (empty, a dot segment or holds a separator)")
        decoded = unquote(seen)
        if decoded == seen:
            break
        seen = decoded
    else:
        raise PathParamError(f"path parameter {name!r} is encoded too many times")
    return quote(text, safe="")


def _has_dot_segment(url: str) -> bool:
    try:
        path = urlsplit(url).path
    except ValueError:
        return True
    return any(seg in (".", "..") for seg in path.split("/"))


def _floor_drift(floor_url: str, method: str, url: str) -> bool:
    """True when the rendered request falls under a stricter catalog floor
    than the template it was built from. Approval was settled at build time
    from the template, so a call whose real target the floor would not have
    allowed must not be sent."""
    from fabri.tools.connectors import request_floor

    built_owner, built_floor = request_floor(floor_url, method)
    if built_owner is None or built_floor is not None:
        # No connector owns the template host, or the tool already needs approval.
        return False
    try:
        final = str(httpx.URL(url))
    except (httpx.InvalidURL, ValueError):
        return True
    owner, floor = request_floor(final, method)
    return owner != built_owner or floor is not None


def _body(resp: httpx.Response) -> Any:
    ctype = resp.headers.get("content-type", "")
    if "json" in ctype:
        try:
            return resp.json()
        except ValueError:
            pass
    text = resp.text
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS] + f"... [truncated {len(text) - MAX_BODY_CHARS} chars]"
    return text or None


def map_response(resp: httpx.Response) -> dict:
    code = resp.status_code
    data = _body(resp)
    if 200 <= code < 300:
        return tool_ok({"status": "ok", "code": code, "data": data})
    if code == 409:
        return tool_error("conflict", result={"status": "conflict", "code": 409, "data": data})
    return tool_error(f"HTTP {code}", result={"status": "error", "code": code, "data": data})


def prepare(
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    json_body: Any = None,
    form_body: Mapping[str, Any] | None = None,
    client: httpx.Client | None = None,
) -> httpx.Request:
    """Build the request ``send`` would send, with the query merged and the
    body encoded, but with no header specs resolved yet.

    Params whose value is None are dropped, and a JSON body wins over a form
    body, as in ``send``. httpx normalizes the URL here, so ``/a/../b``
    becomes ``/b``; callers that render URLs themselves must run their own
    dot segment check on the raw text first (``send`` does).

    With `client`, the request is built by ``client.build_request`` and so
    carries the client's default headers and cookies, which is what ``send``
    uses. Without it, the request holds only the headers the body needs;
    ``send_prepared`` adds the client's default headers later but never its
    cookies."""
    kwargs: dict[str, Any] = {}
    if params:
        kwargs["params"] = {k: v for k, v in params.items() if v is not None}
    if json_body is not None:
        kwargs["json"] = json_body
    elif form_body is not None:
        kwargs["data"] = dict(form_body)
    if client is not None:
        return client.build_request(method.upper(), url, **kwargs)
    return httpx.Request(method.upper(), url, **kwargs)


def _hash_part(digest: Any, data: bytes) -> None:
    # Length prefix each part so two different splits can never hash the same.
    digest.update(str(len(data)).encode("ascii") + b":" + data)


def request_hash(request: httpx.Request, extra_headers: Mapping[str, str] | None = None) -> str:
    """SHA-256 hex over what a prepared request will send: the method, the
    full URL as text, the extra headers (lower-cased names, sorted, one
    ``name:value`` line each, with the name length prefixed as well so a
    colon inside a name or value cannot shift the split) and the body
    bytes.

    The hash binds the exact request, not a normal form of it. Two param
    mappings with the same pairs in a different order give different URLs
    (httpx keeps insertion order) and so different hashes. Resolved
    credential headers are not part of the hash, since they are added after
    approval. The value is the same in every process."""
    digest = hashlib.sha256()
    _hash_part(digest, request.method.encode("ascii"))
    _hash_part(digest, str(request.url).encode("utf-8"))
    pairs = sorted((str(k).lower(), str(v)) for k, v in (extra_headers or {}).items())
    _hash_part(digest, str(len(pairs)).encode("ascii"))
    for name, value in pairs:
        _hash_part(digest, f"{name}:{value}\n".encode("utf-8"))
        _hash_part(digest, name.encode("utf-8"))
    _hash_part(digest, request.content)
    return digest.hexdigest()


# Extra headers a caller may never set: hop-by-hop and framing headers, and
# headers that carry a credential. Credentials only come from header specs.
HOP_BY_HOP_AND_AUTH = frozenset({
    "host", "content-length", "transfer-encoding", "connection", "upgrade", "te", "trailer", "expect",
    "proxy-authorization", "authorization", "cookie", "x-api-key", "api-key", "x-auth-token",
})
MAX_EXTRA_HEADERS = 32
MAX_EXTRA_HEADER_BYTES = 8192
_HEADER_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def check_extra_headers(
    headers: Mapping[str, str] | None, *, declared: Iterable[str] = ()
) -> dict[str, str]:
    """Validate literal extra headers and return a copy.

    Refused, with a HeaderSpecError that names the header but never shows
    its value: more than MAX_EXTRA_HEADERS headers, a name outside the HTTP
    token grammar, a value that is not text or holds a control character
    other than tab (CR, LF and NUL included), a name in HOP_BY_HOP_AND_AUTH
    or in `declared` (the header names the bound source sets from its specs),
    the same name twice in different case, and a total size of names plus
    values over MAX_EXTRA_HEADER_BYTES. Names are compared lower-cased; the
    copy keeps the caller's case."""
    if headers is None:
        return {}
    if not isinstance(headers, Mapping):
        raise HeaderSpecError("extra headers must be a mapping of names to values")
    if len(headers) > MAX_EXTRA_HEADERS:
        raise HeaderSpecError(f"too many extra headers (at most {MAX_EXTRA_HEADERS})")
    denied = {str(name).lower() for name in declared}
    seen: set[str] = set()
    out: dict[str, str] = {}
    total = 0
    for name, value in headers.items():
        if not isinstance(name, str) or not _HEADER_TOKEN.fullmatch(name):
            shown = name if isinstance(name, str) else type(name).__name__
            raise HeaderSpecError(f"extra header {shown[:64]!r}: not a valid header name")
        lower = name.lower()
        if lower in HOP_BY_HOP_AND_AUTH:
            raise HeaderSpecError(f"extra header {name}: this header cannot be set by a tool call")
        if lower in denied:
            raise HeaderSpecError(f"extra header {name}: the source already sets this header")
        if lower in seen:
            raise HeaderSpecError(f"extra header {name}: given more than once")
        if not isinstance(value, str):
            raise HeaderSpecError(f"extra header {name}: value must be text")
        if any((ord(ch) < 0x20 and ch != "\t") or ord(ch) == 0x7F for ch in value):
            raise HeaderSpecError(f"extra header {name}: value holds a control character")
        total += len(name.encode("utf-8")) + len(value.encode("utf-8"))
        if total > MAX_EXTRA_HEADER_BYTES:
            raise HeaderSpecError(f"extra headers are larger than {MAX_EXTRA_HEADER_BYTES} bytes")
        seen.add(lower)
        out[name] = value
    return out


def _dot_segment_refusal() -> dict:
    return tool_error("request refused: the URL path holds a dot segment",
                      result={"status": "error", "code": None})


def _floor_refusal() -> dict:
    return tool_error("request refused: the rendered path is outside what this tool was approved for",
                      result={"status": "error", "code": None})


def _has_encoded_dot_segment(raw_path: bytes) -> bool:
    """True when a path segment is ``.`` or ``..`` once percent decoded.
    httpx removes literal dot segments while it parses a URL but keeps
    encoded ones such as ``%2e%2e``, which a server may decode and follow."""
    try:
        path = raw_path.decode("ascii").split("?", 1)[0]
    except UnicodeDecodeError:
        return True
    for seg in path.split("/"):
        seen = seg
        for _ in range(5):
            if seen in (".", ".."):
                return True
            decoded = unquote(seen)
            if decoded == seen:
                break
            seen = decoded
        else:
            return True
    return False


def send_prepared(
    client: httpx.Client,
    request: httpx.Request,
    *,
    headers: Mapping[str, Any] | None = None,
    store: CredentialStore | None = None,
    extra_headers: Mapping[str, str] | None = None,
    timeout_s: float = 30.0,
    tool_name: str = "",
    floor_url: str | None = None,
) -> dict:
    """Send a request built by ``prepare`` and map the response, with the
    same checks as ``send``: a dot segment in the path (here, one that is
    percent encoded, since httpx already removed literal ones) and, when
    `floor_url` is given, a rendered target the catalog floor did not allow
    for that template are refused before anything is resolved.

    Header specs are resolved on this call, then `extra_headers` are set on
    top (they are not checked here; callers that take them from outside run
    ``check_extra_headers`` first). The client's default headers fill any
    name still missing. With ``headers=None`` no credential is sent.

    The request passed in is not changed: the resolved values go on a new
    request object, so a caller that keeps the prepared request for a hash
    or an approval card never holds a credential. Every resolved value is
    stripped from the result before it is returned."""
    if _has_encoded_dot_segment(request.url.raw_path):
        return _dot_segment_refusal()
    if floor_url is not None and _floor_drift(floor_url, request.method, str(request.url)):
        return _floor_refusal()
    try:
        resolved = resolve_headers(headers, store)
    except HeaderSpecError as exc:
        return tool_error(str(exc), result={"status": "error", "code": None})
    resolved.update(extra_headers or {})
    final_headers = httpx.Headers(request.headers)
    for name, value in resolved.items():
        final_headers[name] = value
    for name, value in client.headers.items():
        if name not in final_headers:
            final_headers[name] = value
    extensions = dict(request.extensions)
    extensions["timeout"] = httpx.Timeout(timeout_s).as_dict()
    outgoing = httpx.Request(request.method, request.url, headers=final_headers,
                             content=request.content, extensions=extensions)
    method = request.method
    try:
        resp = client.send(outgoing)
    except httpx.HTTPError as exc:
        logger.info("tool %s: %s request failed: %s", tool_name, method, type(exc).__name__)
        return tool_error(
            f"request failed: {type(exc).__name__}", result={"status": "error", "code": None}
        )
    logger.debug("tool %s: %s %s -> %s", tool_name, method, resp.request.url.path, resp.status_code)
    out = map_response(resp)
    # An upstream body can echo request headers back (some 401 pages do).
    # Strip every resolved header value before the model sees the result.
    if resolved:
        from fabri.tools.mcp_client import _redact_tree, _secret_pieces
        pieces = _secret_pieces([v for v in resolved.values() if isinstance(v, str)])
        if pieces:
            out = _redact_tree(out, pieces)
    if resp.status_code == 401:
        provider = secret_ref_provider(headers)
        if provider:
            out[ERROR_KEY] = f"HTTP 401; {credential_refused_hint(provider)}"
    return out


def send(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    headers: Mapping[str, Any] | None = None,
    store: CredentialStore | None = None,
    extra_headers: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    json_body: Any = None,
    form_body: Mapping[str, Any] | None = None,
    timeout_s: float = 30.0,
    tool_name: str = "",
    floor_url: str | None = None,
) -> dict:
    """Make one request and map the response. Logs method, URL path and
    status only.

    A URL with a ``.`` or ``..`` path segment is refused, since the client
    would drop it and send to another path. When `floor_url` (the template
    URL the tool was built from) is given, a call whose rendered target is
    not one the catalog floor allowed for that template is refused too.

    This is ``prepare`` followed by ``send_prepared``."""
    if _has_dot_segment(url):
        return _dot_segment_refusal()
    try:
        request = prepare(method, url, params=params, json_body=json_body, form_body=form_body,
                          client=client)
    except Exception as exc:
        # The request could not be built (a bad URL or a body httpx cannot
        # encode). Report it the way send did before the split: the floor
        # check and header resolution came first, then the build error.
        if floor_url is not None and _floor_drift(floor_url, method, url):
            return _floor_refusal()
        try:
            resolve_headers(headers, store)
        except HeaderSpecError as spec_exc:
            return tool_error(str(spec_exc), result={"status": "error", "code": None})
        if isinstance(exc, httpx.HTTPError):
            logger.info("tool %s: %s request failed: %s", tool_name, method.upper(), type(exc).__name__)
            return tool_error(
                f"request failed: {type(exc).__name__}", result={"status": "error", "code": None}
            )
        raise
    return send_prepared(client, request, headers=headers, store=store, extra_headers=extra_headers,
                         timeout_s=timeout_s, tool_name=tool_name, floor_url=floor_url)
