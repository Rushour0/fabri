"""GLOBAL promotion: generic lessons that recur across organizations.

Flow:

1. ``scan_orgs`` reads each org collection as an operator. Every lesson is
   de-identified first (``fabri.memory.scrub``). Only the scrubbed text is fed
   to ``ingest_guideline`` in the ``global_candidates`` collection, with the
   hashed org id standing in for the session id. pruning.py's recurrence
   machinery then counts distinct orgs the same way it counts distinct sessions.
2. A candidate is listable once it has at least N distinct source orgs
   (default 3). The list shows the de-identified text and the source count only.
3. A human runs ``approve`` or ``reject``. Only ``approve`` writes to the GLOBAL
   collection. Nothing reaches GLOBAL any other way.
4. ``forget_org`` drops the org collection and removes that org from every
   candidate. Pending candidates that no longer meet N are deleted.

User collections are never read by this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fabri.memory.pruning import PROMOTION_THRESHOLD_SESSIONS, SIMILARITY_THRESHOLD, ingest_guideline
from fabri.memory.schema import MemoryEntry
from fabri.memory.scopes import (
    CANDIDATES_COLLECTION,
    GLOBAL_COLLECTION,
    ORG_PREFIX,
    USER_PREFIX,
    admin_access,
    org_collection_name,
    user_collection_name,
)
from fabri.memory.scrub import leak_check, placeholder_ratio, rewrite_generic, scrub

DEFAULT_MIN_ORGS = PROMOTION_THRESHOLD_SESSIONS  # 3

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

# Process lessons only. Postmortems and conventions describe one run or one org.
_LESSON_KINDS = frozenset({"tactical", "strategic", "success_pattern"})
_MIN_TEXT_CHARS = 20
_MAX_PLACEHOLDER_RATIO = 0.35


@dataclass
class Candidate:
    """What the CLI may show: an opaque id, de-identified text, a count."""

    id: str
    text: str
    source_count: int
    status: str


@dataclass
class ScanReport:
    orgs_scanned: int = 0
    lessons_seen: int = 0
    ingested: int = 0
    skipped_unclean: int = 0
    skipped_other: int = 0


@dataclass
class ForgetReport:
    collection_dropped: bool = False
    candidates_updated: int = 0
    candidates_dropped: int = 0
    approved_below_min: int = 0


def candidates_store(base: Any):
    return base.sibling(CANDIDATES_COLLECTION)


def global_store(base: Any, name: str = GLOBAL_COLLECTION):
    return base.sibling(name)


def _status(entry: MemoryEntry) -> str:
    return str((entry.payload or {}).get("status", STATUS_PENDING))


def _sources(entry: MemoryEntry) -> set[str]:
    return set(entry.session_ids)


def scan_orgs(
    base: Any,
    *,
    min_orgs: int = DEFAULT_MIN_ORGS,
    similarity: float = SIMILARITY_THRESHOLD,
    llm: Any = None,
    rewrite: bool = False,
    on_usage: Any = None,
) -> ScanReport:
    """De-identify org lessons and count recurrence across distinct orgs."""
    report = ScanReport()
    cand = candidates_store(base)
    with admin_access():
        org_names = [n for n in base.list_collections() if n.startswith(ORG_PREFIX)]
        for name in org_names:
            org_hash = name[len(ORG_PREFIX):]
            org_store = base.sibling(name)
            report.orgs_scanned += 1
            for entry in org_store.iterate():
                report.lessons_seen += 1
                if (
                    entry.kind not in _LESSON_KINDS
                    or entry.tier == "quarantine"
                    or entry.verification == "contradicted"
                    or (entry.payload or {}).get("memory_scope") == "user"
                ):
                    report.skipped_other += 1
                    continue
                names = list((entry.payload or {}).get("aliases") or [])
                text = scrub(entry.text, names)
                if rewrite and llm is not None:
                    rewritten = rewrite_generic(text, llm, on_usage=on_usage)
                    if rewritten:
                        text = scrub(rewritten, names)
                if (
                    len(text) < _MIN_TEXT_CHARS
                    or placeholder_ratio(text) > _MAX_PLACEHOLDER_RATIO
                    or leak_check(text, names)
                ):
                    report.skipped_unclean += 1
                    continue
                ingest_guideline(
                    cand,
                    text,
                    session_id=org_hash,
                    kind="tactical",
                    similarity_threshold=similarity,
                    promotion_threshold_sessions=min_orgs,
                )
                report.ingested += 1
    return report


def list_candidates(
    base: Any, *, min_orgs: int = DEFAULT_MIN_ORGS, status: str | None = STATUS_PENDING
) -> list[Candidate]:
    """Candidates that meet N distinct orgs. ``status=None`` lists all states."""
    out = []
    for entry in candidates_store(base).iterate():
        count = len(_sources(entry))
        if count < min_orgs:
            continue
        if status is not None and _status(entry) != status:
            continue
        out.append(Candidate(entry.id, entry.text, count, _status(entry)))
    out.sort(key=lambda c: (-c.source_count, c.id))
    return out


def _resolve(cand_store: Any, ident: str) -> MemoryEntry:
    matches = [e for e in cand_store.iterate() if e.id == ident or e.id.startswith(ident)]
    if not matches:
        raise KeyError(f"no candidate matches {ident!r}")
    if len(matches) > 1:
        raise KeyError(f"candidate id {ident!r} is ambiguous")
    return matches[0]


def approve(
    base: Any,
    ident: str,
    *,
    min_orgs: int = DEFAULT_MIN_ORGS,
    global_collection: str = GLOBAL_COLLECTION,
) -> MemoryEntry:
    """Write one pending candidate into GLOBAL. Re-checks N at approval time."""
    cand = candidates_store(base)
    entry = _resolve(cand, ident)
    count = len(_sources(entry))
    if count < min_orgs:
        raise ValueError(f"candidate has {count} source org(s); needs {min_orgs}")
    if _status(entry) == STATUS_REJECTED:
        raise ValueError("candidate was rejected")
    glob = global_store(base, global_collection)
    promoted = MemoryEntry(
        text=entry.text,
        kind="strategic",
        hit_count=count,
        verification="human_verified",
        tier="retrieve",
        scope="global",
        domain=entry.domain,
        payload={"memory_scope": "global", "source_orgs": count},
    )
    glob.upsert(promoted)
    entry.payload = {**(entry.payload or {}), "status": STATUS_APPROVED}
    cand.upsert(entry)
    return promoted


def reject(base: Any, ident: str) -> MemoryEntry:
    cand = candidates_store(base)
    entry = _resolve(cand, ident)
    entry.payload = {**(entry.payload or {}), "status": STATUS_REJECTED}
    cand.upsert(entry)
    return entry


def forget_org(
    base: Any, *, org_id: str | None = None, org_hash: str | None = None,
    min_orgs: int = DEFAULT_MIN_ORGS,
) -> ForgetReport:
    """Delete an org's collection and withdraw it from every candidate."""
    if (org_id is None) == (org_hash is None):
        raise ValueError("pass exactly one of org_id or org_hash")
    name = org_collection_name(org_id) if org_id else ORG_PREFIX + str(org_hash)
    org_hash = name[len(ORG_PREFIX):]
    report = ForgetReport()
    with admin_access():
        if name in base.list_collections():
            base.sibling(name).drop()
            report.collection_dropped = True
        if CANDIDATES_COLLECTION not in base.list_collections():
            return report
        cand = candidates_store(base)
        for entry in cand.iterate():
            if org_hash not in _sources(entry):
                continue
            remaining = [s for s in entry.session_ids if s != org_hash]
            status = _status(entry)
            if not remaining or (status != STATUS_APPROVED and len(set(remaining)) < min_orgs):
                cand.delete(entry.id)
                report.candidates_dropped += 1
                continue
            entry.session_ids = remaining
            entry.source_session_ids = list(remaining)
            cand.upsert(entry)
            report.candidates_updated += 1
            if status == STATUS_APPROVED and len(set(remaining)) < min_orgs:
                report.approved_below_min += 1
    return report


def forget_user(base: Any, *, user_id: str | None = None, user_hash: str | None = None) -> bool:
    """Delete a user's collection. Returns whether one existed. User data never
    feeds the candidate queue, so nothing else needs cleanup."""
    if (user_id is None) == (user_hash is None):
        raise ValueError("pass exactly one of user_id or user_hash")
    name = user_collection_name(user_id) if user_id else USER_PREFIX + str(user_hash)
    with admin_access():
        if name not in base.list_collections():
            return False
        base.sibling(name).drop()
        return True
