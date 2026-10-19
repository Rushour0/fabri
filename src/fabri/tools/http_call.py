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

import logging
import os
import re
from collections.abc import Mapping
from typing import Any

import httpx

from fabri.tools.credential_store import CredentialStore, CredentialStoreError
from fabri.tools.result import tool_error, tool_ok
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
) -> dict:
    """Make one request and map the response. Logs method, URL path and
    status only."""
    try:
        resolved = resolve_headers(headers, store)
    except HeaderSpecError as exc:
        return tool_error(str(exc), result={"status": "error", "code": None})
    resolved.update(extra_headers or {})
    kwargs: dict[str, Any] = {"headers": resolved, "timeout": timeout_s}
    if params:
        kwargs["params"] = {k: v for k, v in params.items() if v is not None}
    if json_body is not None:
        kwargs["json"] = json_body
    elif form_body is not None:
        kwargs["data"] = dict(form_body)
    try:
        resp = client.request(method.upper(), url, **kwargs)
    except httpx.HTTPError as exc:
        logger.info("tool %s: %s request failed: %s", tool_name, method.upper(), type(exc).__name__)
        return tool_error(
            f"request failed: {type(exc).__name__}", result={"status": "error", "code": None}
        )
    logger.debug("tool %s: %s %s -> %s", tool_name, method.upper(), resp.request.url.path, resp.status_code)
    return map_response(resp)
