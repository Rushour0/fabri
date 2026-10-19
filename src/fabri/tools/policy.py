"""Caller-supplied limits on dynamic tools.

A host builds one ToolPolicy and passes it to the setup resolver and to tool
registration. Layers and registered tools can narrow what the policy allows,
never widen it.

    allow: [pet_*, owner_list]       # tool names or fnmatch patterns; omit = no name cap
    allowed_kinds: [openapi, http]   # source kinds a layer may declare
    allowed_hosts: ["*.example.com"] # hosts for http, openapi and mcp urls; omit = any
    allow_delete: false              # DELETE operations and http tools
    python_modules: [myapp.tools]    # module prefixes for python callables
    dry_run_header: {name: X-Dry-Run, value: "1"}

A policy file can also be a bare list of names, which sets `allow` only.
"""
from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

SOURCE_KINDS: tuple[str, ...] = ("openapi", "mcp", "http", "python")
_POLICY_KEYS = {"allow", "allowed_kinds", "allowed_hosts", "allow_delete", "python_modules", "dry_run_header"}


class PolicyError(ValueError):
    """A tool or declaration falls outside the caller's policy."""


@dataclass(frozen=True)
class ToolPolicy:
    allow: tuple[str, ...] | None = None
    allowed_kinds: tuple[str, ...] = ("openapi", "http")
    allowed_hosts: tuple[str, ...] | None = None
    allow_delete: bool = False
    # None = any module, () = no python callables at all.
    python_modules: tuple[str, ...] | None = ()
    dry_run_header: Mapping[str, Any] | str | None = field(default=None, hash=False, compare=False)

    @classmethod
    def permissive(cls, allow: Iterable[str] | None = None) -> "ToolPolicy":
        """Every kind, any host, any module, DELETE left to the declaration.
        Suits layers the host itself authors."""
        return cls(allow=tuple(allow) if allow is not None else None, allowed_kinds=SOURCE_KINDS,
                   allowed_hosts=None, allow_delete=True, python_modules=None)

    @classmethod
    def from_data(cls, data: Any, base: "ToolPolicy | None" = None) -> "ToolPolicy":
        base = base or cls()
        if isinstance(data, str):
            data = [line.strip() for line in data.splitlines() if line.strip() and not line.startswith("#")]
        if isinstance(data, list):
            return replace(base, allow=tuple(str(x) for x in data))
        if not isinstance(data, Mapping):
            raise PolicyError("a policy must be a list of tool names or a mapping")
        unknown = set(data) - _POLICY_KEYS
        if unknown:
            raise PolicyError(f"unknown policy keys: {sorted(unknown)}")
        kinds = data.get("allowed_kinds", base.allowed_kinds)
        bad = set(kinds or ()) - set(SOURCE_KINDS)
        if bad:
            raise PolicyError(f"unknown kinds in allowed_kinds: {sorted(bad)}")

        def tup(key: str, default: Any) -> Any:
            if key not in data:
                return default
            value = data[key]
            return None if value is None else tuple(str(x) for x in value)

        return cls(
            allow=tup("allow", base.allow),
            allowed_kinds=tuple(kinds or ()),
            allowed_hosts=tup("allowed_hosts", base.allowed_hosts),
            allow_delete=bool(data.get("allow_delete", base.allow_delete)),
            python_modules=tup("python_modules", base.python_modules),
            dry_run_header=data.get("dry_run_header", base.dry_run_header),
        )

    @classmethod
    def from_file(cls, path: str | Path, base: "ToolPolicy | None" = None) -> "ToolPolicy":
        text = Path(path).read_text(encoding="utf-8")
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise PolicyError(f"policy file {path} is not valid YAML: {exc}") from exc
        if data is None:
            data = []
        return cls.from_data(data, base)

    def with_allow(self, allow: Iterable[str] | None) -> "ToolPolicy":
        return replace(self, allow=tuple(allow) if allow is not None else None)

    # -- checks ---------------------------------------------------------

    def name_allowed(self, name: str, aliases: Iterable[str] = ()) -> bool:
        if self.allow is None:
            return True
        return any(fnmatch.fnmatchcase(n, p) for n in (name, *aliases) for p in self.allow)

    def kind_allowed(self, kind: str) -> bool:
        return kind in self.allowed_kinds

    def host_allowed(self, url: str | None) -> bool:
        if self.allowed_hosts is None or not url:
            return True
        host = (urlparse(url).hostname or "").lower()
        return bool(host) and any(fnmatch.fnmatchcase(host, p.lower()) for p in self.allowed_hosts)

    def module_allowed(self, dotted: str) -> bool:
        if self.python_modules is None:
            return True
        module = dotted.replace(":", ".")
        return any(module == p or module.startswith(p.rstrip(".") + ".") for p in self.python_modules)

    def check_kind(self, kind: str) -> None:
        if not self.kind_allowed(kind):
            raise PolicyError(f"source kind {kind!r} is not allowed by the policy")

    def check_host(self, url: str | None, what: str = "url") -> None:
        if not self.host_allowed(url):
            host = urlparse(url or "").hostname or "?"
            raise PolicyError(f"{what} host {host!r} is not in the policy's allowed_hosts")
