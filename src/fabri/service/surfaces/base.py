"""The contract a surface must satisfy to run fabri agencies.

Adding Discord, Jira, Teams, or a plain webhook should mean writing one of these
and registering it -- and touching nothing in the pipeline. That is the whole
design goal, and :mod:`tests.test_surface_pipeline` holds it to it by driving
the pipeline end to end against a fake adapter that imports nothing.

Why an ABC and not a Protocol: adapters share real default behaviour (most have
no HITL, most take their delivery id straight off the payload), and the
conformance suite needs something to enumerate.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping

from fabri.service.surfaces.types import (
    InboundDecision,
    ReplyTarget,
    RunOutcome,
    ShortCircuit,
    SurfaceCapabilities,
)


class SurfaceAdapter(ABC):
    """One inbound surface: verify, classify, deliver.

    An adapter owns its credentials and its wire format. It does not own run
    dispatch, quotas, dedupe, or cost policy -- those are the pipeline's, so
    that a new adapter cannot forget them.
    """

    #: Registry key, and the ``surface`` tag recorded in a run's origin.
    name: str = ""

    #: Where this surface POSTs. The HTTP layer routes from the registry, so an
    #: adapter never edits the server.
    webhook_path: str = ""

    def capabilities(self) -> SurfaceCapabilities:
        return SurfaceCapabilities()

    @abstractmethod
    def verify(
        self, raw_body: bytes, headers: Mapping[str, str]
    ) -> dict | ShortCircuit:
        """Authenticate the delivery, returning its payload.

        MUST fail closed: an absent, malformed, forged, or stale signature
        returns ``ShortCircuit(401, ...)`` and nothing else happens. Handshake
        challenges that must be answered before authentication (Slack's
        ``url_verification``) also return ``ShortCircuit``.
        """

    def delivery_id(self, payload: dict) -> str | None:
        """This delivery's unique id, for the pipeline's replay guard.

        ``None`` means "cannot dedupe" -- the pipeline will let it through, so
        return an id whenever the surface offers one.
        """
        return None

    @abstractmethod
    def classify(self, payload: dict) -> InboundDecision:
        """Decide what this delivery is.

        Adapters handle their own install lifecycle here and return
        :class:`Handled`. Command text is parsed with
        :func:`fabri.service.surfaces.pipeline.parse_command` so the grammar
        stays identical across surfaces; an adapter's job is to find the text,
        the tenant, and the reply target -- and to ignore its own bot's events
        so results never trigger new runs.
        """

    def deliver_ack(self, target: ReplyTarget, text: str) -> bool:
        """Optional "on it" receipt. Best effort; failure never aborts a run."""
        return False

    @abstractmethod
    def deliver_result(self, target: ReplyTarget, outcome: RunOutcome) -> bool:
        """Post the finished run back. The adapter escapes for its own medium."""

    @abstractmethod
    def deliver_error(self, target: ReplyTarget, text: str) -> bool:
        """Post a refusal: unknown ref, over quota, run failed."""

    def deliver_question(self, target: ReplyTarget, question: dict) -> bool:
        """Ask a human a mid-run question. Only called when ``hitl`` is True."""
        return False
