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
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "MemoryEntry":
        return cls(
            text=payload["text"],
            kind=payload["kind"],
            session_ids=list(payload.get("session_ids", [])),
            tags=list(payload.get("tags", [])),
            tools=list(payload.get("tools", [])),
            hit_count=payload.get("hit_count", 1),
            created_at=payload.get("created_at", time.time()),
            model_version=payload.get("model_version", EMBEDDING_MODEL_VERSION),
        )
