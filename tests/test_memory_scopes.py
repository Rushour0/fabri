import pytest

from fabri.memory.pruning import ingest_guideline
from fabri.memory.scope_classify import classify_scope
from fabri.memory.scopes import (
    GLOBAL_COLLECTION,
    MemoryScope,
    ScopedMemory,
    ScopeViolation,
    admin_access,
    check_collection_access,
    hash_id,
    org_collection_name,
    scope_context,
    user_collection_name,
)
from tests.scoped_helpers import backend_params, make_base

A1 = MemoryScope(user_id="user-a1", org_id="org-a", aliases=("Acme Corp",))
A2 = MemoryScope(user_id="user-a2", org_id="org-a")
B1 = MemoryScope(user_id="user-b1", org_id="org-b")


def test_collection_names_are_hashed_not_raw():
    scope = MemoryScope(user_id="Alice@Example.com", org_id="Acme Corp")
    for name in (scope.user_collection, scope.org_collection):
        assert "alice" not in name.lower() and "acme" not in name.lower() and "@" not in name
    # case and whitespace do not change the id
    assert user_collection_name(" alice@example.com ") == scope.user_collection
    assert user_collection_name("alice@example.com") != org_collection_name("alice@example.com")


def test_scope_requires_ids():
    with pytest.raises(ValueError):
        MemoryScope(user_id="", org_id="o")
    with pytest.raises(ValueError):
        hash_id("user", "  ")


def test_from_config_off_by_default():
    assert MemoryScope.from_config({}) is None
    assert MemoryScope.from_config({"scoped": {"enabled": True, "user_id": "u"}}) is None
    scope = MemoryScope.from_config(
        {"scoped": {"enabled": True, "user_id": "u", "org_id": "o", "project_id": "p"}}
    )
    assert scope == MemoryScope("u", "o", "p")


def test_guard_pure_function():
    # unscoped collections always pass
    check_collection_access("fabri")
    check_collection_access(GLOBAL_COLLECTION)
    # scoped collection with nothing in force: fail closed
    with pytest.raises(ScopeViolation):
        check_collection_access(A1.user_collection)
    with scope_context(A1):
        check_collection_access(A1.user_collection)
        check_collection_access(A1.org_collection)
        with pytest.raises(ScopeViolation):
            check_collection_access(A2.user_collection)
        with pytest.raises(ScopeViolation):
            check_collection_access(B1.org_collection)
    with admin_access():
        check_collection_access(B1.user_collection)


def test_bound_scope_must_match_scope_in_force():
    with scope_context(A2):
        with pytest.raises(ScopeViolation):
            check_collection_access(A1.user_collection, bound=A1)


@pytest.mark.parametrize("backend", backend_params())
def test_store_rejects_foreign_collection(backend, tmp_path):
    base = make_base(backend, tmp_path)
    # Opening another user's collection under my scope is refused at construction.
    with scope_context(A1):
        with pytest.raises(ScopeViolation):
            base.sibling(A2.user_collection)
        with pytest.raises(ScopeViolation):
            base.sibling(B1.org_collection)
    # No scope in force and none bound: refused.
    with pytest.raises(ScopeViolation):
        base.sibling(A1.user_collection)
    # Bound to A1 but a call made while B1 is in force is refused.
    mine = base.sibling(A1.user_collection, bound_scope=A1)
    with scope_context(B1):
        with pytest.raises(ScopeViolation):
            mine.count()
        with pytest.raises(ScopeViolation):
            mine.query("anything")
    assert mine.count() == 0


@pytest.mark.parametrize("backend", backend_params())
def test_guard_catches_collection_swap_after_construction(backend, tmp_path):
    base = make_base(backend, tmp_path)
    mine = base.sibling(A1.user_collection, bound_scope=A1)
    mine.collection = A2.user_collection  # a caller bug or an attack
    with pytest.raises(ScopeViolation):
        mine.count()


@pytest.mark.parametrize("backend", backend_params())
def test_user_and_org_isolation(backend, tmp_path):
    base = make_base(backend, tmp_path)
    mem = {s.user_id: ScopedMemory.from_store(base, s) for s in (A1, A2, B1)}
    ingest_guideline(mem["user-a1"].store("user"), "I prefer short bullet answers with no emojis.", "s1")
    ingest_guideline(mem["user-a1"].store("org"), "Re-read the file after write_file to confirm it persisted.", "s1")
    ingest_guideline(mem["user-b1"].store("org"), "Orgbee only: always run the migration dry run before deploy.", "s2")

    # Same org, different user: org lesson visible, user lesson not.
    text_a2, meta_a2 = mem["user-a2"].retrieve("write_file persisted re-read file")
    assert "Re-read the file after write_file" in text_a2
    assert "short bullet answers" not in text_a2
    assert "migration dry run" not in text_a2

    # Different org: sees nothing of org A or user A1.
    text_b1, _ = mem["user-b1"].retrieve("prefer short bullet answers write_file re-read")
    assert "short bullet answers" not in text_b1
    assert "Re-read the file after write_file" not in text_b1

    # The owner sees their private item.
    text_a1, _ = mem["user-a1"].retrieve("prefer short bullet answers")
    assert "short bullet answers" in text_a1

    # Direct reads of a foreign store object also fail.
    with scope_context(A2):
        with pytest.raises(ScopeViolation):
            mem["user-a1"].store("user").iterate()  # bound to A1


def test_classifier_rules():
    assert classify_scope("I prefer short answers.").scope == "user"
    assert classify_scope("Call me Sam in the greeting.").scope == "user"
    assert classify_scope("The user prefers metric units.").scope == "user"
    assert classify_scope("anything", kind="preference").scope == "user"
    assert classify_scope("The export tool fails when the file is empty; retry after check.").scope == "org"
    assert classify_scope("Our team policy: approval is required before refunds.").scope == "org"
    # personal marker beats org marker (narrower scope wins)
    assert classify_scope("I prefer to follow our team process.").scope == "user"
    # identity mention
    assert classify_scope("Note for user-a1: use tabs", scope=A1).scope == "user"
    # no rule: default, then optional llm
    assert classify_scope("blue sky").method == "default"


def test_classifier_llm_flag_only_when_rules_silent():
    class Resp:
        def __init__(self, t):
            self.final_text, self.usage = t, None

    class LLM:
        calls = 0

        def step(self, system, messages):
            LLM.calls += 1
            return Resp("USER")

    llm = LLM()
    # rules decide: llm not called
    assert classify_scope("I prefer tea", llm=llm, use_llm=True).method == "rule"
    assert LLM.calls == 0
    # flag off: llm not called
    assert classify_scope("blue sky", llm=llm, use_llm=False).scope == "org"
    assert LLM.calls == 0
    d = classify_scope("blue sky", llm=llm, use_llm=True)
    assert (d.scope, d.method, LLM.calls) == ("user", "llm", 1)


@pytest.mark.parametrize("backend", backend_params())
def test_route_never_targets_global(backend, tmp_path):
    mem = ScopedMemory.from_store(make_base(backend, tmp_path), A1)
    tag, _ = mem.route("I prefer dark mode")
    assert tag == "user"
    tag, _ = mem.route("The sync job times out on large files")
    assert tag == "org"
    with pytest.raises(ValueError):
        mem.route("x", force="global")
