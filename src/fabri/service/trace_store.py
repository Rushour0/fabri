"""Descriptor-safe access to run trace files.

Every service-side open of a trace file goes through this module. A run owns
the directory its trace lives in (process mode) or writes the bytes the service
stores (container mode), so the service never trusts the path itself:

- each component below the run home is opened with ``O_NOFOLLOW`` relative to
  the parent directory's descriptor, so a symlinked ``.fabri``, ``traces`` or
  trace file is refused instead of followed;
- the opened file must be a regular file owned by the service's uid with
  ``st_nlink == 1``, so a hardlink to another file (``installs.db``, another
  run's trace) is refused;
- a reader or writer keeps its one descriptor for its whole life and never
  reopens by path, so swapping the path after open changes nothing.

A refused open raises :class:`TraceOpenError`.
"""
from __future__ import annotations

import errno
import fcntl
import os
import stat
from pathlib import Path

__all__ = [
    "TraceOpenError",
    "TraceReader",
    "TraceWriter",
    "open_append",
    "open_read",
    "trace_root",
]

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0)
_APPEND_FLAGS = (os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
                 | getattr(os, "O_NONBLOCK", 0))


class TraceOpenError(OSError):
    """A trace path was refused: a symlink, a hardlink, not a regular file,
    or not owned by the service user."""


def trace_root(path: str | Path) -> Path:
    """The directory the walk starts from for ``path``.

    For the standard layout ``<run home>/.fabri/traces/<sid>.jsonl`` this is
    the run home, which the service created, so ``.fabri`` and ``traces`` are
    both opened without following links. Any other path starts at its parent.
    """
    p = Path(path)
    parent = p.parent
    if parent.name == "traces" and parent.parent.name == ".fabri":
        return parent.parent.parent
    return parent


def _refuse(path: Path, why: str) -> TraceOpenError:
    return TraceOpenError(errno.EPERM, f"trace file refused ({why})", str(path))


def _open_parent(path: Path, root: Path, *, create: bool) -> int:
    """A descriptor for ``path.parent``, walking from ``root`` without
    following any link below it. ``create`` makes missing directories."""
    try:
        rel = path.parent.relative_to(root)
    except ValueError:
        raise _refuse(path, "path is outside its trace root") from None
    if create:
        root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    for part in rel.parts:
        if part in ("", ".", ".."):
            os.close(fd)
            raise _refuse(path, "path has a relative component")
        try:
            try:
                nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise _refuse(path, f"{part} is not a plain directory") from None
            raise
        os.close(fd)
        fd = nxt
    return fd


def _check(fd: int, path: Path) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise _refuse(path, "not a regular file")
    if info.st_uid != os.geteuid():
        raise _refuse(path, "not owned by the service user")
    if info.st_nlink != 1:
        raise _refuse(path, "file has more than one link")
    return info


def _open(path: str | Path, flags: int, *, root: str | Path | None, create: bool) -> int:
    p = Path(os.path.abspath(path))
    base = Path(os.path.abspath(root)) if root is not None else trace_root(p)
    dir_fd = _open_parent(p, base, create=create)
    try:
        try:
            fd = os.open(p.name, flags, 0o600, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise _refuse(p, "path is a symlink") from None
            if exc.errno in (errno.EISDIR, errno.ENXIO, errno.EOPNOTSUPP, errno.ENOTSUP):
                raise _refuse(p, "not a regular file") from None
            raise
    finally:
        os.close(dir_fd)
    try:
        _check(fd, p)
        if flags & getattr(os, "O_NONBLOCK", 0):
            os.set_blocking(fd, True)
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_read(path: str | Path, *, root: str | Path | None = None) -> int:
    """A read-only descriptor for an existing trace file, or
    :class:`TraceOpenError`. ``FileNotFoundError`` when it does not exist."""
    return _open(path, _READ_FLAGS, root=root, create=False)


def open_append(path: str | Path, *, root: str | Path | None = None) -> int:
    """An append-only descriptor, creating the file (``0600``) and any
    missing directories below ``root``, or :class:`TraceOpenError`."""
    return _open(path, _APPEND_FLAGS, root=root, create=True)


class TraceWriter:
    """Appends whole lines to one trace file through one descriptor."""

    def __init__(self, path: str | Path, *, root: str | Path | None = None) -> None:
        self.path = Path(path)
        self._fd: int | None = open_append(path, root=root)

    def append(self, line: str | bytes) -> None:
        if self._fd is None:
            raise ValueError("trace writer is closed")
        data = line.encode("utf-8") if isinstance(line, str) else line
        if not data.endswith(b"\n"):
            data += b"\n"
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        try:
            view = memoryview(data)
            while view:
                n = os.write(self._fd, view)
                view = view[n:]
        finally:
            fcntl.flock(self._fd, fcntl.LOCK_UN)

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> TraceWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class TraceReader:
    """Reads new bytes from one trace file through one descriptor.

    The file is opened on the first :meth:`read_new` that finds it, then the
    descriptor is kept and read from its last offset, so each poll costs only
    the bytes appended since the last one.
    """

    def __init__(self, path: str | Path, *, root: str | Path | None = None) -> None:
        self.path = Path(path)
        self._root = root
        self._fd: int | None = None
        self._closed = False

    @property
    def opened(self) -> bool:
        return self._fd is not None

    def read_new(self, chunk: int = 1 << 20) -> bytes:
        """Bytes appended since the last call. Empty when the file does not
        exist yet. Raises :class:`TraceOpenError` for a refused path."""
        if self._closed:
            return b""
        if self._fd is None:
            try:
                self._fd = open_read(self.path, root=self._root)
            except FileNotFoundError:
                return b""
        parts: list[bytes] = []
        while True:
            data = os.read(self._fd, chunk)
            if not data:
                break
            parts.append(data)
        return b"".join(parts)

    def close(self) -> None:
        self._closed = True
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> TraceReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
