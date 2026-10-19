"""Approval classes for tools.

Every dynamic tool carries one of three classes so a host's approval gate can
decide what needs a human without knowing where the tool came from:

- ``read``: no side effects. Safe to run unattended.
- ``write``: changes state. A host usually asks first or runs it in dry-run.
- ``always_approve``: a human must approve every call, whatever the policy.

fabri only records the class. Enforcing it is the host's job.
"""
from __future__ import annotations

APPROVAL_CLASSES: tuple[str, ...] = ("read", "write", "always_approve")
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class ApprovalError(ValueError):
    """An approval value is not one of APPROVAL_CLASSES."""


def default_approval_for_method(method: str | None) -> str:
    """``read`` for GET, HEAD and OPTIONS, ``write`` for everything else."""
    return "read" if (method or "").upper() in READ_METHODS else "write"


def check_approval(value: object) -> str:
    if value not in APPROVAL_CLASSES:
        raise ApprovalError(
            f"approval must be one of {', '.join(APPROVAL_CLASSES)} (got {value!r})"
        )
    return str(value)
