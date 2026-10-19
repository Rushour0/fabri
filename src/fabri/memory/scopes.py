"""Scoped memory: USER, ORG and GLOBAL collections with a store-level guard.

Three scopes keep learnings apart:

* USER   private to one person. Preferences, phrasing, personal shortcuts.
* ORG    shared inside one organization. Org facts and process lessons.
* GLOBAL generic, de-identified lessons that a human approved (see
         ``fabri.memory.global_promotion``).

Collection names come from hashed ids. A raw email or name never appears in a
Qdrant collection name or a sqlite file name.

Isolation is enforced twice. Callers only ever open the collections that match
their scope, and every store method also calls ``check_collection_access``,
which rejects a user or org collection that does not belong to the scope in
force. The guard fails closed: a user or org collection with no scope in force
is rejected unless the caller entered ``admin_access()`` (used by the
``forget`` and ``promote`` CLI commands).
"""
from __future__ import annotations

import contextlib
import contextvars
import functools
import hashlib
import os
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from fabri.memory.schema import MemoryEntry

USER_PREFIX = "fabri_u_"
ORG_PREFIX = "fabri_o_"
GLOBAL_COLLECTION = "fabri_global"
CANDIDATES_COLLECTION = "fabri_global_candidates"

SCOPE_USER = "user"
SCOPE_ORG = "org"
SCOPE_GLOBAL = "global"
SCOPES = (SCOPE_USER, SCOPE_ORG, SCOPE_GLOBAL)

_HASH_LEN = 24
_SALT_ENV = "FABRI_SCOPE_SALT"


class ScopeViolation(PermissionError):
    """A store call targeted a collection outside the scope in force."""


def hash_id(kind: str, raw: str) -> str:
    """Stable, non-reversible id for a user or org. Case and edge whitespace
    are ignored so ``Alice@Example.com `` and ``alice@example.com`` match.
    An optional ``FABRI_SCOPE_SALT`` env var blocks dictionary guessing of
    low-entropy ids such as emails."""
    norm = (raw or "").strip().lower()
    if not norm:
        raise ValueError(f"{kind} id must be a non-empty string")
    salt = os.environ.get(_SALT_ENV, "")
    digest = hashlib.sha256(f"fabri-scope-v1/{salt}/{kind}/{norm}".encode()).hexdigest()
    return digest[:_HASH_LEN]


def user_collection_name(user_id: str) -> str:
    return USER_PREFIX + hash_id("user", user_id)


def org_collection_name(org_id: str) -> str:
    return ORG_PREFIX + hash_id("org", org_id)


@dataclass(frozen=True)
class ScopeBudgets:
    """Per-scope top-k when retrieval merges the three collections."""

    user: int = 2
    org: int = 3
    glob: int = 2

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "ScopeBudgets":
        raw = raw or {}
        return cls(
            user=int(raw.get("user", cls.user)),
            org=int(raw.get("org", cls.org)),
            glob=int(raw.get("global", raw.get("glob", cls.glob))),
        )


@dataclass(frozen=True)
class MemoryScope:
    """Who a run is for. ``user_id`` must be unique across all orgs (an account
    id, not a first name). ``aliases`` are display names (person, org, project)
    kept only so the de-identifier can remove them later; they are never used in
    a collection name."""

    user_id: str
    org_id: str
    project_id: str | None = None
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not (self.user_id or "").strip() or not (self.org_id or "").strip():
            raise ValueError("MemoryScope needs a non-empty user_id and org_id")

    @property
    def user_collection(self) -> str:
        return user_collection_name(self.user_id)

    @property
    def org_collection(self) -> str:
        return org_collection_name(self.org_id)

    @property
    def project_hash(self) -> str | None:
        return hash_id("project", self.project_id) if self.project_id else None

    @property
    def allowed_collections(self) -> frozenset[str]:
        return frozenset({self.user_collection, self.org_collection})

    def names(self) -> list[str]:
        """Every string the de-identifier must strip for this scope."""
        out = [self.user_id, self.org_id, *self.aliases]
        if self.project_id:
            out.append(self.project_id)
        return [n for n in out if n and n.strip()]

    @classmethod
    def from_config(cls, mem_cfg: Mapping[str, Any]) -> "MemoryScope | None":
        """Build from ``memory.scoped`` config. None when scoping is off or
        the ids are missing (the caller can then supply them per run)."""
        cfg = (mem_cfg or {}).get("scoped") or {}
        if not cfg.get("enabled"):
            return None
        user_id, org_id = cfg.get("user_id"), cfg.get("org_id")
        if not user_id or not org_id:
            return None
        return cls(
            user_id=str(user_id),
            org_id=str(org_id),
            project_id=str(cfg["project_id"]) if cfg.get("project_id") else None,
            aliases=tuple(str(a) for a in (cfg.get("aliases") or ())),
        )


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------

_current_scope: contextvars.ContextVar[MemoryScope | None] = contextvars.ContextVar(
    "fabri_memory_scope", default=None
)
_admin: contextvars.ContextVar[bool] = contextvars.ContextVar("fabri_memory_admin", default=False)


@contextlib.contextmanager
def scope_context(scope: MemoryScope | None) -> Iterator[None]:
    token = _current_scope.set(scope)
    try:
        yield
    finally:
        _current_scope.reset(token)


@contextlib.contextmanager
def admin_access() -> Iterator[None]:
    """Operator access across scoped collections (forget, promote scan). Only
    the CLI maintenance commands should enter this."""
    token = _admin.set(True)
    try:
        yield
    finally:
        _admin.reset(token)


def current_scope() -> MemoryScope | None:
    return _current_scope.get()


def is_scoped_collection(collection: str) -> bool:
    return collection.startswith((USER_PREFIX, ORG_PREFIX))


def check_collection_access(collection: str, bound: MemoryScope | None = None) -> None:
    """Raise ScopeViolation unless ``collection`` may be touched right now.

    Unscoped collections (the legacy single collection, GLOBAL, the candidate
    queue) pass. A user or org collection passes only when it belongs to the
    scope bound to the store, or to the scope in force when no scope is bound.
    A bound scope that disagrees with the scope in force is rejected too."""
    if not is_scoped_collection(collection):
        return
    if _admin.get():
        return
    active = _current_scope.get()
    if bound is not None and active is not None and bound != active:
        raise ScopeViolation(
            "store is bound to a different memory scope than the one in force"
        )
    effective = bound if bound is not None else active
    if effective is None:
        raise ScopeViolation(
            f"collection {collection!r} is scoped but no memory scope is in force"
        )
    if collection not in effective.allowed_collections:
        raise ScopeViolation(
            f"collection {collection!r} does not belong to the current memory scope"
        )


def guarded(fn: Callable) -> Callable:
    """Decorator for store methods: enforce the scope guard on every call."""

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        check_collection_access(self.collection, getattr(self, "bound_scope", None))
        return fn(self, *args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# ScopedMemory: the three stores of one scope, with routing and merged retrieval
# ---------------------------------------------------------------------------

@dataclass
class ScopedMemory:
    """USER, ORG and GLOBAL stores for one MemoryScope."""

    scope: MemoryScope
    base: Any  # a store; only used to locate the backend and open siblings
    budgets: ScopeBudgets = field(default_factory=ScopeBudgets)
    global_collection: str = GLOBAL_COLLECTION
    default_scope: str = SCOPE_ORG
    classifier_llm: Any = None
    use_llm_classifier: bool = False
    _stores: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_store(
        cls, base: Any, scope: MemoryScope, *, budgets: ScopeBudgets | None = None, **kw: Any
    ) -> "ScopedMemory":
        return cls(scope=scope, base=base, budgets=budgets or ScopeBudgets(), **kw)

    @classmethod
    def from_config(cls, mem_cfg: Mapping[str, Any], scope: MemoryScope, base: Any) -> "ScopedMemory":
        cfg = mem_cfg.get("scoped") or {}
        return cls(
            scope=scope,
            base=base,
            budgets=ScopeBudgets.from_mapping(cfg.get("budgets")),
            global_collection=cfg.get("global_collection") or GLOBAL_COLLECTION,
            default_scope=cfg.get("default_scope", SCOPE_ORG),
            use_llm_classifier=bool(cfg.get("llm_classifier", False)),
        )

    def store(self, tag: str):
        if tag not in SCOPES:
            raise ValueError(f"unknown memory scope tag: {tag!r}")
        if tag not in self._stores:
            name = {
                SCOPE_USER: self.scope.user_collection,
                SCOPE_ORG: self.scope.org_collection,
                SCOPE_GLOBAL: self.global_collection,
            }[tag]
            self._stores[tag] = self.base.sibling(name, bound_scope=self.scope)
        return self._stores[tag]

    # -- writes ---------------------------------------------------------------

    def classify(self, text: str, kind: str | None = None):
        from fabri.memory.scope_classify import classify_scope

        return classify_scope(
            text,
            kind=kind,
            scope=self.scope,
            llm=self.classifier_llm,
            use_llm=self.use_llm_classifier,
            default=self.default_scope,
        )

    def route(self, text: str, kind: str | None = None, force: str | None = None):
        """Pick the store a new item belongs in. Returns (tag, store).
        GLOBAL is never a write target: only the approved-candidate path
        writes there."""
        tag = force or self.classify(text, kind).scope
        if tag not in (SCOPE_USER, SCOPE_ORG):
            raise ValueError("writes go to the user or org scope; global is promotion-only")
        return tag, self.store(tag)

    def annotate(self, entry: "MemoryEntry", tag: str, store: Any) -> None:
        """Stamp scope tag and de-identifier aliases onto a stored entry."""
        payload = dict(entry.payload or {})
        payload["memory_scope"] = tag
        if tag == SCOPE_ORG:
            payload["aliases"] = self.scope.names()
        entry.payload = payload
        with scope_context(self.scope):
            store.upsert(entry)

    # -- reads ----------------------------------------------------------------

    def retrieve(
        self,
        task: str,
        *,
        tool_names: list[str] | None = None,
        retrieval_config: Any = None,
        session_id: str | None = None,
    ) -> tuple[str, dict]:
        """Top-k per scope, merged into one fenced block with every line
        tagged by scope. Returns (text, meta) like ``retrieve_context_with_meta``."""
        from fabri.orchestrator.retrieval import (
            CONVENTION_FENCE_CLOSE,
            CONVENTION_FENCE_OPEN,
            GUIDELINE_FENCE_CLOSE,
            GUIDELINE_FENCE_OPEN,
            RetrievalConfig,
            _retrieve_inner,
        )

        rcfg = replace(retrieval_config or RetrievalConfig(), global_collection=None)
        budgets = {
            SCOPE_USER: self.budgets.user,
            SCOPE_ORG: self.budgets.org,
            SCOPE_GLOBAL: self.budgets.glob,
        }
        guideline_lines: list[str] = []
        convention_blocks: list[str] = []
        seen: set[str] = set()
        merged_all: list[tuple[Any, float]] = []
        counts = {tag: 0 for tag in SCOPES}

        with scope_context(self.scope):
            for tag in SCOPES:  # narrowest first, so a duplicate keeps the narrowest tag
                k = budgets[tag]
                if k <= 0:
                    continue
                try:
                    text, merged = _retrieve_inner(
                        self.store(tag), task, top_k=k, tool_names=tool_names,
                        retrieval_config=rcfg, session_id=session_id,
                    )
                except ScopeViolation:
                    raise
                except Exception:  # noqa: BLE001 -- one broken scope must not sink the run
                    from fabri.core.logging_setup import get_logger

                    get_logger().warning("scoped retrieval failed for %s", tag, exc_info=True)
                    continue
                if not merged:
                    continue
                for line in _fence_inner(text, GUIDELINE_FENCE_OPEN, GUIDELINE_FENCE_CLOSE).splitlines():
                    if not line.startswith("- ["):
                        guideline_lines.append(line)
                        continue
                    key = line.split("] ", 1)[-1].strip().lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    guideline_lines.append(f"- [{tag}] " + line[2:])
                    counts[tag] += 1
                conv_inner = _fence_inner(text, CONVENTION_FENCE_OPEN, CONVENTION_FENCE_CLOSE)
                for block in re.findall(r"<convention>.*?</convention>", conv_inner, flags=re.S):
                    convention_blocks.append(block.replace("<convention>", f'<convention scope="{tag}">', 1))
                    counts[tag] += 1
                merged_all.extend(merged)

        sections: list[str] = []
        if guideline_lines:
            sections.append(
                GUIDELINE_FENCE_OPEN + "\n" + "\n".join(guideline_lines) + "\n" + GUIDELINE_FENCE_CLOSE
            )
        if convention_blocks:
            sections.append(
                CONVENTION_FENCE_OPEN + "\n" + "\n".join(convention_blocks) + "\n" + CONVENTION_FENCE_CLOSE
            )
        meta: dict[str, Any] = {
            "retrieved": len(merged_all),
            "from_prior_sessions": sum(
                1 for e, _ in merged_all
                if any(s != session_id for s in e.session_ids) or (e.hit_count or 0) >= 2
            ),
            "strategic": sum(1 for e, _ in merged_all if e.kind == "strategic"),
            "by_scope": counts,
        }
        conventions = [e for e, _ in merged_all if e.kind == "convention"]
        if conventions:
            meta["retrieved_conventions"] = conventions
        return "\n".join(sections), meta


def _fence_inner(text: str, open_tag: str, close_tag: str) -> str:
    if open_tag not in text:
        return ""
    return text.split(open_tag, 1)[1].split(close_tag, 1)[0].strip("\n")


def scoped_collections(names: Sequence[str], prefix: str) -> list[str]:
    return sorted(n for n in names if n.startswith(prefix))
