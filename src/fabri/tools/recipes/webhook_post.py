"""Recipe: SSRF-hardened JSON POSTs to external webhooks.

Drop next to its manifest in your tools/agent_tools/ directory.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from collections.abc import Mapping
from pathlib import Path

# This recipe is launched as a plain script by its JSON manifest. In a source
# checkout, add the src/ root so direct execution can import shared helpers.
if not __package__:
    for _candidate in Path(__file__).resolve().parents:
        if (_candidate / "fabri" / "tools" / "security" / "ssrf.py").is_file():
            sys.path.insert(0, str(_candidate))
            break

from fabri.tools.secret_refs import resolve_secret
from fabri.tools.security.ssrf import ValidatingRedirect, validate_url

BODY_CAP_BYTES = 64 * 1024
DEFAULT_TIMEOUT_S = 15.0
MAX_TIMEOUT_S = 60.0
RESPONSE_HEADERS = frozenset(
    {
        "content-length",
        "content-type",
        "date",
        "etag",
        "location",
        "retry-after",
        "x-request-id",
    }
)

_ValidatingRedirect = ValidatingRedirect
_opener = urllib.request.build_opener(_ValidatingRedirect)


def _timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout_s must be a positive number")
    if value <= 0:
        raise ValueError("timeout_s must be a positive number")
    return min(float(value), MAX_TIMEOUT_S)


def _headers(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("headers must be an object with string values")

    result: dict[str, str] = {}
    for name, header_value in value.items():
        if not isinstance(name, str) or not isinstance(header_value, str):
            raise ValueError("headers must be an object with string values")
        result[name] = header_value
    return result


def _response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        name.lower(): value
        for name, value in headers.items()
        if name.lower() in RESPONSE_HEADERS
    }


def _redacted_error(error: Exception, secret: str | None) -> str:
    message = str(error)
    if secret:
        message = message.replace(secret, "[REDACTED]")
    return message or error.__class__.__name__


def main() -> int:
    secret: str | None = None
    try:
        args = json.loads(sys.stdin.read())
        if not isinstance(args, dict):
            raise ValueError("input must be a JSON object")
        if "url" not in args:
            raise ValueError("url is required")
        if "payload" not in args:
            raise ValueError("payload is required")
        if not isinstance(args["url"], str):
            raise ValueError("url must be a string")

        url = validate_url(args["url"])
        timeout_s = _timeout(args.get("timeout_s", DEFAULT_TIMEOUT_S))
        headers = _headers(args.get("headers"))
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("User-Agent", "fabri-recipe/0.1")

        auth = args.get("auth")
        if auth is not None:
            if not isinstance(auth, str):
                raise ValueError("auth must be a provider:handle reference")
            secret = resolve_secret(auth)
            headers = {
                name: value
                for name, value in headers.items()
                if name.lower() != "authorization"
            }
            headers["Authorization"] = f"Bearer {secret}"

        body = json.dumps(
            args["payload"], allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method="POST",
        )

        with _opener.open(request, timeout=timeout_s) as response:
            raw_body = response.read(BODY_CAP_BYTES + 1)
            truncated = len(raw_body) > BODY_CAP_BYTES
            response_body = raw_body[:BODY_CAP_BYTES].decode(
                "utf-8", errors="replace"
            )
            result = {
                "status": response.status,
                "body": response_body,
                "truncated": truncated,
                "headers": _response_headers(response.headers),
            }
    # This is the subprocess boundary: every ordinary input, credential, and
    # HTTP failure must remain machine-readable instead of emitting a traceback.
    except Exception as error:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": _redacted_error(error, secret),
                }
            )
        )
        return 1

    print(json.dumps({"ok": True, "result": result}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
