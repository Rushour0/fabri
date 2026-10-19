"""run_agent and process_trace with a MemoryScope: merged tagged retrieval and
scope-routed writes."""
import uuid

import pytest

from fabri import ScriptedLLMBackend, ToolRegistry, run_agent
from fabri.core.llm import LLMResponse
from fabri.memory.global_promotion import approve, list_candidates, scan_orgs
from fabri.memory.pruning import ingest_guideline
from fabri.memory.scopes import MemoryScope, ScopeBudgets, ScopedMemory, scope_context
from fabri.events import emit_discrepancy
from fabri.orchestrator.pipeline import process_trace
from fabri.orchestrator.retrieval import RetrievalConfig
from tests.scoped_helpers import backend_params, make_base

SCOPE = MemoryScope(user_id="user-1", org_id="org-1", aliases=("Acme Corp",))


class RecordingLLM(ScriptedLLMBackend):
    def __init__(self, script):
        super().__init__(script)
        self.systems: list[str] = []

    def step(self, system, messages):
        self.systems.append(system)
        return super().step(system, messages)


class NoopLLM:
    def step(self, *a, **k):
        raise RuntimeError("no llm")


def _seed(base):
    mem = ScopedMemory.from_store(base, SCOPE)
    ingest_guideline(mem.store("user"), "I prefer the export report to list totals first.", "s1")
    ingest_guideline(mem.store("org"), "The export tool fails on empty files, so check the file size before export.", "s1")
    return mem


@pytest.mark.parametrize("backend", backend_params())
def test_run_agent_injects_tagged_scopes(backend, tmp_path):
    base = make_base(backend, tmp_path)
    mem = _seed(base)
    # an approved global lesson
    for key in ("a", "b", "c"):
        other = ScopedMemory.from_store(base, MemoryScope(f"u-{key}", f"o-{key}"))
        e = ingest_guideline(other.store("org"), "The export tool needs a retry after a timeout; wait before the second try.", "s")
        other.annotate(e, "org", other.store("org"))
    scan_orgs(base, min_orgs=3)
    approve(base, list_candidates(base, min_orgs=3)[0].id, min_orgs=3)

    llm = RecordingLLM([LLMResponse(final_text="done")])
    result = run_agent(
        "export the report and retry on timeout",
        llm, ToolRegistry(tmp_path), base,
        memory_scope=SCOPE,
        retrieval_config=RetrievalConfig(),
    )
    assert result["outcome"] == "success"
    system = llm.systems[0]
    assert "- [user] [tactical]" in system and "totals first" in system
    assert "- [org] [tactical]" in system and "check the file size" in system
    assert "- [global] [strategic]" in system and "retry after a timeout" in system


@pytest.mark.parametrize("backend", backend_params())
def test_per_scope_budgets_cap_each_scope(backend, tmp_path):
    base = make_base(backend, tmp_path)
    mem = ScopedMemory.from_store(base, SCOPE, budgets=ScopeBudgets(user=1, org=1, glob=0))
    for i, word in enumerate(["alpha", "bravo", "charlie"]):
        ingest_guideline(mem.store("org"), f"Org note {i}: the {word} export step needs a size check first.", f"s{i}")
        ingest_guideline(mem.store("user"), f"I prefer {word} style export summaries number {i}.", f"s{i}")
    text, meta = mem.retrieve("export size check summaries style")
    assert meta["by_scope"]["user"] <= 1 and meta["by_scope"]["org"] <= 1
    assert text.count("- [user]") <= 1 and text.count("- [org]") <= 1


@pytest.mark.parametrize("backend", backend_params())
def test_run_agent_without_scope_is_unchanged(backend, tmp_path):
    base = make_base(backend, tmp_path)
    ingest_guideline(base, "The export tool fails on empty files; check size first.", "s1")
    llm = RecordingLLM([LLMResponse(final_text="done")])
    run_agent("export the file", llm, ToolRegistry(tmp_path), base, retrieval_config=RetrievalConfig())
    assert "- [tactical]" in llm.systems[0] and "- [org]" not in llm.systems[0]


@pytest.mark.parametrize("backend", backend_params())
def test_process_trace_routes_writes_by_scope(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("FABRI_HOME", str(tmp_path))
    base = make_base(backend, tmp_path)
    mem = ScopedMemory.from_store(base, SCOPE, default_scope="user")
    sid = f"sess_{uuid.uuid4().hex[:8]}"
    emit_discrepancy(sid, "data/map.json", "claimed_not_landed")

    entries = process_trace(sid, base, NoopLLM(), scoped_memory=mem)
    assert len(entries) == 1
    # process lesson (rule match) -> ORG, tagged with scope and aliases
    assert mem.store("org").count() == 1 and mem.store("user").count() == 0
    stored = mem.store("org").iterate()[0]
    assert stored.payload["memory_scope"] == "org"
    assert "Acme Corp" in stored.payload["aliases"]
    # the legacy base collection was not written
    assert base.count() == 0


def test_scoped_memory_is_isolated_from_scope_in_force(tmp_path):
    base = make_base("sqlite", tmp_path)
    mem = _seed(base)
    other = MemoryScope("user-2", "org-1")
    with scope_context(other):
        # org-1 is shared, but user-1's private store is bound to user-1
        with pytest.raises(PermissionError):
            mem.store("user").count()
