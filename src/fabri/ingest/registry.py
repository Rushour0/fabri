"""The adapter registry — an open dict (third parties extend it) with a single
dispatch point, mirroring the spirit of ``core.llm.Provider`` but not a closed
enum. Adapters register three ways:

  1. ``@fabri.adapter("name")`` decorator (in-process, this session),
  2. ``register_adapter(name, obj)`` (programmatic, e.g. config-declared),
  3. setuptools ``entry_points`` group ``fabri.adapters`` — a pip-installed
     package's adapters appear automatically (like pytest/flake8 plugins).

``resolve_adapter("auto", source)`` sniffs the first lines of a source to pick a
built-in when the caller didn't name one.
"""
from __future__ import annotations

import json
from typing import Callable, Iterator

from fabri.core.logging_setup import get_logger
from fabri.ingest.adapters.base import Adapter, Session

logger = get_logger()

_ADAPTERS: dict[str, Adapter] = {}
_ENTRY_POINTS_LOADED = False


class UnknownAdapterError(KeyError):
    def __init__(self, name: str, available: list[str]):
        self.name = name
        self.available = available
        super().__init__(
            f"unknown ingest adapter {name!r}; available: {', '.join(available) or '(none)'}"
        )


class _FnAdapter:
    """Wraps a plain ``callable(source, options) -> Iterator[Session]`` as an
    Adapter so a decorated function satisfies the protocol without ceremony."""

    def __init__(self, name: str, fn: Callable):
        self.name = name
        self._fn = fn

    def sessions(self, source, options: dict) -> Iterator[Session]:
        return self._fn(source, options)


def _coerce_to_adapter(name: str, obj) -> Adapter:
    if isinstance(obj, Adapter) and not isinstance(obj, type):
        return obj
    if isinstance(obj, type):  # an Adapter class → instantiate
        inst = obj()
        if not hasattr(inst, "name"):
            inst.name = name  # type: ignore[attr-defined]
        return inst
    if callable(obj):
        return _FnAdapter(name, obj)
    raise TypeError(f"adapter {name!r} must be an Adapter, class, or callable (got {type(obj).__name__})")


def register_adapter(name: str, obj) -> None:
    _ADAPTERS[name.lower()] = _coerce_to_adapter(name, obj)


def adapter(name: str) -> Callable:
    """The ``@fabri.adapter("name")`` decorator. Works on a function
    ``(source, options) -> Iterator[Session]`` or an Adapter class."""

    def deco(obj):
        register_adapter(name, obj)
        return obj

    return deco


def _ensure_entry_points_loaded() -> None:
    global _ENTRY_POINTS_LOADED
    if _ENTRY_POINTS_LOADED:
        return
    _ENTRY_POINTS_LOADED = True  # set first so a failing load never retries in a loop
    try:
        from importlib.metadata import entry_points

        eps = entry_points(group="fabri.adapters")
    except Exception as e:  # pragma: no cover - importlib shape varies by py version
        logger.debug("ingest: entry-point discovery unavailable: %s", e)
        return
    for ep in eps:
        if ep.name.lower() in _ADAPTERS:
            continue  # a decorator/config registration wins over an entry point
        try:
            register_adapter(ep.name, ep.load())
        except Exception as e:
            # Never let one bad third-party plugin abort ingestion — log & skip,
            # exactly as runtime does for a failed MCP server connection.
            logger.warning("ingest: adapter entry-point %r failed to load (skipping): %s", ep.name, e)


def get_adapter(name: str, *, load_plugins: bool = True) -> Adapter:
    key = (name or "").lower()
    if key not in _ADAPTERS and load_plugins:
        _ensure_entry_points_loaded()
    if key not in _ADAPTERS:
        raise UnknownAdapterError(name, list_adapters())
    return _ADAPTERS[key]


def list_adapters(*, load_plugins: bool = True) -> list[str]:
    if load_plugins:
        _ensure_entry_points_loaded()
    return sorted(_ADAPTERS)


def resolve_adapter(spec, source=None, *, load_plugins: bool = True) -> Adapter:
    """``spec`` is an adapter name, an Adapter instance, or ``"auto"``. Auto
    peeks the source and picks a built-in by shape."""
    if isinstance(spec, Adapter) and not isinstance(spec, (str, type)):
        return spec
    if isinstance(spec, str) and spec != "auto":
        return get_adapter(spec, load_plugins=load_plugins)
    sniffed = _sniff(source) if source is not None else "regex"
    logger.info("ingest: auto-detected adapter %r for %s", sniffed, getattr(source, "name", "?"))
    return get_adapter(sniffed, load_plugins=load_plugins)


def _sniff(source) -> str:
    """Heuristic format detection from the first few lines. Native fabri JSONL
    (objects with a ``type`` field) → ``jsonl``; OTel/OpenAI-ish JSON → ``otel``;
    otherwise plaintext → ``regex``."""
    try:
        preview = source.peek(5)
    except Exception:
        return "regex"
    saw_json = False
    for item in preview:
        obj = item if isinstance(item, dict) else _try_json(item)
        if obj is None:
            continue
        saw_json = True
        if "type" in obj and isinstance(obj.get("type"), str):
            return "jsonl"
        if any(k in obj for k in ("resource_spans", "spans", "trace_id", "choices", "response")):
            return "otel"
    return "otel" if saw_json else "regex"


def _try_json(item):
    if isinstance(item, (bytes, bytearray)):
        item = item.decode("utf-8", "replace")
    if not isinstance(item, str):
        return None
    try:
        obj = json.loads(item.strip())
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None
