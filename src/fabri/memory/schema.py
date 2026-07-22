import hashlib
import time
import uuid
from dataclasses import dataclass, field

EMBEDDING_MODEL_VERSION = "minilm-l6-v2"

# Per-kind point-id namespace. Kinds NOT listed fall back to "failure"
# (tactical + strategic share it because promotion mutates kind in place on
# the same point). success_pattern and postmortem are distinct signals that
# must not collide with a textually similar failure entry.
_ID_NAMESPACE = {
    "success_pattern": "success",
    "postmortem": "postmortem",
}


@dataclass
class MemoryEntry:
    text: str
    kind: str  # "tactical" | "strategic"
    session_ids: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    hit_count: int = 1
    created_at: float = field(default_factory=time.time)
    model_version: str = EMBEDDING_MODEL_VERSION
    domain: str = "generic"           # "code"|"planning"|"search"|"api"|"generic"
    outcome: str = "unknown"          # "failure"|"partial"|"success"|"unknown"
    agent_id: str | None = None       # which agent produced this entry
    task_embedding_hash: str | None = None  # sha256(task)[:8] for clustering
    dedup_key: str | None = None      # stable signature of the mined lesson inputs
    producer_agent_id: str | None = None
    scope: str = "agent"              # "agent" | "agency" | "company"
    verification: str = "unverified"  # unverified | tool_verified | rubric_verified | contradicted
    tier: str = "unclassified"        # core | retrieve | action | quarantine | unclassified
    resolution: dict | None = None     # typed ActionMemory recovery payload
    source_session_ids: list[str] = field(default_factory=list)
    source_event_ids: list[str] = field(default_factory=list)
    applicability: list[str] = field(default_factory=list)
    do_not_reuse_when: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Keep legacy provenance aliases synchronized during the transition."""
        sessions = list(dict.fromkeys([*self.source_session_ids, *self.session_ids]))
        self.source_session_ids = sessions
        self.session_ids = list(sessions)
        producer = self.producer_agent_id or self.agent_id
        self.producer_agent_id = producer
        self.agent_id = producer

    @property
    def id(self) -> str:
        """Deterministic point ID derived from the compressed text, so re-upserting
        the same guideline is idempotent rather than relying on locking.

        A4: success_pattern entries share a separate id namespace from
        failure-derived guidelines (tactical / strategic). Tactical and
        strategic share a namespace because promotion mutates kind in place
        on the same point; success_pattern is a different signal entirely
        and must not collide with a textually similar failure entry.

        M1: postmortem entries (whole-run summaries written on every run when
        enabled) get their own namespace too -- a postmortem and a tactical
        guideline can share words without one suppressing the other."""
        namespace = _ID_NAMESPACE.get(self.kind, "failure")
        digest = hashlib.sha256(f"{namespace}::{self.text.strip().lower()}".encode()).hexdigest()
        return str(uuid.UUID(digest[:32]))

    def to_payload(self) -> dict:
        return {
            "text": self.text,
            "kind": self.kind,
            "session_ids": self.session_ids,
            "tags": self.tags,
            "tools": self.tools,
            "hit_count": self.hit_count,
            "created_at": self.created_at,
            "model_version": self.model_version,
            "domain": self.domain,
            "outcome": self.outcome,
            "agent_id": self.agent_id,
            "task_embedding_hash": self.task_embedding_hash,
            "dedup_key": self.dedup_key,
            "producer_agent_id": self.producer_agent_id,
            "scope": self.scope,
            "verification": self.verification,
            "tier": self.tier,
            "resolution": self.resolution,
            "source_session_ids": self.source_session_ids,
            "source_event_ids": self.source_event_ids,
            "applicability": self.applicability,
            "do_not_reuse_when": self.do_not_reuse_when,
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "MemoryEntry":
        return cls(
            text=payload["text"],
            kind=payload["kind"],
            session_ids=list(payload.get("session_ids", payload.get("source_session_ids", []))),
            tags=list(payload.get("tags", [])),
            tools=list(payload.get("tools", [])),
            hit_count=payload.get("hit_count", 1),
            created_at=payload.get("created_at", time.time()),
            model_version=payload.get("model_version", EMBEDDING_MODEL_VERSION),
            domain=payload.get("domain", "generic"),
            outcome=payload.get("outcome", "unknown"),
            agent_id=payload.get("agent_id", payload.get("producer_agent_id")),
            task_embedding_hash=payload.get("task_embedding_hash"),
            dedup_key=payload.get("dedup_key"),
            producer_agent_id=payload.get("producer_agent_id", payload.get("agent_id")),
            scope=payload.get("scope", "agent"),
            verification=payload.get("verification", "unverified"),
            tier=payload.get("tier", "unclassified"),
            resolution=payload.get("resolution"),
            source_session_ids=list(
                payload.get("source_session_ids", payload.get("session_ids", []))
            ),
            source_event_ids=list(payload.get("source_event_ids", [])),
            applicability=list(payload.get("applicability", [])),
            do_not_reuse_when=list(payload.get("do_not_reuse_when", [])),
        )
