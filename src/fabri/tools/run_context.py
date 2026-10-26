"""Per-run and per-call context carried through context variables.

`run_agent` sets one `RunContext` for the whole run (repair attempts share it).
The dispatcher sets a `CallContext` around each tool call, and `invoke_batch`
sets a nested one per batch entry. Thread-pool dispatch copies the context per
submit, so parallel calls see the same run.

Code that runs outside `run_agent` (a host calling `registry.invoke` directly)
sees no `RunContext`, and `ToolRegistry.invoke` then behaves as it always has.

Fields named but not used yet (`gate`, `ledger`, `cancel`, `loaded`,
`deferred`, `occurrences`, `step_holds`, `landed_checks`, `key`) are slots for
the approval gate, write ledger, cancel and deferred tool loading.
"""
from __future__ import annotations

import contextlib
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any, Iterator


@dataclass
class RunContext:
    session_id: str
    hooks: Any = None
    gate: Any = None
    ledger: Any = None
    cancel: Any = None
    loaded: set | None = None
    deferred: frozenset = frozenset()
    occurrences: dict = field(default_factory=dict)
    step_holds: list = field(default_factory=list)
    landed_checks: Any = None
    # Set when the run is stopped early (core.hooks.StopSignal).
    stop: Any = None


@dataclass(frozen=True)
class CallContext:
    run: RunContext | None
    step: int | None = None
    call_index: int | None = None
    batch_index: int | None = None
    tool_call_id: str | None = None
    invoked_name: str | None = None
    canonical_name: str | None = None
    args: dict | None = None
    key: str | None = None


_RUN: ContextVar[RunContext | None] = ContextVar("fabri_run_context", default=None)
_CALL: ContextVar[CallContext | None] = ContextVar("fabri_call_context", default=None)


def current_run() -> RunContext | None:
    return _RUN.get()


def current_call() -> CallContext | None:
    return _CALL.get()


@contextlib.contextmanager
def run_scope(ctx: RunContext) -> Iterator[RunContext]:
    token = _RUN.set(ctx)
    try:
        yield ctx
    finally:
        _RUN.reset(token)


@contextlib.contextmanager
def call_scope(ctx: CallContext) -> Iterator[CallContext]:
    token = _CALL.set(ctx)
    try:
        yield ctx
    finally:
        _CALL.reset(token)


def batch_entry_call(index: int, name: str, args: dict) -> CallContext:
    """The CallContext for one batch entry: the enclosing call with the entry's
    name, args and `batch_index` swapped in."""
    parent = current_call()
    if parent is None:
        parent = CallContext(run=current_run())
    return replace(
        parent, batch_index=index, invoked_name=name, canonical_name=None, args=args,
    )
