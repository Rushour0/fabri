"""The Improver — plug-and-play log ingestion (fabri.readlogs / `fabri ingest`).

Everything here runs offline: adapters are pure functions over raw lines, the
store is an in-process cosine store (embeddings are cached locally), and the
deterministic ingest path uses NoOpLLM so it costs $0 and never touches a
provider. The one synthesize=True test drives a ScriptedLLMBackend.
"""
import json

import pytest

import fabri
from fabri.core.llm import LLMResponse, LLMUsage, ScriptedLLMBackend
from fabri.events import EventType
from fabri.ingest import Improver, NoOpLLM, readlogs
from fabri.ingest.adapters.base import normalize_events, valid_event
from fabri.ingest.adapters.builtins import jsonl_adapter, otel_adapter, regex_adapter
from fabri.ingest.adapters.configmap import ConfigMapAdapter
from fabri.ingest.registry import UnknownAdapterError, resolve_adapter
from fabri.ingest.sources import LogSource
from fabri.memory.embeddings import embed
from fabri.orchestrator.pipeline import process_trace
from fabri.orchestrator.retrieval import retrieve_context


# --------------------------------------------------------------------------- #
# A faithful in-memory vector store (embeddings are normalized → dot == cosine) #
# --------------------------------------------------------------------------- #
class InMemoryVectorStore:
    def __init__(self, collection="test"):
        self.collection = collection
        self._e = {}

    def upsert(self, entry):
        self._e[entry.id] = (entry, embed(entry.text))
        return entry.id

    def query_by_vector(self, vector, top_k=5, kind=None, tools_any=None):
        scored = []
        for entry, vec in self._e.values():
            if kind is not None and entry.kind != kind:
                continue
            if tools_any is not None and not (set(entry.tools or []) & set(tools_any)):
                continue
            scored.append((entry, sum(a * b for a, b in zip(vector, vec))))
        scored.sort(key=lambda p: p[1], reverse=True)
        return scored[:top_k]

    def query(self, text, top_k=5, kind=None, tools_any=None):
        return self.query_by_vector(embed(text), top_k=top_k, kind=kind, tools_any=tools_any)

    def find_similar(self, text, threshold=0.85, kind=None):
        r = self.query(text, top_k=1, kind=kind)
        return r[0] if r and r[0][1] >= threshold else None

    def find_by_dedup_key(self, dedup_key, kind=None):
        for entry, _ in self._e.values():
            if entry.dedup_key == dedup_key and (kind is None or entry.kind == kind):
                return entry, 1.0
        return None

    def count(self, kind=None):
        if kind is None:
            return len(self._e)
        return sum(1 for e, _ in self._e.values() if e.kind == kind)

    def iterate(self, kind=None, limit=None):
        out = [e for e, _ in self._e.values() if kind is None or e.kind == kind]
        return out[:limit] if limit else out

    def delete(self, pid):
        self._e.pop(pid, None)


@pytest.fixture(autouse=True)
def _fabri_home(tmp_path, monkeypatch):
    # ingest_guideline takes a per-collection fcntl lock under $FABRI_HOME/.fabri/locks.
    monkeypatch.setenv("FABRI_HOME", str(tmp_path))


def _jsonl(events):
    return [json.dumps(e) for e in events]


# --------------------------------------------------------------------------- #
# Adapters — pure normalization                                                #
# --------------------------------------------------------------------------- #
def test_jsonl_adapter_whole_file_one_session():
    events = [
        {"type": "start", "task": "do the thing"},
        {"type": "tool_call", "name": "t", "args": {}, "result": {"ok": False, "error": "boom"}},
        {"type": "final", "outcome": "failed"},
    ]
    src = LogSource.from_any(_jsonl(events))
    sessions = list(jsonl_adapter(src, {}))
    assert len(sessions) == 1
    assert [e["type"] for e in sessions[0].events] == ["start", "tool_call", "final"]


def test_jsonl_adapter_splits_on_session_key():
    records = [
        {"type": "start", "task": "a", "session_id": "s1"},
        {"type": "tool_call", "name": "x", "result": {"ok": True}, "session_id": "s1"},
        {"type": "start", "task": "b", "session_id": "s2"},
    ]
    src = LogSource.from_any([json.dumps(r) for r in records])
    sessions = list(jsonl_adapter(src, {}))
    assert len(sessions) == 2  # grouped by session_id


def test_regex_adapter_maps_named_groups_and_status():
    lines = [
        "req=1 doing checkout tool=validate status=ok",
        "req=1 doing checkout tool=save status=error msg=disk full",
    ]
    src = LogSource.from_any(lines)
    opt = {"pattern": r"req=(?P<session>\d+) doing (?P<task>\w+) tool=(?P<tool>\S+) status=(?P<status>\w+)(?: msg=(?P<error>.*))?"}
    sessions = list(regex_adapter(src, opt))
    assert len(sessions) == 1
    calls = [e for e in sessions[0].events if e["type"] == "tool_call"]
    assert calls[0]["result"]["ok"] is True
    assert calls[1]["result"]["ok"] is False and calls[1]["result"]["error"] == "disk full"


def test_regex_adapter_requires_pattern():
    with pytest.raises(ValueError):
        list(regex_adapter(LogSource.from_any(["x"]), {}))


def test_otel_adapter_spans_grouped_by_trace():
    records = [
        {"trace_id": "T", "name": "fetch", "status": "ok", "input": "grab data"},
        {"trace_id": "T", "name": "write", "status": "error", "error": "perm denied"},
    ]
    src = LogSource.from_any([json.dumps(r) for r in records])
    sessions = list(otel_adapter(src, {}))
    assert len(sessions) == 1
    calls = [e for e in sessions[0].events if e["type"] == "tool_call"]
    assert calls[-1]["result"]["ok"] is False


def test_configmap_adapter_dotted_paths():
    records = [
        {"trace": "t1", "prompt": "ship it", "tool": {"name": "deploy", "ok": True}},
        {"trace": "t1", "prompt": "ship it", "tool": {"name": "verify", "ok": False, "err": "flaky"}},
    ]
    src = LogSource.from_any([json.dumps(r) for r in records])
    adapter = ConfigMapAdapter("prod", {
        "session_key": "trace", "task_field": "prompt",
        "tool_field": "tool.name", "ok_field": "tool.ok", "error_field": "tool.err",
    })
    sessions = list(adapter.sessions(src, {}))
    assert len(sessions) == 1
    start = sessions[0].events[0]
    assert start["type"] == "start" and start["task"] == "ship it"
    calls = [e for e in sessions[0].events if e["type"] == "tool_call"]
    assert calls[0]["name"] == "deploy" and calls[1]["result"]["ok"] is False


def test_configmap_requires_tool_field():
    with pytest.raises(ValueError):
        ConfigMapAdapter("bad", {"session_key": "x"})


def test_configmap_string_status_is_not_always_truthy():
    # Regression: a string ok_field like "false"/"error" must read as FAILURE,
    # not success (plain bool("false") is True). Real bools still pass through.
    records = [
        {"trace": "t1", "prompt": "ship", "tool": {"name": "deploy", "ok": "true"}},
        {"trace": "t1", "prompt": "ship", "tool": {"name": "verify", "ok": "false"}},
        {"trace": "t1", "prompt": "ship", "tool": {"name": "probe", "ok": "error"}},
    ]
    src = LogSource.from_any([json.dumps(r) for r in records])
    adapter = ConfigMapAdapter("prod", {
        "session_key": "trace", "task_field": "prompt",
        "tool_field": "tool.name", "ok_field": "tool.ok",
    })
    calls = [e for e in list(adapter.sessions(src, {}))[0].events if e["type"] == "tool_call"]
    assert calls[0]["result"]["ok"] is True    # "true"  -> success
    assert calls[1]["result"]["ok"] is False   # "false" -> failure (was truthy before fix)
    assert calls[2]["result"]["ok"] is False   # "error" -> failure


# --------------------------------------------------------------------------- #
# LogSource.peek() — must not double-yield re-iterable (list/tuple) sources    #
# --------------------------------------------------------------------------- #
def test_peek_does_not_duplicate_list_source():
    # Regression: peek() on a list source used to re-append the whole list,
    # yielding every item twice on the auto-sniff path.
    src = LogSource.from_any(["a", "b", "c"])
    assert src.peek(5) == ["a", "b", "c"]
    assert list(src.lines()) == ["a", "b", "c"]


def test_peek_partial_then_full_iterate_list_source():
    src = LogSource.from_any(["a", "b", "c", "d", "e"])
    assert src.peek(2) == ["a", "b"]
    assert list(src.lines()) == ["a", "b", "c", "d", "e"]


def test_peek_generator_source_still_consumes_once():
    src = LogSource.from_any(iter(["x", "y", "z"]))
    assert src.peek(2) == ["x", "y"]
    assert list(src.lines()) == ["x", "y", "z"]


def test_readlogs_auto_adapter_matches_explicit_jsonl():
    # Regression (headline SDK path): readlogs(list) with the DEFAULT adapter
    # ('auto' -> sniff -> peek) must not double-count in-memory list logs.
    auto = readlogs(
        _jsonl(_run_events()),
        config={"memory": {}, "ingest": {}}, store=InMemoryVectorStore(),
    )
    explicit = readlogs(
        _jsonl(_run_events()), adapter="jsonl",
        config={"memory": {}, "ingest": {}}, store=InMemoryVectorStore(),
    )
    assert auto.sessions == explicit.sessions == 1
    assert auto.failures_mined == explicit.failures_mined == 1


# --------------------------------------------------------------------------- #
# Event validation / normalization                                             #
# --------------------------------------------------------------------------- #
def test_normalize_drops_malformed_events():
    events = [
        {"type": "start", "task": "ok"},
        {"no_type": True},                                  # dropped: no type
        {"type": "tool_call", "name": "t"},                 # dropped: no result dict
        {"type": "tool_call", "name": "t", "result": {"ok": True}},  # kept
    ]
    kept, skipped = normalize_events(events)
    assert skipped == 2 and len(kept) == 2


def test_valid_event_tool_call_needs_name_and_result():
    assert valid_event({"type": "tool_call", "name": "t", "result": {"ok": True}})
    assert not valid_event({"type": "tool_call", "result": {"ok": True}})
    assert not valid_event({"type": "tool_call", "name": "t"})


# --------------------------------------------------------------------------- #
# Registry + discovery                                                          #
# --------------------------------------------------------------------------- #
def test_builtins_registered():
    names = fabri.list_adapters()
    assert {"jsonl", "regex", "otel", "openai"} <= set(names)


def test_decorator_registers_and_resolves():
    @fabri.adapter("unit_custom")
    def _custom(source, options):
        from fabri.ingest.adapters.base import Session, start_event
        yield Session("s", [start_event("t")])

    adp = resolve_adapter("unit_custom")
    assert adp.name == "unit_custom"
    sessions = list(adp.sessions(LogSource.from_any([]), {}))
    assert sessions[0].session_id == "s"


def test_unknown_adapter_lists_available():
    from fabri.ingest import get_adapter

    with pytest.raises(UnknownAdapterError) as ei:
        get_adapter("does_not_exist")
    assert "jsonl" in ei.value.available


def test_auto_sniff_picks_jsonl_for_native_events():
    src = LogSource.from_any(_jsonl([{"type": "start", "task": "x"}]))
    assert resolve_adapter("auto", src).name == "jsonl"


def test_auto_sniff_picks_regex_for_plaintext():
    src = LogSource.from_any(["just some plaintext line"])
    assert resolve_adapter("auto", src).name == "regex"


# --------------------------------------------------------------------------- #
# process_trace backward-compat + the events=/synthesize= knobs                 #
# --------------------------------------------------------------------------- #
def _run_events(task="charge a payment"):
    return [
        {"type": EventType.START.value, "task": task},
        {"type": EventType.TOOL_CALL.value, "name": "charge", "args": {}, "result": {"ok": False, "error": "429 rate limited"}},
        {"type": EventType.TOOL_CALL.value, "name": "charge", "args": {}, "result": {"ok": True}},
        {"type": EventType.FINAL.value, "outcome": "success"},
    ]


def test_process_trace_deterministic_events_no_llm():
    store = InMemoryVectorStore()
    entries = process_trace(
        "sess-1", store, NoOpLLM(), events=_run_events(),
        record_postmortem=True, synthesize=False,
    )
    kinds = {e.kind for e in entries}
    assert kinds == {"postmortem", "success_pattern", "tactical"}
    # NoOpLLM never raised → no synthesis call happened.


def test_process_trace_reads_disk_when_events_none(monkeypatch):
    # Back-compat: the default path still calls read_trace(session_id).
    called = {}

    def fake_read_trace(sid):
        called["sid"] = sid
        return _run_events()

    monkeypatch.setattr("fabri.orchestrator.pipeline.read_trace", fake_read_trace)
    store = InMemoryVectorStore()
    process_trace("disk-sess", store, NoOpLLM(), synthesize=False, record_postmortem=True)
    assert called["sid"] == "disk-sess"


# --------------------------------------------------------------------------- #
# readlogs — round-trip ("routes back into the agent")                          #
# --------------------------------------------------------------------------- #
def test_readlogs_deterministic_round_trip():
    store = InMemoryVectorStore()
    summary = readlogs(
        _jsonl(_run_events()), adapter="jsonl",
        config={"memory": {}, "ingest": {}}, store=store,
    )
    assert summary.sessions == 1
    assert summary.failures_mined == 1
    assert summary.llm_cost_usd == 0.0
    assert set(summary.by_kind) == {"postmortem", "success_pattern", "tactical"}

    # The ingested knowledge is retrievable by a future agent run.
    ctx = retrieve_context(store, "charge a payment", top_k=5)
    assert "charge" in ctx


def test_readlogs_dir_and_iterator_and_stdin_equivalent(tmp_path):
    store = InMemoryVectorStore()
    log = tmp_path / "a.jsonl"
    log.write_text("\n".join(_jsonl(_run_events())))
    summary = readlogs(str(log), adapter="jsonl", config={"memory": {}, "ingest": {}}, store=store)
    assert summary.sessions == 1 and store.count() >= 2


def test_ingest_stream_yields_per_session():
    store = InMemoryVectorStore()
    imp = Improver(store, {"memory": {}, "ingest": {}}, synthesize=False)
    lines = _jsonl([
        {"type": "start", "task": "a", "session_id": "s1"},
        {"type": "tool_call", "name": "x", "result": {"ok": False, "error": "e"}, "session_id": "s1"},
        {"type": "start", "task": "b", "session_id": "s2"},
        {"type": "tool_call", "name": "y", "result": {"ok": True}, "session_id": "s2"},
    ])
    got = list(imp.ingest_stream(lines, adapter="jsonl"))
    assert len(got) == 2  # one summary per session


def test_dry_run_writes_nothing():
    store = InMemoryVectorStore()
    summary = readlogs(_jsonl(_run_events()), adapter="jsonl",
                       config={"memory": {}, "ingest": {}}, store=store, dry_run=True)
    assert summary.sessions == 1 and store.count() == 0


# --------------------------------------------------------------------------- #
# synthesize=True — LLM path, driven offline by a scripted backend             #
# --------------------------------------------------------------------------- #
def test_readlogs_synthesize_uses_llm_and_prices():
    store = InMemoryVectorStore()
    usage = LLMUsage(input_tokens=10, output_tokens=5, model="claude-sonnet-4-6")
    # Semantically distinct text per synthesis call so the success pattern and
    # the failure guideline don't collapse into one entry via similarity dedup.
    texts = [
        "validate the schema before writing to storage",
        "add an idempotency key when charging a payment",
        "cache DNS lookups to cut redundant network calls",
        "prefer batch inserts over per-row database writes",
    ]
    script = [LLMResponse(final_text=t, usage=usage) for t in texts]
    llm = ScriptedLLMBackend(script)
    summary = readlogs(
        _jsonl(_run_events()), adapter="jsonl",
        config={"memory": {}, "ingest": {}}, store=store,
        synthesize=True, llm=llm,
    )
    assert summary.llm_cost_usd > 0.0  # priced from the scripted usage
    assert summary.by_kind.get("tactical", 0) >= 1


def test_process_trace_merges_llm_paraphrases_by_dedup_key():
    store = InMemoryVectorStore()
    events = [
        {"type": "start", "task": "Charge a payment safely"},
        {
            "type": "tool_call", "name": "charge_card", "args": {},
            "result": {"ok": False, "error": "Card token expired\nretry failed"},
        },
        {"type": "final", "outcome": "failed"},
    ]
    llm = ScriptedLLMBackend([
        LLMResponse(final_text="Refresh an expired card token before charging again."),
        LLMResponse(final_text="Obtain a new payment credential prior to retrying a declined transaction."),
    ])

    process_trace("session-one", store, llm, events=events, synthesize=True)
    process_trace("session-two", store, llm, events=events, synthesize=True)

    entries = store.iterate(kind="tactical")
    assert len(entries) == 1
    assert entries[0].hit_count == 2
    assert set(entries[0].session_ids) == {"session-one", "session-two"}


def test_noop_llm_raises_if_called():
    with pytest.raises(RuntimeError):
        NoOpLLM().step("sys", [])
