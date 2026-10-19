import json

import pytest

from fabri.memory.global_promotion import (
    approve,
    candidates_store,
    forget_org,
    forget_user,
    global_store,
    list_candidates,
    reject,
    scan_orgs,
)
from fabri.memory.pruning import ingest_guideline
from fabri.memory.scopes import (
    CANDIDATES_COLLECTION,
    GLOBAL_COLLECTION,
    MemoryScope,
    ScopedMemory,
    admin_access,
)
from tests.scoped_helpers import backend_params, make_base

ORGS = {
    "acme": ("Acme Corp", "After write_file at /srv/acme/report.csv, re-read the file to confirm the write persisted."),
    "globex": ("Globex Inc", "After write_file at /srv/globex/q3.csv, re-read the file to confirm the write persisted."),
    "initech": ("Initech LLC", "After write_file at /srv/initech/ledger.csv, re-read the file to confirm the write persisted."),
}
ODD = "Orgwide only: Acme Corp pays vendor 4417 on the 3rd, never on weekends."


def _scope(org_key: str) -> MemoryScope:
    return MemoryScope(user_id=f"user-{org_key}", org_id=f"org-{org_key}", aliases=(ORGS[org_key][0],))


def _seed(base, keys=("acme", "globex", "initech")):
    for key in keys:
        mem = ScopedMemory.from_store(base, _scope(key))
        store = mem.store("org")
        e = ingest_guideline(store, ORGS[key][1], f"sess-{key}", kind="tactical")
        mem.annotate(e, "org", store)
    # one org-specific lesson that must never leave its org
    mem = ScopedMemory.from_store(base, _scope("acme"))
    e = ingest_guideline(mem.store("org"), ODD, "sess-acme2")
    mem.annotate(e, "org", mem.store("org"))


def _all_candidate_text(base) -> str:
    return json.dumps([e.to_payload() for e in candidates_store(base).iterate()])


@pytest.mark.parametrize("backend", backend_params())
def test_promotion_needs_n_distinct_orgs(backend, tmp_path):
    base = make_base(backend, tmp_path)
    _seed(base, keys=("acme", "globex"))
    scan_orgs(base, min_orgs=3)
    assert list_candidates(base, min_orgs=3) == []  # two orgs: not enough
    assert [c.source_count for c in list_candidates(base, min_orgs=2)] == [2]

    _seed(base, keys=("initech",))
    scan_orgs(base, min_orgs=3)
    found = list_candidates(base, min_orgs=3)
    assert len(found) == 1 and found[0].source_count == 3
    # org-specific lesson (one org) never surfaces
    odd = [c for c in list_candidates(base, min_orgs=1) if "vendor" in c.text]
    assert odd and all(c.source_count == 1 for c in odd)


@pytest.mark.parametrize("backend", backend_params())
def test_same_org_repeating_does_not_count_twice(backend, tmp_path):
    base = make_base(backend, tmp_path)
    mem = ScopedMemory.from_store(base, _scope("acme"))
    store = mem.store("org")
    for i in range(4):  # many sessions, one org
        e = ingest_guideline(store, ORGS["acme"][1], f"s{i}", kind="tactical")
        mem.annotate(e, "org", store)
    scan_orgs(base, min_orgs=2)
    assert list_candidates(base, min_orgs=2) == []


@pytest.mark.parametrize("backend", backend_params())
def test_candidates_are_deidentified_and_need_approval(backend, tmp_path):
    base = make_base(backend, tmp_path)
    _seed(base)
    scan_orgs(base, min_orgs=3)

    blob = _all_candidate_text(base)
    for raw in ("Acme", "Globex", "Initech", "/srv/", "4417", "org-acme", "user-acme"):
        assert raw not in blob, raw

    # nothing in GLOBAL before approval, and retrieval cannot see it
    assert GLOBAL_COLLECTION not in base.list_collections() or global_store(base).count() == 0
    reader = ScopedMemory.from_store(base, MemoryScope("user-new", "org-new"))
    text, _ = reader.retrieve("after write_file re-read the file persisted")
    assert "re-read the file" not in text

    cand = list_candidates(base, min_orgs=3)[0]
    approve(base, cand.id[:8], min_orgs=3)
    assert global_store(base).count() == 1
    assert list_candidates(base, min_orgs=3) == []  # no longer pending
    assert list_candidates(base, min_orgs=3, status="approved")[0].id == cand.id

    # any scope now sees it, tagged global; no org id or name in the global entry
    text, meta = reader.retrieve("after write_file re-read the file persisted")
    assert "- [global] [strategic]" in text and "re-read the file" in text
    assert meta["by_scope"]["global"] == 1
    glob_blob = json.dumps([e.to_payload() for e in global_store(base).iterate()])
    assert "org-" not in glob_blob and "Acme" not in glob_blob


@pytest.mark.parametrize("backend", backend_params())
def test_reject_and_below_n_cannot_be_approved(backend, tmp_path):
    base = make_base(backend, tmp_path)
    _seed(base)
    scan_orgs(base, min_orgs=3)
    cand = list_candidates(base, min_orgs=3)[0]
    with pytest.raises(ValueError):
        approve(base, cand.id, min_orgs=4)
    reject(base, cand.id)
    assert list_candidates(base, min_orgs=3) == []
    with pytest.raises(ValueError):
        approve(base, cand.id, min_orgs=3)
    assert GLOBAL_COLLECTION not in base.list_collections() or global_store(base).count() == 0
    with pytest.raises(KeyError):
        approve(base, "nope", min_orgs=3)


@pytest.mark.parametrize("backend", backend_params())
def test_scan_never_reads_user_collections(backend, tmp_path):
    base = make_base(backend, tmp_path)
    for key in ("acme", "globex", "initech"):
        mem = ScopedMemory.from_store(base, _scope(key))
        ingest_guideline(mem.store("user"), "I prefer the retry loop to wait a little before the next try.", f"u-{key}")
    report = scan_orgs(base, min_orgs=3)
    assert report.orgs_scanned == 0
    assert CANDIDATES_COLLECTION not in base.list_collections() or candidates_store(base).count() == 0


@pytest.mark.parametrize("backend", backend_params())
def test_forget_org_drops_collection_and_weakens_candidates(backend, tmp_path):
    base = make_base(backend, tmp_path)
    _seed(base)
    scan_orgs(base, min_orgs=3)
    assert len(list_candidates(base, min_orgs=3)) == 1

    report = forget_org(base, org_id="org-acme", min_orgs=3)
    assert report.collection_dropped
    assert _scope("acme").org_collection not in base.list_collections()
    assert report.candidates_dropped >= 1
    assert list_candidates(base, min_orgs=3, status=None) == []
    # other orgs are untouched
    assert _scope("globex").org_collection in base.list_collections()
    # a re-scan with only two orgs left does not bring it back
    scan_orgs(base, min_orgs=3)
    assert list_candidates(base, min_orgs=3, status=None) == []


@pytest.mark.parametrize("backend", backend_params())
def test_forget_org_flags_approved_candidates(backend, tmp_path):
    base = make_base(backend, tmp_path)
    _seed(base)
    scan_orgs(base, min_orgs=3)
    approve(base, list_candidates(base, min_orgs=3)[0].id, min_orgs=3)
    report = forget_org(base, org_id="org-globex", min_orgs=3)
    assert report.approved_below_min == 1  # human must review the live global entry


@pytest.mark.parametrize("backend", backend_params())
def test_forget_user(backend, tmp_path):
    base = make_base(backend, tmp_path)
    mem = ScopedMemory.from_store(base, _scope("acme"))
    ingest_guideline(mem.store("user"), "I prefer short answers.", "s1")
    other = ScopedMemory.from_store(base, MemoryScope("user-other", "org-acme"))
    ingest_guideline(other.store("user"), "I prefer long answers.", "s2")
    assert forget_user(base, user_id="user-acme") is True
    assert _scope("acme").user_collection not in base.list_collections()
    assert other.scope.user_collection in base.list_collections()  # not collateral
    assert forget_user(base, user_id="user-acme") is False
    with admin_access():
        assert other.store("user").count() == 1
