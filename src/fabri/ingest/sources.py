"""``LogSource`` — a uniform read surface over every place a log can come from:
a file, a directory of files, stdin, or an in-process iterator of lines/records.

Adapters never touch files or stdin directly; they pull ``source.lines()`` or
``source.records()`` (and ``source.group_by(...)`` for record-shaped logs).
Batch and streaming are the same object — a file yields all its lines, a live
``tail -f`` on stdin yields them as they arrive.
"""
from __future__ import annotations

import json
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Iterable, Iterator

from fabri.core.logging_setup import get_logger

logger = get_logger()

# Files a directory expansion will pick up (adapters decide how to parse them).
_LOG_GLOBS = ("*.jsonl", "*.log", "*.json", "*.txt", "*.ndjson")


class LogSource:
    """Wraps a raw source and exposes it as lines or JSON records. Malformed
    JSON on the ``records()`` path is skipped and counted in ``skipped`` (same
    tolerance as ``read_trace``), never fatal."""

    def __init__(self, raw: Iterable, *, name: str = "log"):
        self._raw = raw
        self.name = name
        self.skipped = 0
        self._buffer: list = []  # items pushed back by peek()

    # -- construction ------------------------------------------------------
    @classmethod
    def from_any(cls, src) -> "LogSource":
        """Coerce anything reasonable into a LogSource:
        ``"-"`` → stdin; a str/Path file → its lines; a str/Path dir is NOT
        handled here (use ``iter_sources``); an iterable of str/dict → itself;
        an open text file object → its lines."""
        if isinstance(src, LogSource):
            return src
        if src == "-" or src is sys.stdin:
            return cls(sys.stdin, name="<stdin>")
        if isinstance(src, (str, Path)):
            p = Path(src)
            if p.is_dir():
                raise ValueError(
                    f"{p} is a directory; use iter_sources() to expand it into files"
                )
            return cls(_read_lines(p), name=str(p))
        if hasattr(src, "read") or hasattr(src, "readline"):  # file-like / TextIO
            return cls(src, name=getattr(src, "name", "<stream>"))
        if isinstance(src, Iterable):
            return cls(src, name="<iterable>")
        raise TypeError(f"cannot build a LogSource from {type(src).__name__}")

    # -- iteration ---------------------------------------------------------
    def _iter_raw(self) -> Iterator:
        for item in self._buffer:
            yield item
        self._buffer = []
        for item in self._raw:
            yield item

    def peek(self, n: int = 5) -> list:
        """Return up to ``n`` raw items without consuming them (used by adapter
        auto-sniffing). Idempotent-ish: buffered items are replayed on iterate."""
        need = n - len(self._buffer)
        if need > 0:
            it = iter(self._raw)
            for _ in range(need):
                try:
                    self._buffer.append(next(it))
                except StopIteration:
                    break
            # The un-peeked remainder now lives in `it`; the first `need` items
            # are safely held in `self._buffer`. Reusing a fresh `iter(list)`
            # here (rather than chaining the original iterable back on) is what
            # keeps list/tuple sources from being yielded twice.
            self._raw = it
        return self._buffer[:n]

    def lines(self) -> Iterator[str]:
        for item in self._iter_raw():
            if isinstance(item, str):
                line = item.rstrip("\n")
                if line.strip():
                    yield line
            elif isinstance(item, (bytes, bytearray)):
                yield item.decode("utf-8", "replace").rstrip("\n")
            else:  # a dict/record — render back to a line
                yield json.dumps(item)

    def records(self) -> Iterator[dict]:
        for item in self._iter_raw():
            if isinstance(item, dict):
                yield item
                continue
            text = item.decode("utf-8", "replace") if isinstance(item, (bytes, bytearray)) else str(item)
            text = text.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError as e:
                self.skipped += 1
                logger.warning("ingest: skipping non-JSON line in %s: %s", self.name, e)
                continue
            if isinstance(obj, dict):
                yield obj
            else:
                self.skipped += 1

    def group_by(self, key: str | Callable[[dict], str]) -> "OrderedDict[str, list[dict]]":
        """Bucket records by a field name (dotted path) or callable. Preserves
        first-seen order so sessions come out in log order. A record missing the
        key lands under the sentinel ``"_ungrouped"`` bucket."""
        getter = key if callable(key) else (lambda r, k=key: _dotted(r, k))
        groups: "OrderedDict[str, list[dict]]" = OrderedDict()
        for rec in self.records():
            gk = getter(rec)
            gk = str(gk) if gk is not None else "_ungrouped"
            groups.setdefault(gk, []).append(rec)
        return groups


def iter_sources(src) -> Iterator[LogSource]:
    """Expand a possibly-directory ``src`` into one LogSource per file; pass
    through everything else as a single source. A directory is globbed for
    common log extensions, sorted for determinism."""
    if isinstance(src, (str, Path)) and src != "-" and Path(src).is_dir():
        d = Path(src)
        files = sorted({p for g in _LOG_GLOBS for p in d.glob(g)})
        if not files:
            logger.warning("ingest: no log files under %s (globs: %s)", d, _LOG_GLOBS)
        for p in files:
            yield LogSource.from_any(p)
        return
    yield LogSource.from_any(src)


def _read_lines(path: Path) -> Iterator[str]:
    with path.open() as f:
        yield from f


def _dotted(record: dict, path: str):
    """Look up ``a.b.c`` in nested dicts; return None if any hop is missing."""
    cur = record
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur
