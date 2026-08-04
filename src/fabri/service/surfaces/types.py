"""Value objects every surface adapter and the shared pipeline speak in.

These are deliberately dumb: the pipeline must be able to route, quota, launch
and deliver a run without knowing whether the surface is a chat workspace, a
code host, or an issue tracker. Anything surface-specific hides inside
``ReplyTarget.locator``, which the pipeline only ever passes back to the adapter
that produced it.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SurfaceCapabilities:
    """What a surface can do, so the pipeline stops guessing.

    ``hitl`` is the load-bearing one: a run that can ask a human a question but
    has no channel to ask it on would block until the process dies, so the
    pipeline disables ``ask_user`` for surfaces that answer False.
    """

    hitl: bool = False
    threads: bool = False
    attachments: bool = False


@dataclass(frozen=True)
class TenantRef:
    """Who this delivery belongs to: a Slack team, GitHub installation, Linear
    workspace. The quota key, and half of a run's recorded origin."""

    surface: str
    tenant_id: str | None = None

    def as_dict(self) -> dict:
        return {"surface": self.surface, "tenant_id": self.tenant_id}


@dataclass(frozen=True)
class ReplyTarget:
    """Where a result goes back to.

    ``locator`` is opaque to the pipeline and meaningful only to the adapter
    that made it (Slack: channel + thread_ts; GitHub: repo + issue number). It
    must stay JSON-serializable: it is persisted with the run so a delivery can
    outlive the process that started it.
    """

    tenant: TenantRef
    locator: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {**self.tenant.as_dict(), "locator": self.locator}

    def key(self) -> tuple:
        """A hashable identity for this target, for the HITL reply registry.

        Surface + locator, deliberately without the tenant: a locator is already
        globally unique on every surface we speak (a Slack channel id, a GitHub
        repo + issue, a Linear issue id), and including the tenant would break
        the match whenever one side of the round trip knows the tenant and the
        other does not -- which is exactly what happens when a run registers its
        thread before an inbound reply arrives.
        """
        return (
            self.tenant.surface,
            tuple(sorted((k, str(v)) for k, v in self.locator.items())),
        )


@dataclass(frozen=True)
class Command:
    """A parsed instruction from a surface. ``verb`` is the whole grammar."""

    verb: str  # "run" | "list" | "help"
    task: str = ""
    catalog_ref: str | None = None


# --- what an adapter decides an inbound delivery *is* -------------------------
#
# One sum type instead of a bool, because Slack's url_verification handshake has
# to be answered before signature verification -- a plain verify() -> bool
# cannot express "reply 200 with this body and stop", and every future adapter
# would rediscover that the hard way.


@dataclass(frozen=True)
class Dispatch:
    """Run something."""

    command: Command
    target: ReplyTarget


@dataclass(frozen=True)
class HitlAnswer:
    """A human answered a pending question on the surface."""

    target: ReplyTarget
    text: str


@dataclass(frozen=True)
class Handled:
    """The adapter dealt with it itself (install lifecycle, a revoked token)."""


@dataclass(frozen=True)
class Ignore:
    """Not for us: a bot's own message, an event type we don't act on."""


@dataclass(frozen=True)
class ShortCircuit:
    """Answer the HTTP request right now, without running anything."""

    status: int
    body: str = ""
    headers: dict = field(default_factory=dict)


InboundDecision = Dispatch | HitlAnswer | Handled | Ignore | ShortCircuit


@dataclass(frozen=True)
class RunOutcome:
    """The finished run, in the shape a surface needs to talk about it."""

    session_id: str
    success: bool
    final_text: str
    outcome: str | None = None
    total_cost_usd: float | None = None
    studio_url: str | None = None
