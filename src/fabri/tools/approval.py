"""Approval classes for tools.

Every dynamic tool carries one of three classes so a host's approval gate can
decide what needs a human without knowing where the tool came from:

- ``read``: no side effects. Safe to run unattended.
- ``write``: changes state. A host usually asks first or runs it in dry-run.
- ``always_approve``: a human must approve every call, whatever the policy.

fabri only records the class. Enforcing it is the host's job.
"""
from __future__ import annotations

import re
from itertools import pairwise

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


RANK = {"read": 0, "write": 1, "always_approve": 2}
# Words that mark an action a human should approve on every call: it sends
# something to people (a message, comment, review, share) or moves money.
# Matched against name tokens and paths.
_SENSITIVE = frozenset({
    "send", "sends", "message", "messages", "msg", "email", "emails", "mail", "sms", "notify", "notification",
    "notifications", "broadcast", "publish", "post_message", "postmessage", "reply", "dm", "invite",
    "pay", "pays", "payment", "payments", "charge", "charges", "refund", "refunds", "payout", "payouts",
    "transfer", "transfers", "checkout", "purchase", "purchases", "withdraw", "withdrawal",
    "schedule", "reschedule", "resume", "activate", "launch", "trigger", "import", "subscribe", "unsubscribe",
    "sender", "export", "action",
})
# Words for posts other people read: a comment, a review, a reply, a share, a
# forward. They gate like _SENSITIVE, except on a name that starts with a read
# verb ("list_comments", "get_pull_request_reviews"), which only reads them.
_OUTBOUND = frozenset({
    "comment", "comments", "review", "reviews", "post", "posts", "replies", "share", "shares",
    "forward", "forwards",
})
_READ_VERBS = frozenset({
    "get", "list", "search", "read", "fetch", "find", "query", "view", "show", "describe", "count",
})
_DESTRUCTIVE = frozenset({"delete", "deletes", "remove", "destroy", "drop", "purge", "erase", "wipe", "truncate"})


def stricter(a: str, b: str) -> str:
    """The stricter of two approval classes."""
    return a if RANK[check_approval(a)] >= RANK[check_approval(b)] else b


def _tokens(*texts: object) -> set[str]:

    out: set[str] = set()
    for text in texts:
        if not text:
            continue
        spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(text)).lower()
        parts = [p for p in re.split(r"[^a-z0-9]+", spaced) if p]
        out.update(parts)
        out.update(f"{a}_{b}" for a, b in pairwise(parts))
    return out


def _gated(words: set[str], name: object) -> bool:
    """True when `words` hold a word that sends to people, moves money or
    deletes. `name` is the tool's own name, whose first word decides whether
    the _OUTBOUND words count."""
    if words & (_SENSITIVE | _DESTRUCTIVE):
        return True
    if not words & _OUTBOUND:
        return False
    lead = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(name or "")).lower()
    first = next((p for p in re.split(r"[^a-z0-9]+", lead) if p), "")
    return first not in _READ_VERBS


def safe_approval(method: str | None, *names: object) -> str:
    """The default class for an HTTP operation: ``read`` for GET, HEAD and
    OPTIONS; ``always_approve`` for DELETE and for anything whose name or path
    sends messages, posts a comment, review or share, or moves money;
    ``write`` otherwise. The bare method word is dropped from the name
    tokens, since generated operation ids often end in it
    ("create_key_keys_post") and the method is already known."""
    verb = (method or "").upper()
    if verb in READ_METHODS:
        return "read"
    if verb == "DELETE":
        return "always_approve"
    words = _tokens(*names) - {verb.lower()}
    return "always_approve" if _gated(words, names[0] if names else "") else "write"


def mcp_approval(name: str, description: str = "", annotations: object = None) -> str:
    """The default class for an MCP tool. Never ``read``: a server's own
    readOnlyHint is a claim, so read access has to be granted in the YAML.
    ``always_approve`` when the server marks it destructive or its name sends
    messages, posts a comment, review or share, moves money or deletes."""
    hints = annotations if isinstance(annotations, dict) else {}
    if hints.get("destructiveHint") is True:
        return "always_approve"
    return "always_approve" if _gated(_tokens(name), name) else "write"
