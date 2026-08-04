"""Surface adapters: how fabri runs reach the tools a team already uses.

One contract (:mod:`.base`), one shared pipeline (:mod:`.pipeline`), and one
adapter per surface. Adding an integration means writing an adapter and
registering it -- not editing the pipeline, the service, or the HTTP server.
"""
from fabri.service.surfaces.base import SurfaceAdapter
from fabri.service.surfaces.registry import SurfaceRegistry
from fabri.service.surfaces.types import (
    Command,
    Dispatch,
    Handled,
    HitlAnswer,
    Ignore,
    InboundDecision,
    ReplyTarget,
    RunOutcome,
    ShortCircuit,
    SurfaceCapabilities,
    TenantRef,
)

__all__ = [
    "Command",
    "Dispatch",
    "Handled",
    "HitlAnswer",
    "Ignore",
    "InboundDecision",
    "ReplyTarget",
    "RunOutcome",
    "ShortCircuit",
    "SurfaceAdapter",
    "SurfaceCapabilities",
    "SurfaceRegistry",
    "TenantRef",
]
