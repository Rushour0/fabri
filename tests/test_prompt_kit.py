"""B5 -- prompt-kit: the prompt-skeleton renderer and the user-prose /
machine-memory output splitter. All pure functions -- no LLM, no store, no
network."""
from __future__ import annotations

from fabri.builder import (
    AGENT_MEMORY_MARKER,
    format_agent_memory,
    new_prompt,
    render_prompt_template,
    split_agent_output,
)
from fabri.orchestrator.pipeline import extract_agent_memory


# ---------------------------------------------------------------------------
# render_prompt_template: section order + caller-supplied content
# ---------------------------------------------------------------------------

_SECTION_HEADERS = [
    "ABSOLUTE SCOPE",
    "RETRIEVED CONTEXT",
    "CHARTER",
    "WHAT YOU OWN",
    "DECOMPOSITION RULES",
    "VERIFICATION LADDER",
    "TOOL ROUTING",
    "HARD INVARIANTS",
    "OUTPUT FORMAT",
]


def test_template_has_all_sections_in_order():
    text = render_prompt_template("data normalizer")
    positions = [text.index(f"# {h}") for h in _SECTION_HEADERS]
    assert positions == sorted(positions), "section headers out of order"
    # role lands in the charter; the default is coherent, not blank
    assert "data normalizer" in text


def test_template_fills_owns_and_tools():
    text = render_prompt_template(
        "reviewer",
        owns=["the review report", "the line-level findings"],
        tools=["read_file", "run_shell -- run a read-only shell command"],
    )
    assert "- the review report" in text
    assert "- the line-level findings" in text
    # a bare tool name gets a routing fill-in; a tool with its own note is kept
    assert "- read_file -- <when to reach for it and what it returns>" in text
    assert "- run_shell -- run a read-only shell command" in text


def test_template_describes_the_memory_marker():
    # OUTPUT FORMAT must teach the same marker the splitter parses.
    text = render_prompt_template("agent")
    assert AGENT_MEMORY_MARKER in text


def test_new_prompt_writes_a_file_from_template(tmp_path):
    out = tmp_path / "my_agent.prompt.md"
    result = new_prompt("my_agent", output=out)
    assert result["created"] is True
    body = out.read_text()
    # a real starter, not a blank file
    assert "# ABSOLUTE SCOPE" in body
    assert "# OUTPUT FORMAT" in body
    assert len(body) > 200


def test_new_prompt_refuses_to_clobber_without_force(tmp_path):
    out = tmp_path / "p.md"
    out.write_text("keep me")
    result = new_prompt("p", output=out)
    assert result["created"] is False
    assert out.read_text() == "keep me"
    # force overwrites
    forced = new_prompt("p", output=out, force=True)
    assert forced["created"] is True
    assert "# ABSOLUTE SCOPE" in out.read_text()


# ---------------------------------------------------------------------------
# split_agent_output: marker present / absent + round-trip
# ---------------------------------------------------------------------------


def test_split_without_marker_returns_text_and_none():
    text = "Here is the plain answer with no memory block."
    prose, memory = split_agent_output(text)
    assert prose == text
    assert memory is None


def test_split_with_marker_parses_keys_and_nested_list():
    text = (
        "Done. The dataset is cleaned.\n\n"
        f"{AGENT_MEMORY_MARKER}\n"
        "TASK: clean the input dataset\n"
        "OUTCOME: success\n"
        "CHANGES:\n"
        "- normalized column names\n"
        "- dropped 3 null rows\n"
    )
    prose, memory = split_agent_output(text)
    assert prose == "Done. The dataset is cleaned."
    assert memory == {
        "TASK": "clean the input dataset",
        "OUTCOME": "success",
        "CHANGES": ["normalized column names", "dropped 3 null rows"],
    }


def test_split_value_with_colon_is_not_misread_as_key():
    text = f"answer\n{AGENT_MEMORY_MARKER}\nTASK: fix bug: the parser choked\n"
    _, memory = split_agent_output(text)
    assert memory == {"TASK": "fix bug: the parser choked"}


def test_split_bare_marker_with_empty_block_is_none():
    prose, memory = split_agent_output(f"answer\n{AGENT_MEMORY_MARKER}\n")
    assert prose == "answer"
    assert memory is None


def test_split_round_trips_via_format_agent_memory():
    memory = {
        "TASK": "build the index",
        "OUTCOME": "success",
        "CHANGES": ["added a cache", "tightened the schema"],
    }
    text = "Prose answer.\n\n" + format_agent_memory(memory)
    prose, parsed = split_agent_output(text)
    assert prose == "Prose answer."
    assert parsed == memory


# ---------------------------------------------------------------------------
# pipeline wiring: extract_agent_memory is additive + guarded
# ---------------------------------------------------------------------------


def test_extract_agent_memory_from_final_event():
    events = [
        {"type": "start", "task": "t"},
        {"type": "final", "outcome": "success",
         "text": f"ok\n{AGENT_MEMORY_MARKER}\nTASK: t\nOUTCOME: success\n"},
    ]
    assert extract_agent_memory(events) == {"TASK": "t", "OUTCOME": "success"}


def test_extract_agent_memory_none_without_marker_or_final():
    # final present but no marker -> None (no behaviour change for the miner)
    assert extract_agent_memory(
        [{"type": "final", "outcome": "success", "text": "plain answer"}]
    ) is None
    # no final event at all -> None
    assert extract_agent_memory([{"type": "start", "task": "t"}]) is None
