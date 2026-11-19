"""Where the pipeline remembers which deliveries it has already seen.

Surfaces retry. Slack resends an event it thinks went unanswered, GitHub
redelivers a webhook from its settings page, and a load balancer may hand the
same request to a second replica. The pipeline's replay guard asks this store
one question per delivery: am I the first to see this id?

The default keeps the answer in process memory, which is right for a single
``fabri serve``. Several receiver replicas behind a load balancer each have
their own memory, so a retry that lands on a different replica would run
twice. A durable shared store fixes that: a database table with a unique key
on ``(surface, delivery_id)`` where ``claim`` is an insert that reports whether
it won, or a conditional put (``attribute_not_exists``, ``SET NX``) on a
key-value store. Either makes the claim atomic across replicas.
"""
from __future__ import annotations

from collections import OrderedDict
from threading import Lock
from typing import Protocol, runtime_checkable

_MAX_DELIVERY_IDS = 512


@runtime_checkable
class DeliveryStore(Protocol):
    """The replay guard's memory."""

    def claim(self, surface: str, delivery_id: str) -> bool:
        """True when this call is the first to see ``delivery_id`` on
        ``surface``, False for a repeat. Must be atomic: two concurrent claims
        of the same id return True exactly once."""
        ...


class InMemoryDeliveryStore:
    """The last 512 delivery ids per surface, in this process only.

    Old ids fall off the end, so a retry arriving after 512 newer deliveries is
    treated as new. Surfaces retry within minutes, so that has been enough for
    a single process.
    """

    def __init__(self, max_ids: int = _MAX_DELIVERY_IDS) -> None:
        self._max_ids = max_ids
        self._ids: dict[str, OrderedDict[str, None]] = {}
        self._lock = Lock()

    def claim(self, surface: str, delivery_id: str) -> bool:
        with self._lock:
            seen = self._ids.setdefault(surface, OrderedDict())
            if delivery_id in seen:
                return False
            seen[delivery_id] = None
            if len(seen) > self._max_ids:
                seen.popitem(last=False)
        return True

    def clear(self) -> None:
        with self._lock:
            self._ids.clear()
